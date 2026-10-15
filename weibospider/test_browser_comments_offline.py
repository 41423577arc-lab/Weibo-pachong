"""Synthetic browser flows: no platform requests or credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from browser_comment_collector import BrowserCommentCollector, BrowserLimits
from browser_comment_store import BrowserCommentStore, record_key, validate_targets
from run_browser_comments import PlaywrightDriver, RunLock
from browser_comment_collector import CollectionStopped


TARGET = {'tweet_id': '123', 'url': 'https://weibo.com/456/Test123'}


def row(user='1', content='hello', level=1, replies=0):
    return {'comment_id': None, 'comment_level': level, 'user_id': user,
            'nick_name': 'synthetic', 'created_at_display': '26-10-12 12:00',
            'content': content, 'reply_count_display': replies, 'content_truncated': False}


class FakeDriver:
    def __init__(self, screens, replies=None, end=True, failure=None):
        self.screens, self.reply_screens = screens, replies or [[]]
        self.end, self.failure = end, failure
        self.index, self.reply_index, self.layer = 0, 0, False
        self.opens, self.reply_opens = 0, 0

    async def open(self, url):
        self.opens += 1
        if self.failure:
            raise self.failure

    async def snapshot(self):
        return {'comments': self.screens[self.index],
                'replies': self.reply_screens[self.reply_index] if self.layer else [],
                'roots_end_visible': self.end and self.index == len(self.screens) - 1,
                'replies_end_visible': self.layer and self.reply_index == len(self.reply_screens) - 1}

    async def open_replies(self, root):
        self.layer = True
        self.reply_opens += 1
        return True

    async def close_replies(self):
        self.layer = False

    async def scroll(self, stream):
        if stream == 'roots':
            self.index = min(self.index + 1, len(self.screens) - 1)
        else:
            self.reply_index = min(self.reply_index + 1, len(self.reply_screens) - 1)


class BrowserFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = BrowserCommentStore(self.path / 'comments.sqlite3')
        self.store.register([TARGET])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def collector(self, driver, passes=20, idle=3, threads=10):
        return BrowserCommentCollector(driver, self.store, BrowserLimits(passes, idle, 0, threads),
                                       self.path / 'comments.json')

    async def test_virtualized_pages_and_complete_reply_pagination(self):
        root = row(replies=2)
        driver = FakeDriver([[root], [row('2', 'next screen')]],
                            [[row('3', 'first reply', 2)], [row('4', 'last reply', 2)]])
        await self.collector(driver).run([TARGET])
        self.assertEqual(len(self.store.records('123')), 4)
        self.assertEqual(self.store.target_status('123'), 'visible_exhausted')
        self.assertEqual(driver.reply_opens, 1)
        children = self.store.records('123', 2)
        self.assertEqual({r['root_record_key'] for r in children}, {record_key('123', root)})
        self.assertTrue(all(r['comment_id'] is None for r in children))

    async def test_resume_replays_many_already_saved_screens_without_stalling(self):
        screens = [[row(str(i), str(i))] for i in range(8)]
        for screen in screens[:7]:
            self.store.save('123', screen)
        await self.collector(FakeDriver(screens), idle=2).run([TARGET])
        self.assertEqual(self.store.target_status('123'), 'visible_exhausted')
        self.assertEqual(len(self.store.records('123')), 8)

    async def test_finished_target_is_skipped_and_refresh_revisits(self):
        await self.collector(FakeDriver([[row()]])).run([TARGET])
        second = FakeDriver([[row()]])
        await self.collector(second).run([TARGET])
        self.assertEqual(second.opens, 0)
        self.store.register([TARGET], refresh=True)
        await self.collector(second).run([TARGET])
        self.assertEqual(second.opens, 1)
        self.assertEqual(len(self.store.records('123')), 1)

    async def test_stall_and_budget_do_not_claim_complete(self):
        await self.collector(FakeDriver([[row()]], end=False), passes=2, idle=10).run([TARGET])
        self.assertEqual(self.store.target_status('123'), 'incomplete')
        self.assertEqual(self.store.stream_status('123'), 'incomplete')
        await self.collector(FakeDriver([[row()]], end=False), passes=10, idle=2).run([TARGET])
        output = self.store.export(self.path / 'out.json')
        self.assertEqual(output['targets'][0]['reason'], 'stalled_without_end_marker')

    async def test_reply_limit_and_truncation_are_not_complete(self):
        root = row(replies=1)
        await self.collector(FakeDriver([[root]]), threads=0).run([TARGET])
        self.assertEqual(self.store.target_status('123'), 'incomplete')
        self.store.register([TARGET], refresh=True)
        root = {**row(), 'content_truncated': True}
        await self.collector(FakeDriver([[root]])).run([TARGET])
        self.assertEqual(self.store.target_status('123'), 'incomplete')

    async def test_verification_stops_batch_before_next_target(self):
        class VerificationDriver(FakeDriver):
            async def snapshot(self):
                return {**await super().snapshot(), 'verification_visible': True}
        second = {'tweet_id': '789', 'url': 'https://weibo.com/456/Test789'}
        self.store.register([second])
        driver = VerificationDriver([[row()]])
        await self.collector(driver).run([TARGET, second])
        self.assertEqual(driver.opens, 1)
        self.assertEqual(self.store.target_status('123'), 'needs_attention')
        self.assertEqual(self.store.target_status('789'), 'pending')

    async def test_partial_records_survive_exception_and_reopen(self):
        class BrokenDriver(FakeDriver):
            async def scroll(self, stream):
                raise TimeoutError('synthetic transport failure')
        await self.collector(BrokenDriver([[row()]], end=False)).run([TARGET])
        self.store.close()
        self.store = BrowserCommentStore(self.path / 'comments.sqlite3')
        self.assertEqual(len(self.store.records('123')), 1)
        self.assertEqual(self.store.target_status('123'), 'incomplete')
        await self.collector(FakeDriver([[row()], [row('2', 'new')]])).run([TARGET])
        self.assertEqual(len(self.store.records('123')), 2)

    async def test_late_render_before_end_marker_is_saved(self):
        class LateDriver(FakeDriver):
            async def snapshot(self):
                result = await super().snapshot()
                self.index = min(self.index + 1, len(self.screens) - 1)
                return result
        await self.collector(LateDriver([[row()], [row(), row('2', 'late')]])).run([TARGET])
        self.assertEqual(len(self.store.records('123')), 2)


class StoreTests(unittest.TestCase):
    def test_fingerprint_ignores_likes_and_target_validation_rejects_external_urls(self):
        first = row()
        self.assertEqual(record_key('123', first), record_key('123', {**first, 'like_count_display': '99'}))
        self.assertNotEqual(record_key('123', first), record_key('456', first))
        self.assertNotEqual(record_key('123', row(level=2), 'root1'), record_key('123', row(level=2), 'root2'))
        self.assertEqual(len(validate_targets([TARGET, TARGET])), 1)
        for url in ('https://example.com/456/Test123', 'https://weibo.com/456/Test123?token=x',
                    'https://weibo.com@evil.test/456/Test123'):
            with self.assertRaises(ValueError):
                validate_targets([{**TARGET, 'url': url}])

    def test_export_is_unicode_and_records_survive_repeated_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            store = BrowserCommentStore(Path(folder) / 'db.sqlite3')
            try:
                store.register([TARGET])
                record = row(content='中文😀[打call]')
                self.assertEqual(store.save('123', [record]), 1)
                self.assertEqual(store.save('123', [record]), 0)
                output = Path(folder) / 'data.json'
                store.export(output)
                data = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(data['targets'][0]['comments'][0]['content'], record['content'])
                self.assertIsNone(data['targets'][0]['historical_total_complete'])
            finally:
                store.close()

    def test_only_one_run_can_hold_an_output_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'collector.lock'
            with RunLock(path):
                with self.assertRaises(RuntimeError):
                    with RunLock(path):
                        pass
            with RunLock(path):
                pass


class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_empty_comment_marker_is_ready(self):
        page = SimpleNamespace(url=TARGET['url'], evaluate=AsyncMock(return_value={
            'comments': [], 'all_loaded': False, 'no_comments_visible': False, 'no_comments_rendered': True}),
            goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        driver = PlaywrightDriver(page)
        await driver.open(TARGET['url'])

    async def test_browser_network_code_is_preserved_without_request_details(self):
        page = SimpleNamespace(url=TARGET['url'],
            goto=AsyncMock(side_effect=RuntimeError('net::ERR_CONNECTION_CLOSED private=never-log-this')))
        driver = PlaywrightDriver(page)
        with self.assertRaises(CollectionStopped) as raised:
            await driver.open(TARGET['url'])
        self.assertEqual(str(raised.exception), 'browser_navigation_ERR_CONNECTION_CLOSED')
        page.url = 'chrome-error://chromewebdata/'
        with self.assertRaises(CollectionStopped) as raised:
            await driver.snapshot()
        self.assertEqual(str(raised.exception), 'browser_error_page')

    async def test_redirect_and_navigation_rejection_stop(self):
        page = SimpleNamespace(url=TARGET['url'], evaluate=AsyncMock(return_value={}),
                               goto=AsyncMock(return_value=SimpleNamespace(status=403)))
        driver = PlaywrightDriver(page)
        with self.assertRaises(CollectionStopped):
            await driver.open(TARGET['url'])
        page.url = 'https://weibo.com/newlogin'
        with self.assertRaises(CollectionStopped):
            await driver.snapshot()
        page.url = 'https://passport.weibo.com/login'
        with self.assertRaises(CollectionStopped):
            await driver.snapshot()

    async def test_empty_rendered_result_is_ready_and_extractor_is_read_only(self):
        page = SimpleNamespace(url=TARGET['url'], evaluate=AsyncMock(return_value={
            'comments': [], 'all_loaded': True}),
            goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        driver = PlaywrightDriver(page)
        await driver.open(TARGET['url'])
        expression = page.evaluate.call_args.args[0]
        self.assertNotIn('export function', expression)
        self.assertIn('return readVisibleComments()', expression)
        for forbidden in ('document.cookie', 'localStorage', 'fetch(', 'XMLHttpRequest'):
            self.assertNotIn(forbidden, expression)


if __name__ == '__main__':
    unittest.main()
