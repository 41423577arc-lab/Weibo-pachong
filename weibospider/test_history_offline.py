"""Offline checks for history scope, checkpoints, request refusal, and data safety."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from history_collect import Collector, fresh_state, normalized_tweet, parse_search, search_url, write_json


class HistoryTests(unittest.TestCase):
    def test_all_nine_batches_and_date_bounds(self):
        state = fresh_state()
        self.assertEqual(len(state['queue']), 9)
        self.assertEqual(len(state['batch_status']), 9)
        last = next(t for t in state['queue'] if t['date'] == '2026-10-02')
        self.assertEqual(parse_qs(urlparse(search_url(last)).query)['timescope'],
                         ['custom:2026-10-02-00:2026-10-03-00'])

    def test_search_links_and_disabled_pagination(self):
        result = parse_search('<div class="from"><a href="//weibo.com/123/RlLumFqCu">time</a></div>'
                              '<a class="next" href="javascript:void(0);">next</a>', 'https://s.weibo.com/weibo')
        self.assertEqual(result['links'][0]['tweet_id'], '5351874389873778')
        self.assertIsNone(result['next_url'])
        self.assertTrue(parse_search('访问频次过高', 'https://s.weibo.com/weibo')['blocked'])

    def test_raw_json_and_timezone(self):
        raw = {'mid': '123', 'mblogid': 'abc', 'created_at': 'Tue Sep 29 16:30:00 +0000 2026',
               'reposts_count': 0, 'comments_count': 0, 'attitudes_count': 0, 'source': 'web',
               'text_raw': '正文😀', 'pic_num': 0,
               'user': {'id': 456, 'avatar_hd': 'https://example.test/a', 'screen_name': 'name', 'verified': False}}
        item = normalized_tweet(raw, '第五人格')
        self.assertEqual(item['created_at'], '2026-09-30 00:30:00')
        self.assertEqual(item['raw_api_json'], raw)

    def collector(self):
        c = object.__new__(Collector)
        c.state = fresh_state()
        c.args = SimpleNamespace(max_requests=2000, max_seconds=7200, delay=5)
        c.prior_spent = 0
        c.started = time.monotonic()
        c.last_request = None
        c.session = Mock()
        c.save = Mock()
        return c

    def test_403_blocks_route_without_second_request(self):
        c = self.collector()
        c.session.get.return_value = SimpleNamespace(status_code=403)
        task = {'kind': 'detail', 'tweet_id': '123', 'keyword': '第五人格', 'date': '2026-09-30'}
        self.assertEqual(c.request(task, 'https://weibo.com/ajax/statuses/show?id=abc')['status'], 403)
        self.assertIsNone(c.request(task, 'https://weibo.com/ajax/statuses/show?id=def'))
        self.assertEqual(c.session.get.call_count, 1)
        self.assertEqual(c.state['requests'], 1)
        self.assertEqual(len(c.state['deferred_tasks']), 2)

    def test_budget_and_uncertain_inflight_never_send(self):
        c = self.collector()
        c.state['requests'] = 2000
        with self.assertRaises(TimeoutError):
            c.request({'kind': 'search'}, 'https://s.weibo.com/weibo')
        c.state['requests'] = 0
        c.state['inflight'] = {'url': 'https://s.weibo.com/weibo'}
        with self.assertRaises(RuntimeError):
            c.request({'kind': 'search'}, 'https://s.weibo.com/weibo')
        c.session.get.assert_not_called()

    def test_cached_response_never_downloaded_again(self):
        c = self.collector()
        url = 'https://s.weibo.com/weibo'
        c.state['request_results'][url] = {'links': [], 'empty': True}
        self.assertTrue(c.request({'kind': 'search'}, url)['empty'])
        c.session.get.assert_not_called()

    def test_atomic_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            write_json(path, fresh_state())
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['requests'], 0)
            self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_insert_does_not_update_existing_rows(self):
        c = self.collector()
        cur = Mock()
        cur.rowcount = 0
        context = Mock()
        context.__enter__ = Mock(return_value=cur)
        context.__exit__ = Mock(return_value=False)
        c.db = Mock()
        c.db.cursor.return_value = context
        item = {'_id': '1', 'keyword': '第五人格', 'crawl_time': 123}
        self.assertEqual(c.insert(item), 0)
        sql, values = cur.execute.call_args.args
        self.assertIn('INSERT IGNORE', sql)
        self.assertNotIn('UPDATE', sql)
        self.assertEqual(values[1], '1')


if __name__ == '__main__':
    unittest.main()
