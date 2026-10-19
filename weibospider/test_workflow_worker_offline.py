"""Worker parsing and persistence contracts, no live requests."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scrapy.http import Request, TextResponse
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure
from crawl_workflow_worker import make_spiders
from workflow_pipeline import WorkflowPipeline


class WorkerTests(unittest.TestCase):
    def setUp(self):
        cls, _ = make_spiders()
        self.spider = cls(keyword="种子", start_date="2026-10-08", end_date="2026-10-09", workflow_round_id="test")
        self.spider.crawler = SimpleNamespace(stats=Mock(), engine=Mock())

    def raw(self, long=False):
        return {"mid": "123", "mblogid": "abc", "created_at": "Thu Oct 08 10:00:00 +0800 2026",
            "reposts_count": 0, "comments_count": 0, "attitudes_count": 0, "source": "web",
            "text_raw": "正文 #不要# #新词", "pic_num": 0, "isLongText": long,
            "user": {"id": 456, "avatar_hd": "https://example.test/a", "screen_name": "测试", "verified": False}}

    def response(self, raw, request=None):
        request = request or Request("https://weibo.com/ajax/statuses/show?id=abc", meta={"keyword": "种子"})
        return TextResponse(url=request.url, request=request, body=json.dumps(raw).encode(), encoding="utf-8")

    def test_body_with_rejected_topic_is_retained_with_raw_json(self):
        result = list(self.spider.parse_tweet(self.response(self.raw())))[0]
        self.assertIn("#不要#", result["content"])
        self.assertEqual(result["raw_api_json"], self.raw())
        self.assertTrue(result["long_text_complete"])

    def test_longtext_failure_saves_partial_and_stops_on_rejection(self):
        request = list(self.spider.parse_tweet(self.response(self.raw(True))))[0]
        response = TextResponse(url=request.url, status=403, request=request)
        failure = Failure(HttpError(response))
        failure.request = request
        result = self.spider.request_failed(failure)
        self.assertEqual(result[0]["_id"], "123")
        self.assertFalse(result[0]["long_text_complete"])
        self.spider.crawler.engine.close_spider.assert_called_once_with(self.spider, reason="platform_access_limit")

    def test_longtext_parse_failure_retains_body(self):
        request = list(self.spider.parse_tweet(self.response(self.raw(True))))[0]
        result = list(self.spider.parse_longtext(self.response({"data": {}}, request)))
        self.assertEqual(result[0]["content"], "正文 #不要# #新词")
        self.assertFalse(result[0]["long_text_complete"])

    def test_inline_longtext_avoids_unnecessary_extra_request(self):
        raw = self.raw(True)
        raw['longText'] = {'content': '已有完整长文 #新词'}
        result = list(self.spider.parse_tweet(self.response(raw)))
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['long_text_complete'])
        self.assertEqual(result[0]['content'], '已有完整长文 #新词')

    def test_only_upgrade_incomplete_longtext_in_persistence(self):
        cursor = Mock()
        WorkflowPipeline._write(cursor, ("tweet_spider_by_keyword", "123", "种子", 0, "{}"))
        query = cursor.execute.call_args.args[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", query)
        self.assertIn("long_text_complete", query)
        self.assertNotIn("DELETE", query)


if __name__ == "__main__":
    unittest.main()
