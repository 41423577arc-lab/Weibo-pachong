"""Synthetic fixtures only: no platform requests, credentials, or database writes."""
import json
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from scrapy.exceptions import CloseSpider
from scrapy.http import Request, TextResponse
from spiders.comment import CommentSpider
from comment_run import select_targets, run_outcome


MID = '5351874389873778'
AUTHOR = '7966996383'


class CommentOfflineTests(unittest.TestCase):
    def setUp(self):
        self.closed = []
        self.spider = CommentSpider(tweet_ids=[MID], author_ids={MID: AUTHOR})
        self.spider.crawler = SimpleNamespace(
            stats=SimpleNamespace(set_value=lambda *a: None),
            engine=SimpleNamespace(close_spider=lambda *a, **k: self.closed.append(k)))

    def comment(self, cid='123456789012345678', rootid=0, reply=None):
        result = {'id': cid, 'rootid': rootid,
                  'created_at': 'Thu Oct 08 10:00:00 +0800 2026',
                  'text_raw': '离线测试😀', 'like_counts': 0,
                  'user': {'id': '111', 'screen_name': '测试用户'}}
        if reply:
            result['reply_comment'] = {'id': reply}
        return result

    def response(self, request, data):
        return TextResponse(url=request.url, request=request,
                            body=json.dumps(data).encode(), encoding='utf-8')

    def test_initial_request_exactly_matches_browser_parameters(self):
        request = list(self.spider.start_requests())[0]
        self.assertEqual(urlparse(request.url).path, '/ajax/statuses/buildComments')
        self.assertEqual(parse_qs(urlparse(request.url).query), {
            'is_reload': ['1'], 'id': [MID], 'is_show_bulletin': ['3'],
            'is_mix': ['0'], 'count': ['10'], 'uid': [AUTHOR],
            'fetch_level': ['0'], 'locale': ['zh-CN']})

    def test_short_mid_conversion_and_missing_author_rejected(self):
        spider = CommentSpider(tweet_ids=['RlLumFqCu'], author_ids={MID: AUTHOR})
        self.assertEqual(list(spider.start_requests())[0].meta['tweet_id'], MID)
        with self.assertRaises(ValueError):
            CommentSpider(tweet_ids=[MID]).make_request(MID)

    def test_zero_cursor_does_not_page_despite_total_23(self):
        request = self.spider.make_request(MID)
        results = list(self.spider.parse(self.response(request, {
            'ok': 1, 'data': [self.comment()], 'total_number': 23,
            'max_id': 0, 'rootComment': [], 'trendsText': '已加载全部评论'})))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['_id'], '123456789012345678')
        self.assertEqual(results[0]['comment_level'], 1)
        self.assertIsNone(results[0]['parent_comment_id'])

    def test_rootid_zero_or_self_is_first_level(self):
        for rootid in (0, '0', None, '123456789012345678'):
            item = self.spider.parse_comment(self.comment(rootid=rootid), MID)
            self.assertEqual(item['comment_level'], 1)

    def test_rootid_and_direct_parent_are_distinct(self):
        item = self.spider.parse_comment(self.comment(rootid='222', reply='333'), MID, '222')
        self.assertEqual(item['root_comment_id'], '222')
        self.assertEqual(item['parent_comment_id'], '333')
        self.assertEqual(item['api_rootid'], '222')
        self.assertEqual(item['parent_relation_source'], 'reply_comment')
        self.assertEqual(item['tweet_id'], MID)
        self.assertEqual(item['comment_user']['_id'], '111')

    def test_fallback_parent_is_labeled_and_conflicting_root_rejected(self):
        item = self.spider.parse_comment(self.comment(rootid='222'), MID)
        self.assertEqual(item['comment_level'], 2)
        self.assertEqual(item['parent_relation_source'], 'thread_root_fallback')
        with self.assertRaises(ValueError):
            self.spider.parse_comment(self.comment(rootid='222'), MID, '444')

    def test_inline_reply_dedup_and_reply_cursor(self):
        root = self.comment(cid='222')
        root['comments'] = [self.comment(cid='333', rootid='222')]
        request = self.spider.make_request(MID)
        results = list(self.spider.parse(self.response(request, {'ok': 1, 'data': [root], 'max_id': 0})))
        self.assertEqual(len([v for v in results if isinstance(v, dict)]), 2)
        reply_request = next(v for v in results if isinstance(v, Request))
        params = parse_qs(urlparse(reply_request.url).query)
        self.assertEqual(params['uid'], [AUTHOR])
        self.assertEqual(params['fetch_level'], ['1'])
        results = list(self.spider.parse(self.response(reply_request, {
            'ok': 1, 'data': root['comments'], 'max_id': '999', 'max_id_type': 1})))
        self.assertEqual(len(results), 1)  # Already-seen inline reply is not emitted twice.
        self.assertEqual(parse_qs(urlparse(results[0].url).query)['max_id'], ['999'])
        self.assertEqual(results[0].meta['root_comment_id'], '222')

    def test_missing_cursor_is_audited_and_error_response_stops(self):
        request = self.spider.make_request(MID)
        list(self.spider.parse(self.response(request, {'ok': 1, 'data': []})))
        self.assertEqual(self.spider.audit[-1]['event'], 'missing_pagination_cursor')
        with self.assertRaises(CloseSpider):
            list(self.spider.parse(self.response(request, {'ok': 0, 'data': []})))

    def test_403_stops_scheduling_and_processing(self):
        request = self.spider.make_request(MID)
        response = TextResponse(url=request.url, request=request, status=403)
        failure = SimpleNamespace(value=SimpleNamespace(response=response), request=request,
                                  type=SimpleNamespace(__name__='HttpError'))
        self.spider.request_failed(failure)
        self.assertEqual(self.closed[0]['reason'], 'platform_access_limit')
        self.assertIsNone(self.spider.make_request(MID))
        self.assertEqual(list(self.spider.parse(response)), [])

    def test_unlimited_pages_and_cursor_cycle_end_as_incomplete(self):
        self.spider.max_pages = 0
        request = self.spider.make_request(MID)
        for cid, cursor in [('1', '100'), ('2', '200')]:
            results = list(self.spider.parse(self.response(request, {
                'data': [self.comment(cid=cid)], 'max_id': cursor})))
            request = next(v for v in results if isinstance(v, Request))
        results = list(self.spider.parse(self.response(request, {
            'data': [self.comment(cid='3')], 'max_id': '100'})))
        self.assertFalse(any(isinstance(v, Request) for v in results))
        self.assertEqual(self.spider.streams[(MID, None)], 'repeated_cursor')

    def test_all_reply_threads_and_malformed_item_do_not_drop_good_rows(self):
        self.spider.max_reply_threads = -1
        roots = [self.comment(cid=str(n)) for n in (222, 333, 444)]
        for root in roots:
            root['total_number'] = 1
        request = self.spider.make_request(MID)
        results = list(self.spider.parse(self.response(request, {
            'data': [{}, *roots], 'max_id': 0})))
        self.assertEqual(len([v for v in results if isinstance(v, Request)]), 3)
        self.assertEqual(len([v for v in results if isinstance(v, dict)]), 3)
        self.assertTrue(any(e['event'] == 'invalid_comment' for e in self.spider.audit))

    def test_zero_comments_http_failure_and_unpersisted_items_are_not_success(self):
        request = self.spider.make_request(MID)
        list(self.spider.parse(self.response(request, {'data': [], 'max_id': 0})))
        result = run_outcome(self.spider, {'finish_reason': 'finished'}, 0)
        self.assertEqual(result['outcome'], 'empty_visible_result')
        self.assertFalse(result['collection_verified'])
        self.spider.seen_items.add('111')
        self.assertFalse(run_outcome(self.spider, {'finish_reason': 'finished'}, 0)['collection_verified'])
        self.assertFalse(run_outcome(self.spider, {'finish_reason': 'platform_access_limit'}, 1)['collection_verified'])

    def test_budget_and_skipped_threads_report_incomplete(self):
        self.spider.max_requests = 1
        self.spider.max_reply_threads = 0
        root = self.comment(cid='222')
        root['total_number'] = 2
        request = self.spider.make_request(MID)
        list(self.spider.parse(self.response(request, {'data': [root], 'max_id': '100'})))
        self.assertEqual(self.spider.streams[(MID, None)], 'request_budget_limit')
        self.assertEqual(self.spider.streams[(MID, '222')], 'reply_thread_limit')
        self.assertFalse(run_outcome(self.spider, {'finish_reason': 'finished'}, 1)['collection_verified'])

    def test_recent_targets_are_deduplicated_across_keywords_and_not_hardcoded(self):
        def payload(count=1):
            return json.dumps({'comments_count': count, 'user': {'_id': AUTHOR}})
        rows = [('tweet_spider_by_keyword', MID, 10, payload()),
                ('tweet_spider_by_keyword', MID, 20, payload()),
                ('tweet_spider_by_user_id', '222', 30, payload()),
                ('history_search_hit', '333', 40, payload()),
                ('tweet_spider_by_keyword', '444', 50, payload(0))]
        targets = select_targets(rows, since=15)
        self.assertEqual([v['tweet_id'] for v in targets], ['222', MID])
        self.assertEqual(targets[1]['crawl_time'], 20)

    def test_repeated_root_on_next_page_does_not_schedule_reply_thread_again(self):
        self.spider.max_pages = 0
        root = self.comment(cid='222')
        root['total_number'] = 1
        request = self.spider.make_request(MID)
        results = list(self.spider.parse(self.response(request, {'data': [root], 'max_id': '100'})))
        next_page = next(r for r in results if isinstance(r, Request) and not r.meta['root_comment_id'])
        list(self.spider.parse(self.response(next_page, {'data': [root], 'max_id': 0})))
        self.assertEqual(self.spider.reply_threads[MID], 1)
        self.assertEqual(self.spider.streams[(MID, '222')], 'pending')
        self.assertFalse(any(e['event'] == 'repeated_cursor' for e in self.spider.audit))

    def test_success_requires_saved_comments_and_exhausted_streams(self):
        request = self.spider.make_request(MID)
        list(self.spider.parse(self.response(request, {'data': [self.comment()], 'max_id': 0})))
        self.assertTrue(run_outcome(self.spider, {'finish_reason': 'finished'}, 1)['collection_verified'])
        self.assertFalse(run_outcome(self.spider, {'finish_reason': 'finished'}, 1,
                                     existing_rows_unchanged=False)['collection_verified'])


if __name__ == '__main__':
    unittest.main()
