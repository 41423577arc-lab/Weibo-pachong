"""Extract #topic# candidates from saved tweets; never call Weibo or write MySQL.

Default source: crawl_items in the existing local MySQL database.
Offline source: --input JSON/JSONL, including logs/history-baseline.json.
"""
import argparse
import ast
from collections import Counter, defaultdict
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import unicodedata
from local_config import read_config

PROJECT = Path(__file__).resolve().parents[1]
TOPIC = re.compile(r"[#＃]([^#＃\r\n]+)[#＃]")
TOPIC_BOUNDARY = re.compile(r"[\s#＃，。！？；：、,.!?;:<>\"'“”‘’（）()【】{}…]")
TWEET_SPIDERS = {"tweet_spider_by_keyword", "tweet_spider_by_user_id", "tweet_spider_by_tweet_id"}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div"}:
            self.parts.append("\n")


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).replace("\u200b", "").split())


def extract_topics(text):
    parser = PlainText()
    parser.feed(str(text or ""))
    plain = html.unescape("".join(parser.parts))
    topics = set()
    position = 0
    while position < len(plain):
        match = re.search(r"[#＃]", plain[position:])
        if not match:
            break
        start = position + match.start() + 1
        closing = re.search(r"[#＃]", plain[start:])
        end = start + closing.start() if closing else len(plain)
        candidate = plain[start:end]
        # A space immediately before the next # means it starts a new tag.
        # Paired tags may contain internal spaces, e.g. #刘耀文up it#.
        paired = bool(closing and candidate and not candidate[-1].isspace()
                      and not re.search(r"[\r\n，。！？；：、,.!?;:<>]", candidate))
        if paired:
            topic = normalize(candidate)
            position = end + 1
        else:
            boundary = TOPIC_BOUNDARY.search(plain, start)
            end = boundary.start() if boundary else len(plain)
            topic = normalize(plain[start:end])
            position = max(start, end)
        if topic:
            topics.add(topic)
    return sorted(topics)


def configured_keywords():
    """Read literal historical configuration without importing collector dependencies."""
    tree = ast.parse((PROJECT / "weibospider/history_collect.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "KEYWORDS" for t in node.targets):
            return ast.literal_eval(node.value)
    return []


def file_records(path):
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("tweets", rows.get("items", [rows]))
    for row in rows:
        if isinstance(row, list) and len(row) == 5:
            spider, item_id, keyword, _, payload = row
            yield {"spider": spider, "item_id": str(item_id), "keyword": keyword,
                   "payload": json.loads(payload) if isinstance(payload, str) else payload}
        elif isinstance(row, dict):
            payload = row.get("payload", row)
            if isinstance(payload, str):
                payload = json.loads(payload)
            yield {"spider": row.get("spider", ""),
                   "item_id": str(row.get("item_id") or payload.get("_id") or payload.get("id") or ""),
                   "keyword": row.get("keyword") or payload.get("keyword") or "", "payload": payload}
        else:
            raise ValueError("Unsupported input row; expected a tweet object or five-column database backup")


def database_records(config_path):
    import pymysql

    config = read_config(config_path)
    def setting(key, default=None):
        return os.environ.get(key, config.get(key, default))
    db = pymysql.connect(host=setting("WEIBO_DB_HOST", "127.0.0.1"),
                         port=int(setting("WEIBO_DB_PORT", 3306)), user=setting("WEIBO_DB_USER"),
                         password=setting("WEIBO_DB_PASSWORD", ""),
                         database=setting("WEIBO_DB_NAME", "weibo_data"), charset="utf8mb4",
                         connect_timeout=10, read_timeout=30, cursorclass=pymysql.cursors.SSCursor)
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT spider,item_id,keyword,payload FROM crawl_items ORDER BY spider,item_id,keyword")
            for spider, item_id, keyword, payload in cursor:
                yield {"spider": spider, "item_id": str(item_id), "keyword": keyword,
                       "payload": json.loads(payload) if isinstance(payload, str) else payload}
    finally:
        db.close()


def analyze(records, known_keywords, source):
    counts = defaultdict(Counter)
    tweets = {}
    skipped = Counter()
    rows_read = 0
    for record in records:
        rows_read += 1
        spider = record["spider"]
        payload = record["payload"]
        if record["keyword"]:
            counts[spider or "file"][record["keyword"]] += 1
        if (spider and spider not in TWEET_SPIDERS) or (not spider and
                ("tweet_id" in payload or "comment_level" in payload or "root_comment_id" in payload)):
            skipped["not_tweet_body"] += 1
            continue
        content = payload.get("content") or payload.get("text_raw") or payload.get("text") or ""
        if not isinstance(content, str) or not content.strip():
            skipped["missing_body"] += 1
            continue
        tweet_id = record["item_id"]
        if not tweet_id:
            skipped["missing_tweet_id"] += 1
            continue
        entry = tweets.setdefault(tweet_id, {"tweet_id": tweet_id, "url": payload.get("url", ""),
            "created_at": payload.get("created_at", ""), "search_keywords": set(), "topics": set(),
            "content_variants": set(), "possibly_incomplete_longtext": False})
        entry["content_variants"].add(content)
        entry["topics"].update(extract_topics(content))
        if record["keyword"]:
            entry["search_keywords"].add(record["keyword"])
        if payload.get("long_text_complete") is False or "展开全文" in content:
            entry["possibly_incomplete_longtext"] = True
    actual_keywords = {k for counter in counts.values() for k in counter}
    known = {normalize(k).strip("#＃").strip() for k in set(known_keywords) | actual_keywords}
    topic_ids = defaultdict(set)
    for tweet_id, entry in tweets.items():
        for topic in entry["topics"]:
            topic_ids[topic].add(tweet_id)
        for field in ("search_keywords", "topics", "content_variants"):
            entry[field] = sorted(entry[field])
    topics = [{"keyword": topic, "hashtag": f"#{topic}#", "tweet_count": len(ids),
               "is_new": topic not in known, "tweet_ids": sorted(ids)} for topic, ids in topic_ids.items()]
    topics.sort(key=lambda row: (-row["tweet_count"], row["keyword"]))
    tagged_ids = {mid for ids in topic_ids.values() for mid in ids}
    return {"source": source, "rows_read": rows_read, "keyword_record_counts_by_spider":
            {k: dict(v) for k, v in sorted(counts.items())}, "known_keywords": sorted(known),
            "unique_tweets_with_body": len(tweets), "tweets_with_topics": len(tagged_ids),
            "skipped_rows": dict(skipped),
            "topic_count": len(topics), "new_topic_count": sum(t["is_new"] for t in topics),
            "topics": topics, "tweets": sorted(tweets.values(), key=lambda t: t["tweet_id"])}


def write_outputs(result, output, min_tweets):
    output.mkdir(parents=True, exist_ok=True)
    (output / "topic_keywords.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    candidates = [t for t in result["topics"] if t["is_new"] and t["tweet_count"] >= min_tweets]
    keyword_file = output / "new_keywords.txt"
    marked = set()
    if keyword_file.exists():
        marked = {normalize(line.lstrip().lstrip("#＃")) for line in
                  keyword_file.read_text(encoding="utf-8-sig").splitlines()
                  if line.lstrip().startswith(("#", "＃"))}
    keyword_file.write_text("".join(("#" if t["keyword"] in marked else "") + t["keyword"] + "\n"
                                   for t in candidates), encoding="utf-8")
    lines = ["# 已采集微博的话题关键词", "", f"数据来源：{result['source']}", "",
             f"有正文的不同微博：{result['unique_tweets_with_body']}；含话题微博：{result['tweets_with_topics']}；"
             f"不同话题：{result['topic_count']}；新候选：{result['new_topic_count']}。", "",
             "新候选按话题名与历史检索关键词/配置关键词精确比较；不表示此前没有见过，也不保证与研究目标相关。",
             "次数按不同微博 ID 计数，同一微博多次命中或同一话题重复出现只计一次。仅分析已保存的正文，不含评论、搜索链接索引或未保存的转发原文。", "",
             "## 实际记录中的检索关键词", ""]
    for spider, keywords in result["keyword_record_counts_by_spider"].items():
        for keyword, count in sorted(keywords.items()):
            lines.append(f"- {spider}：{keyword}（{count} 条记录）")
    lines += ["", "## 话题与微博出处", ""]
    by_id = {t["tweet_id"]: t for t in result["tweets"]}
    for topic in result["topics"]:
        links = [f"[{mid}]({by_id[mid]['url']})" if by_id[mid]["url"] else mid for mid in topic["tweet_ids"]]
        lines.append(f"- {topic['hashtag']}：{topic['tweet_count']} 条；{'新候选' if topic['is_new'] else '已有关键词'}；" + "、".join(links))
    lines += ["", f"new_keywords.txt 仅导出新候选且至少出现在 {min_tweets} 条不同微博中。",
              "完整正文及每条微博的话题映射见 topic_keywords.json。候选不会自动触发新一轮爬取。", ""]
    (output / "topic_keywords.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Offline JSON/JSONL instead of MySQL")
    parser.add_argument("--config", type=Path, help="Optional private configuration file")
    parser.add_argument("--output", type=Path, default=PROJECT / "output/topic-keywords")
    parser.add_argument("--known-keyword", action="append", default=[], help="Additional existing keyword; repeatable")
    parser.add_argument("--min-tweets", type=int, default=1)
    args = parser.parse_args()
    if args.min_tweets < 1:
        parser.error("--min-tweets must be at least 1")
    records = file_records(args.input) if args.input else database_records(args.config)
    source = str(args.input.resolve()) if args.input else "MySQL crawl_items (read-only SELECT)"
    result = analyze(records, configured_keywords() + args.known_keyword, source)
    write_outputs(result, args.output, args.min_tweets)
    print(json.dumps({k: result[k] for k in ("source", "rows_read", "keyword_record_counts_by_spider",
          "unique_tweets_with_body", "tweets_with_topics", "topic_count", "new_topic_count", "skipped_rows")}, ensure_ascii=False))
    print("Output: " + str(args.output.resolve()))


if __name__ == "__main__":
    main()
