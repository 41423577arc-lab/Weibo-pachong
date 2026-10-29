"""Collect tweets through normal rendered search/post pages in a dedicated Chrome.

Reads responses initiated by the page itself; never reads/exports authentication
or sends substitute API requests. Access checks stop the saved queue.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlparse, parse_qs

from crawl_workflow import write_json
from run_browser_comments import RunLock
from workflow_pipeline import WorkflowPipeline
from workflow_store import connect, read_records


class BrowserAccessStopped(Exception):
    pass


def mid_to_sid(mid):
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
    value = str(mid)
    if not value.isdigit():
        raise ValueError('Numeric tweet ID required')
    chunks = [value[max(0, end - 7):end] for end in range(len(value), 0, -7)][::-1]
    encoded = []
    for index, chunk in enumerate(chunks):
        number, result = int(chunk), ''
        while number:
            number, remainder = divmod(number, 62)
            result = alphabet[remainder] + result
        result = result or '0'
        encoded.append(result.zfill(4) if index else result)
    return ''.join(encoded)


def normalize_tweet(raw, keyword, round_id, longtext=None):
    timestamp = datetime.strptime(raw["created_at"], "%a %b %d %H:%M:%S %z %Y")
    user = raw["user"]
    body = raw.get("text_raw")
    if not isinstance(body, str):
        raise ValueError("Missing tweet text_raw")
    is_long = bool(raw.get("isLongText"))
    inline = raw.get("longText")
    if not longtext and isinstance(inline, dict) and isinstance(inline.get("content"), str) and inline["content"]:
        longtext = {"data": {"longTextContent": inline["content"]}, "source": "inline_status_longText"}
    content = longtext.get("data", {}).get("longTextContent") if longtext else None
    item = {"_id": str(raw.get("mid") or raw["id"]), "mblogid": raw["mblogid"],
        "created_at": timestamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "content": (content if isinstance(content, str) and content else body).replace("\u200b", ""),
        "keyword": keyword, "crawl_time": int(time.time()), "workflow_round_id": round_id,
        "source": raw.get("source"), "collection_source": "normal_browser_page_response",
        "reposts_count": raw.get("reposts_count"), "comments_count": raw.get("comments_count"),
        "attitudes_count": raw.get("attitudes_count"), "isLongText": is_long,
        "long_text_complete": not is_long or bool(content), "is_retweet": "retweeted_status" in raw,
        "user": {"_id": str(user["id"]), "nick_name": user.get("screen_name"),
                 "avatar_hd": user.get("avatar_hd"), "verified": user.get("verified")},
        "url": f"https://weibo.com/{user['id']}/{raw['mblogid']}", "raw_api_json": raw,
        "pic_num": raw.get("pic_num", 0), "pic_urls": [f"https://wx1.sinaimg.cn/orj960/{p}" for p in raw.get("pic_ids", [])],
        "ip_location": raw.get("region_name"), "geo": raw.get("geo")}
    if longtext:
        item["raw_longtext_json"] = longtext
    if raw.get("retweeted_status"):
        item["retweet_id"] = str(raw["retweeted_status"].get("mid") or raw["retweeted_status"].get("id"))
    return item


def initial_queue(job, hour=None):
    start, end = datetime.fromisoformat(job["start"]), datetime.fromisoformat(job["end"])
    if hour is not None:
        start += timedelta(hours=hour)
        end = min(end, start + timedelta(hours=1))
    queue = []
    while start < end:
        following = min(end, start + (timedelta(hours=1) if hour is not None else timedelta(days=1)))
        scope = f"custom:{start:%Y-%m-%d-%H}:{following:%Y-%m-%d-%H}"
        queue.append({"kind": "search", "url": "https://s.weibo.com/weibo?" +
                      urlencode({"q": job["keyword"], "timescope": scope, "page": 1})})
        start = following
    return queue


def safe_search_next(url, previous):
    parsed = urlparse(url)
    query, old = parse_qs(parsed.query), parse_qs(urlparse(previous).query)
    return (parsed.scheme == "https" and parsed.hostname == "s.weibo.com" and parsed.path == "/weibo"
            and query.get("q") == old.get("q") and query.get("timescope") == old.get("timescope"))


async def collect(args, job):
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    checkpoint_path = args.output / "browser-tweets-state.json"
    if checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if state["keyword"] != job["keyword"] or state["start"] != job["start"] or state["end"] != job["end"]:
            raise ValueError("Checkpoint keyword/date mismatch")
    else:
        state = {"keyword": job["keyword"], "start": job["start"], "end": job["end"],
                 "queue": initial_queue(job, args.hour), "seen_pages": [], "saved_posts": [], "events": []}
    state["stop_reason"] = None
    db = connect()
    records = read_records(db)
    saved_sids = {r["payload"].get("mblogid") for r in records if r["keyword"] == job["keyword"]
                  and r["payload"].get("content")}
    known_bodies = {r["payload"].get("mblogid"): r["payload"] for r in records
                    if r["spider"].startswith("tweet") and r["payload"].get("content")
                    and r["payload"].get("long_text_complete") is not False}
    date_cache_path = args.cache or args.output / "post-date-cache.json"
    date_cache = json.loads(date_cache_path.read_text(encoding="utf-8")) if date_cache_path.exists() else {}
    db.commit()
    captured, response_status, handlers = {}, [], []
    navigated = 0
    collected = 0
    incomplete_longtexts = sorted({r["item_id"] for r in records
        if r["keyword"] == job["keyword"] and r["payload"].get("long_text_complete") is False
        and job["start"] <= r["payload"].get("created_at", "") < job["end"]})
    state['incomplete_longtexts'] = incomplete_longtexts
    reused = 0

    async def observe(response):
        parsed = urlparse(response.url)
        if parsed.hostname != "weibo.com" or parsed.path not in ("/ajax/statuses/show", "/ajax/statuses/longtext"):
            return
        sid = parse_qs(parsed.query).get("id", [""])[0]
        response_status.append({"path": parsed.path, "id": sid, "status": response.status})
        if response.status == 200:
            try:
                captured[(parsed.path, sid)] = await response.json()
            except Exception:
                pass

    async def open_page(page, url):
        nonlocal navigated
        if navigated:
            await asyncio.sleep(job["delay"])
        navigated += 1
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            code = re.search(r"net::(ERR_[A-Z_]+)", str(exc))
            raise BrowserAccessStopped(code.group(1) if code else type(exc).__name__) from None
        if response and response.status in (401, 403, 418, 429):
            raise BrowserAccessStopped("navigation_access_limit_" + str(response.status))
        expected, actual = urlparse(url), urlparse(page.url)
        if actual.hostname != expected.hostname or actual.path != expected.path:
            raise BrowserAccessStopped("redirect_or_login_required")
        await page.wait_for_timeout(1500)
        signals = await page.evaluate("""() => ({
          blocked: Array.from(document.querySelectorAll('body *')).some(e => e.children.length===0 &&
            !e.closest('article,.text') && e.getBoundingClientRect().width > 0 &&
            /访问频次过高|访问异常|安全验证|完成验证|请输入验证码/.test(e.textContent)),
          login: /请先登录|登录后才能查看/.test(document.body.innerText)
        })""")
        if signals["blocked"] or signals["login"]:
            raise BrowserAccessStopped("manual_verification_required" if signals["blocked"] else "manual_login_required")

    try:
        with RunLock(args.output / "browser-tweets.lock"):
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(str(args.profile.resolve()),
                    channel="chrome", headless=True, accept_downloads=False, viewport={"width": 1280, "height": 900})
                try:
                    page = await context.new_page()
                    page.on("response", lambda response: handlers.append(asyncio.create_task(observe(response))))
                    while state["queue"]:
                        if navigated >= job["max_requests"]:
                            raise BrowserAccessStopped("navigation_budget")
                        if args.max_posts and collected >= args.max_posts:
                            raise BrowserAccessStopped("pilot_post_limit")
                        task = state["queue"][0]
                        if task["kind"] == "search":
                            if task["url"] in state["seen_pages"]:
                                state["queue"].pop(0)
                                continue
                            await open_page(page, task["url"])
                            evidence = await page.evaluate("""() => ({
                              links: Array.from(document.querySelectorAll('.from a')).map(a => a.href),
                              next: document.querySelector('a.next')?.href || null,
                              empty: document.body.innerText.includes('抱歉，未找到相关结果')
                            })""")
                            if not evidence["links"] and not evidence["empty"]:
                                raise BrowserAccessStopped("unrecognized_search_page")
                            details = {}
                            for url in evidence["links"]:
                                parsed = urlparse(url)
                                match = re.fullmatch(r"/(\d+)/([A-Za-z0-9]+)", parsed.path)
                                if parsed.hostname == "weibo.com" and match and match.group(2) not in saved_sids:
                                    details[match.group(2)] = {"kind": "post", "url": f"https://weibo.com{parsed.path}",
                                                              "sid": match.group(2)}
                            next_tasks = []
                            if evidence["next"] and safe_search_next(evidence["next"], task["url"]):
                                next_tasks.append({"kind": "search", "url": evidence["next"]})
                            state["seen_pages"].append(task["url"])
                            state["queue"] = list(details.values()) + next_tasks + state["queue"][1:]
                            state["events"].append({"kind": "search", "visible_links": len(evidence["links"]),
                                                    "new_post_targets": len(details)})
                        else:
                            sid = task["sid"]
                            if sid in saved_sids:
                                state["queue"].pop(0)
                                continue
                            known = known_bodies.get(sid)
                            known_date = known.get("created_at") if known else date_cache.get(sid)
                            if known_date and not job["start"] <= known_date < job["end"]:
                                state["queue"].pop(0)
                                write_json(checkpoint_path, state)
                                continue
                            if known:
                                item = {**known, "keyword": job["keyword"], "crawl_time": int(time.time()),
                                    "workflow_round_id": job["round_id"], "body_reused_from_saved_tweet": True}
                                with db.cursor() as cursor:
                                    WorkflowPipeline._write(cursor, ("tweet_spider_by_keyword", item["_id"], item["keyword"],
                                        item["crawl_time"], json.dumps(item, ensure_ascii=False)))
                                db.commit()
                                collected += 1
                                reused += 1
                                if item["_id"] not in state["saved_posts"]:
                                    state["saved_posts"].append(item["_id"])
                                saved_sids.add(sid)
                                state["queue"].pop(0)
                                write_json(checkpoint_path, state)
                                continue
                            captured.clear()
                            response_status.clear()
                            await open_page(page, task["url"])
                            for _ in range(50):
                                if any(e["status"] in (401, 403, 418, 429) for e in response_status):
                                    raise BrowserAccessStopped("post_page_access_limit")
                                if ("/ajax/statuses/show", sid) in captured:
                                    break
                                await asyncio.sleep(0.2)
                            raw = captured.get(("/ajax/statuses/show", sid))
                            if not isinstance(raw, dict) or raw.get("mblogid") != sid:
                                raise BrowserAccessStopped("post_body_not_loaded")
                            numeric_id = str(raw.get("mid") or raw.get("id") or "")
                            longtext = captured.get(("/ajax/statuses/longtext", sid)) or captured.get(("/ajax/statuses/longtext", numeric_id))
                            inline = raw.get("longText")
                            has_inline = isinstance(inline, dict) and bool(inline.get("content"))
                            if raw.get("isLongText") and not longtext and not has_inline:
                                expand = page.locator('main article').get_by_text(re.compile(r'^展开(?:全文)?$'))
                                if await expand.count() == 1 and await expand.is_visible():
                                    try:
                                        await expand.click(timeout=10000)
                                        await page.wait_for_timeout(1500)
                                    except PlaywrightTimeoutError:
                                        state['events'].append({'kind': 'longtext_expand_timeout', 'sid': sid})
                                    longtext = captured.get(("/ajax/statuses/longtext", sid)) or captured.get(("/ajax/statuses/longtext", numeric_id))
                            item = normalize_tweet(raw, job["keyword"], job["round_id"], longtext)
                            date_cache[sid] = item["created_at"]
                            write_json(date_cache_path, date_cache)
                            if job["start"] <= item["created_at"] < job["end"]:
                                with db.cursor() as cursor:
                                    WorkflowPipeline._write(cursor, ("tweet_spider_by_keyword", item["_id"], item["keyword"],
                                        item["crawl_time"], json.dumps(item, ensure_ascii=False)))
                                db.commit()
                                collected += 1
                                if item["_id"] not in state["saved_posts"]:
                                    state["saved_posts"].append(item["_id"])
                                if not item["long_text_complete"]:
                                    if item["_id"] not in incomplete_longtexts:
                                        incomplete_longtexts.append(item["_id"])
                                    state["events"].append({"kind": "longtext_incomplete_saved_and_skipped",
                                                            "tweet_id": item["_id"]})
                                if any(e["status"] in (401, 403, 418, 429) for e in response_status):
                                    raise BrowserAccessStopped("post_page_access_limit")
                            else:
                                state["events"].append({"kind": "outside_date_range", "tweet_id": item["_id"],
                                                        "created_at": item["created_at"]})
                            saved_sids.add(sid)
                            state["queue"].pop(0)
                        write_json(checkpoint_path, state)
                    if handlers:
                        await asyncio.gather(*handlers, return_exceptions=True)
                finally:
                    await context.close()
    except BrowserAccessStopped as exc:
        state["stop_reason"] = str(exc)
    except Exception as exc:
        state["stop_reason"] = type(exc).__name__
    finally:
        db.close()
        write_json(checkpoint_path, state)
        result = {"complete": not state["queue"] and not state["stop_reason"],
            "stop_reason": state["stop_reason"], "saved_posts_this_attempt": collected,
            "saved_posts_checkpoint": len(state["saved_posts"]), "pending_tasks": len(state["queue"]),
            "normal_page_navigations": navigated, "coverage": "normal_browser_pages",
            "reused_saved_tweet_bodies": reused,
            "incomplete_longtexts": incomplete_longtexts,
            "historical_total_complete": None}
        write_json(args.result, result)
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["complete"] else 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--max-posts", type=int, default=0, help="0 collects all queued visible posts")
    parser.add_argument("--hour", type=int, help="Pilot: first-date hour, 0..23")
    parser.add_argument("--cache", type=Path, help="Shared cache of already verified publication dates")
    args = parser.parse_args()
    if args.max_posts < 0 or (args.hour is not None and not 0 <= args.hour <= 23):
        parser.error("Invalid pilot limits")
    args.output.mkdir(parents=True, exist_ok=True)
    job = json.loads(args.job.read_text(encoding="utf-8"))
    return asyncio.run(collect(args, job))


if __name__ == "__main__":
    raise SystemExit(main())
