"""At most three low-frequency requests using the existing configured identity.

Validate one known saved tweet, then inspect one search hour to infer the missing
tweet ID from the previous incomplete batch. Stop immediately on access denial.
Never prints credentials, response bodies or rotates identities.
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode

from access_diagnostics import response_diagnostics
from extract_topic_keywords import PROJECT
from workflow_store import load_configuration, connect, read_records


def main():
    import requests
    from parsel import Selector
    import re
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "output/access-diagnostic/http.json")
    args = parser.parse_args()
    load_configuration()
    session = requests.Session()
    session.headers.update({"Cookie": os.environ["WEIBO_COOKIE"],
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://weibo.com/",
        "Origin": "https://weibo.com"})
    results = []
    def probe(url, purpose):
        if results:
            time.sleep(5)
        try:
            response = session.get(url, timeout=(10, 25), allow_redirects=False)
            result = {"purpose": purpose, **response_diagnostics(url, response.status_code, response.headers, response.text)}
        except requests.RequestException as exc:
            result = {"purpose": purpose, "error_type": type(exc).__name__}
            response = None
        results.append(result)
        return response
    try:
        response = probe("https://weibo.com/ajax/statuses/show?id=RlD1X4Afy", "known_saved_tweet_control")
        if response is None or response.status_code != 200:
            return
        db = connect()
        try:
            records = read_records(db)
            saved_short_ids = {r["payload"].get("mblogid") for r in records if r["keyword"] == "年锦四抓"}
        finally:
            db.close()
        url = "https://s.weibo.com/weibo?" + urlencode({"q": "年锦四抓",
              "timescope": "custom:2026-10-08-01:2026-10-08-02", "page": 1})
        response = probe(url, "one_search_hour_for_missing_target")
        if response is None or response.status_code != 200:
            return
        links = []
        for href in Selector(text=response.text).css(".from a::attr(href)").getall():
            match = re.search(r"weibo\.com/(\d+)/([A-Za-z0-9]+)", href)
            if match:
                links.append({"author_id": match.group(1), "mblogid": match.group(2)})
        unique = {link["mblogid"]: link for link in links}
        missing = [link for sid, link in sorted(unique.items()) if sid not in saved_short_ids]
        results[-1].update(visible_links=len(unique), missing_saved_targets=missing)
        if len(missing) == 1:
            probe("https://weibo.com/ajax/statuses/show?" + urlencode({"id": missing[0]["mblogid"]}),
                  "single_missing_target_verification")
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(results, ensure_ascii=False))
        session.close()


if __name__ == "__main__":
    main()
