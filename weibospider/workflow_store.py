"""Database access, saved-comment synchronization and workflow metrics."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time

from extract_topic_keywords import PROJECT, TWEET_SPIDERS
from browser_comment_store import BrowserCommentStore
from local_config import load_environment

COMMENT_SPIDERS = {"comment", "browser_comment"}


def load_configuration():
    load_environment()


def connect():
    import pymysql
    load_configuration()
    return pymysql.connect(host=os.environ.get("WEIBO_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEIBO_DB_PORT", 3306)), user=os.environ.get("WEIBO_DB_USER"),
        password=os.environ.get("WEIBO_DB_PASSWORD", ""),
        database=os.environ.get("WEIBO_DB_NAME", "weibo_data"), charset="utf8mb4",
        connect_timeout=10, read_timeout=30, write_timeout=30)


def read_records(db):
    with db.cursor() as cursor:
        cursor.execute("SELECT spider,item_id,keyword,payload FROM crawl_items ORDER BY spider,item_id,keyword")
        return [{"spider": s, "item_id": str(mid), "keyword": k,
                 "payload": json.loads(p) if isinstance(p, str) else p} for s, mid, k, p in cursor.fetchall()]


def totals(records):
    tweet_ids, comment_ids, parents = set(), set(), set()
    levels, comment_sources = Counter(), Counter()
    tweet_rows = index_rows = 0
    for row in records:
        data, spider = row["payload"], row["spider"]
        if spider in TWEET_SPIDERS and any(data.get(f) for f in ("content", "text_raw", "text")):
            tweet_ids.add(row["item_id"])
            tweet_rows += 1
        elif spider in COMMENT_SPIDERS:
            identity = (spider, row["item_id"])
            if identity not in comment_ids:
                comment_ids.add(identity)
                levels[str(data.get("comment_level", "unknown"))] += 1
                comment_sources[spider] += 1
                parents.add(str(data.get("tweet_id") or ""))
        elif spider == "history_search_hit":
            index_rows += 1
    return {"unique_tweets": len(tweet_ids), "tweet_keyword_rows": tweet_rows,
            "comments_including_replies": len(comment_ids), "comment_levels": dict(levels),
            "comment_sources": dict(comment_sources), "tweet_plus_comment_total": len(tweet_ids) + len(comment_ids),
            "search_index_rows_excluded_from_total": index_rows,
            "comment_parent_ids_without_stored_body": sorted(parents - tweet_ids),
            "comment_identity_note": "API IDs and DOM fingerprints counted separately; cross-source equality is unverified"}


def comment_targets(records, keywords, start, end):
    """All in-scope saved tweet bodies, including counts recorded as zero."""
    targets = {}
    for row in records:
        data = row["payload"]
        if row["spider"] not in TWEET_SPIDERS or row["keyword"] not in keywords:
            continue
        if not start <= data.get("created_at", "") < end:
            continue
        mid = row["item_id"]
        author = str(data.get("user", {}).get("_id") or "")
        if not mid.isdigit() or not author.isdigit():
            raise ValueError(f"Stored tweet {mid} lacks numeric tweet/author identity; repair before collecting comments")
        targets[mid] = {"tweet_id": mid, "author_id": author, "url": data.get("url"),
                        "comments_count_at_tweet_crawl": data.get("comments_count"),
                        "published": data.get("created_at")}
    return sorted(targets.values(), key=lambda t: t["tweet_id"])


def browser_identity(mid, record, root=""):
    values = [str(mid), record.get("comment_level"), root,
              record.get("user_id"), record.get("created_at_display"), record.get("content")]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def saved_browser_comments(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    targets = data if isinstance(data, list) else data.get("targets", [])
    for target in targets:
        mid = str(target.get("tweet_id") or "")
        for record in target.get("comments", []):
            if not mid.isdigit() or (record.get("tweet_id") and str(record["tweet_id"]) != mid):
                raise ValueError("Invalid or conflicting parent tweet_id in saved browser comments")
            root = record.get("root_record_key") or ""
            key = record.get("record_key") or browser_identity(mid, record, root)
            payload = {**record, "_id": key, "tweet_id": mid, "record_key": key,
                       "root_record_key": root or None, "source": "browser_visible_dom",
                       "identity_source": "dom_fingerprint", "platform_comment_id_available": False,
                       "source_file": str(path.resolve())}
            yield payload


def import_browser_comments(db, paths):
    """Atomic, idempotent append of all saved records; no keyword/content filtering."""
    inserted = observed = 0
    try:
        with db.cursor() as cursor:
            for path in paths:
                for payload in saved_browser_comments(path):
                    cursor.execute("INSERT IGNORE INTO crawl_items (spider,item_id,keyword,crawl_time,payload) "
                        "VALUES (%s,%s,%s,%s,%s)", ("browser_comment", payload["_id"], "", int(time.time()),
                                                   json.dumps(payload, ensure_ascii=False)))
                    inserted += cursor.rowcount
                    observed += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"files": len(paths), "observed_records": observed, "new_comment_rows": inserted}


def default_comment_files():
    paths = sorted((PROJECT / "output").rglob("comments.json"))
    evidence = PROJECT / "logs/browser-visible-comments-20261012.json"
    if evidence.exists():
        paths.append(evidence)
    return paths


class MirroredBrowserCommentStore(BrowserCommentStore):
    """Keep crash-resumable SQLite and mirror each capture to the main MySQL DB."""
    def __init__(self, path):
        super().__init__(path)
        self.source_sqlite = str(Path(path).resolve())
        try:
            self.mysql = connect()
        except Exception:
            super().close()
            raise

    def mirror(self, mid):
        try:
            with self.mysql.cursor() as cursor:
                for record in self.records(mid):
                    payload = {**record, "_id": record["record_key"], "source_sqlite": self.source_sqlite}
                    cursor.execute("INSERT IGNORE INTO crawl_items (spider,item_id,keyword,crawl_time,payload) "
                        "VALUES (%s,%s,%s,%s,%s)", ("browser_comment", payload["_id"], "", int(time.time()),
                                                   json.dumps(payload, ensure_ascii=False)))
            self.mysql.commit()
        except Exception:
            self.mysql.rollback()
            raise

    def register(self, targets, refresh=False):
        super().register(targets, refresh)
        for target in targets:
            self.mirror(target["tweet_id"])

    def save(self, mid, records, root_key=""):
        added = super().save(mid, records, root_key)
        self.mirror(mid)
        return added

    def close(self):
        try:
            self.mysql.close()
        finally:
            super().close()
