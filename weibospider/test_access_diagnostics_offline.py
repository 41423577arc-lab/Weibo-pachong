"""Offline checks for bounded browser collection and credential-safe diagnostics."""
import json
import unittest
from urllib.parse import parse_qs, urlparse

from access_diagnostics import response_diagnostics
from run_browser_tweets import normalize_tweet, initial_queue, safe_search_next, mid_to_sid


class AccessTests(unittest.TestCase):
    def test_numeric_ids_match_observed_public_short_ids(self):
        self.assertEqual(mid_to_sid('5351549251092660'), 'RlD1X4Afy')
        self.assertEqual(mid_to_sid('5351874389873778'), 'RlLumFqCu')

    def test_diagnostics_do_not_persist_secret_headers_or_raw_messages(self):
        result = response_diagnostics('https://weibo.com/ajax/statuses/show?id=abc', 403,
            {'Content-Type': 'application/json', 'Set-Cookie': 'private-token'},
            '{"error":"Forbidden private-token", "error_code":403}')
        self.assertTrue(result['reason_signals']['permission'])
        self.assertEqual(result['id'], 'abc')
        self.assertNotIn('private-token', json.dumps(result))

    def test_response_reason_and_redirect_query_are_redacted(self):
        result = response_diagnostics('https://weibo.com/ajax/statuses/show?id=abc', 302,
            {'Location':'https://passport.weibo.com/login?token=private-token'}, '请先登录')
        self.assertTrue(result['reason_signals']['login'])
        self.assertEqual(result['redirect_host'], 'passport.weibo.com')
        self.assertNotIn('private-token', json.dumps(result))

    def test_search_queue_and_pilot_are_date_bounded(self):
        job = {'start':'2026-10-08','end':'2026-10-09','keyword':'新词'}
        self.assertEqual(len(initial_queue(job)), 1)
        daily = parse_qs(urlparse(initial_queue(job)[0]['url']).query)
        self.assertEqual(daily['timescope'], ['custom:2026-10-08-00:2026-10-09-00'])
        self.assertEqual(len(initial_queue(job, 1)), 1)
        query = parse_qs(urlparse(initial_queue(job, 23)[0]['url']).query)
        self.assertEqual(query['timescope'], ['custom:2026-10-08-23:2026-10-09-00'])

    def test_search_pagination_stays_in_same_authorized_scope(self):
        previous='https://s.weibo.com/weibo?q=test&timescope=custom%3A2026-10-08-00%3A2026-10-08-01&page=1'
        self.assertTrue(safe_search_next(previous.replace('page=1','page=2'), previous))
        self.assertFalse(safe_search_next(previous.replace('q=test','q=other'), previous))
        self.assertFalse(safe_search_next('https://evil.test/weibo', previous))

    def test_browser_body_keeps_rejected_words_raw_data_and_parent_author(self):
        raw={'created_at':'Wed Oct 07 16:30:00 +0000 2026','mid':'123','mblogid':'abc',
             'text_raw':'正文 #不要# #新词','user':{'id':456,'screen_name':'测试'},'isLongText':True}
        item=normalize_tweet(raw,'新词','round')
        self.assertEqual(item['created_at'],'2026-10-08 00:30:00')
        self.assertIn('#不要#',item['content'])
        self.assertFalse(item['long_text_complete'])
        self.assertEqual(item['raw_api_json'],raw)
        full=normalize_tweet(raw,'新词','round',{'data':{'longTextContent':'完整正文 #不要#'}})
        self.assertTrue(full['long_text_complete'])
        self.assertEqual(full['content'],'完整正文 #不要#')
        raw['longText'] = {'content':'已有内嵌长正文'}
        inline=normalize_tweet(raw,'新词','round')
        self.assertTrue(inline['long_text_complete'])
        self.assertEqual(inline['content'],'已有内嵌长正文')


if __name__ == '__main__':
    unittest.main()
