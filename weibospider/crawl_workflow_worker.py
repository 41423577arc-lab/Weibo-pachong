"""One isolated Scrapy reactor per durable workflow job."""
import argparse
import json
import os
from pathlib import Path

from workflow_store import load_configuration


def write_result(path, result):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def make_spiders():
    from spiders.tweet_by_keyword import TweetSpiderByKeyword
    from spiders.comment import CommentSpider
    from spiders.common import parse_tweet_info
    from scrapy import Request

    class WorkflowTweetSpider(TweetSpiderByKeyword):
        custom_settings = {"ITEM_PIPELINES": {"workflow_pipeline.WorkflowPipeline": 300}}

        def request_failed(self, failure):
            super().request_failed(failure)
            response = getattr(failure.value, "response", None)
            status = response.status if response is not None else None
            if not hasattr(self, "access_diagnostics"):
                self.access_diagnostics = []
            from access_diagnostics import response_diagnostics
            if response is not None:
                headers = {key: response.headers.get(key.encode(), b'').decode('utf-8', 'replace')
                           for key in ('Content-Type', 'Location')}
                self.access_diagnostics.append(response_diagnostics(failure.request.url, status, headers, response.text))
            else:
                self.access_diagnostics.append({"error_type": failure.type.__name__})
            partial = failure.request.meta.get("item")
            is_longtext_request = partial is not None and "/ajax/statuses/longtext" in failure.request.url
            # A longtext fetch is non-essential: skip it (mark incomplete) instead of
            # halting the whole keyword batch. Search/show access limits still stop.
            if status in (401, 403, 418, 429) and not is_longtext_request:
                self.crawler.engine.close_spider(self, reason="platform_access_limit")
            if partial:
                partial["long_text_complete"] = False
                return [partial]

        def parse_tweet(self, response):
            # Preserve every acquired in-scope body even if longtext fails later.
            from zoneinfo import ZoneInfo
            import dateutil.parser
            raw = json.loads(response.text)
            item = parse_tweet_info(raw)
            timestamp = dateutil.parser.parse(raw["created_at"])
            if timestamp.tzinfo is None:
                raise ValueError("Tweet publication time requires a timezone")
            created = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            if not self.start_time <= created < self.end_time:
                self.crawler.stats.inc_value("search/outside_date_range")
                return
            item["created_at"] = created.strftime("%Y-%m-%d %H:%M:%S")
            item["keyword"] = response.meta["keyword"]
            item["raw_api_json"] = raw
            if raw.get("isLongText"):
                item["isLongText"] = True
                item["long_text_complete"] = False
                inline = raw.get("longText")
                if isinstance(inline, dict) and isinstance(inline.get("content"), str) and inline["content"]:
                    item["content"] = inline["content"]
                    item["raw_longtext_json"] = {"data": {"longTextContent": inline["content"]}, "source": "inline_status_longText"}
                    item["long_text_complete"] = True
                    yield item
                    return
                yield Request("https://weibo.com/ajax/statuses/longtext?id=" + item["mblogid"],
                    callback=self.parse_longtext, errback=self.request_failed, meta={"item": item}, priority=20)
            else:
                item["long_text_complete"] = True
                yield item

        def parse_longtext(self, response):
            item = response.meta["item"]
            try:
                raw = json.loads(response.text)
                content = raw["data"]["longTextContent"]
                if not isinstance(content, str) or not content:
                    raise ValueError("Missing longtext")
            except (ValueError, KeyError, TypeError):
                self.crawler.stats.inc_value("workflow/longtext_parse_failures")
                yield item
                return
            item["content"] = content
            item["raw_longtext_json"] = raw
            item["long_text_complete"] = True
            yield item

    class WorkflowCommentSpider(CommentSpider):
        custom_settings = {**CommentSpider.custom_settings,
                           "ITEM_PIPELINES": {"workflow_pipeline.WorkflowPipeline": 300}}

    return WorkflowTweetSpider, WorkflowCommentSpider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    load_configuration()
    os.environ["SCRAPY_SETTINGS_MODULE"] = "settings"
    from scrapy import signals
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings
    WorkflowTweetSpider, WorkflowCommentSpider = make_spiders()

    settings = get_project_settings()
    overrides = {"CONCURRENT_REQUESTS": 1, "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": job["delay"], "RETRY_TIMES": 0, "DOWNLOAD_TIMEOUT": 30,
        "CLOSESPIDER_PAGECOUNT": job["max_requests"], "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "INFO", "LOG_FILE": str(args.result.with_suffix(".log"))}
    for key, value in overrides.items():
        settings.set(key, value, priority="cmdline")
    process = CrawlerProcess(settings)
    cls = WorkflowTweetSpider if job["kind"] == "tweet" else WorkflowCommentSpider
    crawler = process.create_crawler(cls)
    errors = []
    def parsing_failed(failure, response, spider):
        errors.append(type(failure.value).__name__)
    crawler.signals.connect(parsing_failed, signal=signals.spider_error, weak=False)
    if job["kind"] == "tweet":
        options = {"keyword": job["keyword"], "start_date": job["start"], "end_date": job["end"]}
    else:
        options = {"tweet_ids": [t["tweet_id"] for t in job["targets"]],
            "author_ids": {t["tweet_id"]: t["author_id"] for t in job["targets"]},
            "max_requests": job["max_requests"], "max_pages": 0, "max_reply_threads": -1}
    options["workflow_round_id"] = job["round_id"]
    process.crawl(crawler, **options)
    process.start()
    stats = crawler.stats.get_stats()
    spider = getattr(crawler, "spider", None)
    # Longtext-only failures are tolerated: the affected item is stored with
    # long_text_complete=False and does not invalidate the whole keyword batch.
    complete = (stats.get("finish_reason") == "finished" and not errors
        and not stats.get("database/write_failures"))
    audit = []
    if job["kind"] == "comment":
        audit = getattr(spider, "audit", [])
        complete = (complete and not getattr(spider, "access_limited", True)
            and bool(getattr(spider, "streams", {}))
            and all(v == "exhausted" for v in spider.streams.values())
            and all(spider.streams.get((t["tweet_id"], None)) == "exhausted" for t in job["targets"])
            and all(e["event"] == "page" for e in audit))
    result = {"complete": bool(complete), "stats": stats, "parse_errors": errors, "audit": audit,
              "access_diagnostics": getattr(spider, "access_diagnostics", []),
              "coverage": "visible_responses_only", "historical_total_complete": None}
    write_result(args.result, result)
    return 0 if complete else 5


if __name__ == "__main__":
    raise SystemExit(main())
