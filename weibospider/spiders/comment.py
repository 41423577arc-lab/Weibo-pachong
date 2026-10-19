"""Comment/reply collection with explicit limits and pagination outcomes."""
import html
import json
import re
from urllib.parse import urlencode

from scrapy import Spider, Request
from scrapy.exceptions import CloseSpider
from spiders.common import parse_time, url_to_mid, parse_user_info


class CommentSpider(Spider):
    name = "comment"
    custom_settings = {
        'ITEM_PIPELINES': {'comment_pipeline.CommentMySQLPipeline': 300},
        'CONCURRENT_REQUESTS': 1, 'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
        'DOWNLOAD_DELAY': 5, 'RETRY_TIMES': 0,
        'TELNETCONSOLE_ENABLED': False,
    }

    def __init__(self, tweet_ids=None, author_ids=None, max_requests=20, max_pages=2,
                 max_reply_threads=2, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tweet_ids = json.loads(tweet_ids) if isinstance(tweet_ids, str) else (tweet_ids or [])
        self.author_ids = json.loads(author_ids) if isinstance(author_ids, str) else (author_ids or {})
        self.max_requests = int(max_requests)
        self.max_pages = int(max_pages)
        self.max_reply_threads = int(max_reply_threads)
        if self.max_requests < 1 or self.max_pages < 0 or self.max_reply_threads < -1:
            raise ValueError('Positive request budget, pages >= 0, reply threads >= -1 required')
        self.scheduled = 0
        self.seen_items = set()
        self.reply_threads = {}
        self.scheduled_reply_threads = set()
        self.audit = []
        self.failed_urls = set()
        self.access_limited = False
        self.requested_cursors = set()
        self.streams = {}

    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        for tweet_id in self.tweet_ids:
            mid = str(tweet_id) if str(tweet_id).isdigit() else str(url_to_mid(tweet_id))
            request = self.make_request(mid)
            if request:
                yield request

    def make_request(self, tweet_id, root_id=None, cursor=0, page=1, cursor_type=None):
        tweet_id = str(tweet_id)
        root_id = str(root_id) if root_id else None
        stream = (tweet_id, root_id)
        if self.access_limited:
            return None
        if self.scheduled >= self.max_requests:
            self.streams[stream] = 'request_budget_limit'
            self.audit.append({'tweet_id': tweet_id, 'root_comment_id': root_id,
                               'event': 'request_budget_limit', 'cursor': cursor})
            return None
        cursor_key = (tweet_id, root_id, str(cursor))
        if cursor_key in self.requested_cursors:
            self.streams[stream] = 'repeated_cursor'
            self.audit.append({'tweet_id': tweet_id, 'root_comment_id': root_id,
                               'event': 'repeated_cursor', 'cursor': str(cursor)})
            return None
        # uid is the original tweet author, never the current login or comment author.
        author_id = self.author_ids.get(str(tweet_id))
        if not author_id or not str(author_id).isdigit():
            raise ValueError('A numeric original tweet author ID is required')
        params = {'is_reload': 1, 'id': root_id or tweet_id, 'is_show_bulletin': 3,
                  'is_mix': 1 if root_id else 0, 'count': 10, 'uid': str(author_id),
                  'fetch_level': 1 if root_id else 0, 'locale': 'zh-CN'}
        # Match the observed initial browser request: max_id is only sent for continuation/replies.
        if page > 1 or root_id:
            params['max_id'] = cursor
        if cursor_type is not None:
            params['max_id_type'] = cursor_type
        self.scheduled += 1
        self.requested_cursors.add(cursor_key)
        self.streams[stream] = 'pending'
        return Request('https://weibo.com/ajax/statuses/buildComments?' + urlencode(params),
                       callback=self.parse, errback=self.request_failed,
                       meta={'tweet_id': tweet_id, 'root_comment_id': root_id,
                             'page': page, 'cursor': str(cursor)}, priority=10 if root_id else 0)

    def request_failed(self, failure):
        response = getattr(failure.value, 'response', None)
        status = response.status if response is not None else None
        # Store diagnostic types/codes only, never body text, headers, or credentials.
        diagnostics = {}
        if response is not None:
            diagnostics['content_type'] = response.headers.get(b'Content-Type', b'').decode('ascii', 'replace').split(';')[0]
            try:
                body = json.loads(response.text)
                diagnostics['json_response'] = True
                if isinstance(body, dict):
                    for key in ('ok', 'error_code', 'errno'):
                        if isinstance(body.get(key), (int, bool)):
                            diagnostics[key] = body[key]
            except (ValueError, AttributeError):
                diagnostics['json_response'] = False
        self.failed_urls.add(failure.request.url)
        self.streams[(failure.request.meta['tweet_id'],
                      failure.request.meta.get('root_comment_id'))] = 'request_failed'
        self.audit.append({'tweet_id': failure.request.meta['tweet_id'],
                           'root_comment_id': failure.request.meta.get('root_comment_id'),
                           'event': 'request_failed', 'status': status,
                           'error_type': failure.type.__name__, 'diagnostics': diagnostics})
        self.crawler.stats.set_value('comments/http_failed', len(self.failed_urls))
        if status in (401, 403, 418, 429) or (status in (301, 302) and response is not None
                                            and b'passport' in response.headers.get(b'Location', b'')):
            self.access_limited = True
            self.crawler.engine.close_spider(self, reason='platform_access_limit')

    def parse(self, response, **kwargs):
        meta = response.meta
        stream = (meta['tweet_id'], meta['root_comment_id'])
        if self.access_limited:
            return
        try:
            data = json.loads(response.text)
        except ValueError:
            self.streams[stream] = 'non_json_response'
            self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                               'event': 'non_json_response'})
            raise CloseSpider('non_json_or_verification_page')
        if (not isinstance(data, dict) or ('ok' in data and data['ok'] != 1)
                or not isinstance(data.get('data'), list)):
            self.streams[stream] = 'unexpected_api_response'
            self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                               'event': 'unexpected_api_response'})
            raise CloseSpider('unexpected_api_response')
        comments = data['data']
        next_cursor = data.get('max_id')
        self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                           'event': 'page', 'page': meta['page'], 'returned': len(comments),
                           'next_cursor': str(next_cursor), 'cursor_present': 'max_id' in data,
                           'api_total': data.get('total_number'),
                           'api_says_all_loaded': data.get('trendsText') == '已加载全部评论',
                           'rootComment_count': len(data['rootComment'])
                           if isinstance(data.get('rootComment'), list) else None})
        for comment in comments:
            try:
                item = self.parse_comment(comment, meta['tweet_id'], meta['root_comment_id'])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.audit.append({'tweet_id': meta['tweet_id'],
                                   'root_comment_id': meta['root_comment_id'],
                                   'event': 'invalid_comment', 'error_type': type(exc).__name__})
                continue
            if item['_id'] not in self.seen_items:
                self.seen_items.add(item['_id'])
                yield item
            if item['comment_level'] == 1:
                root_id = str(comment['id'])
                # Inline replies can be available even before requesting the thread endpoint.
                for reply in comment.get('comments') or []:
                    try:
                        reply_item = self.parse_comment(reply, meta['tweet_id'], root_id)
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': root_id,
                                           'event': 'invalid_comment', 'error_type': type(exc).__name__})
                        continue
                    if reply_item['_id'] not in self.seen_items:
                        self.seen_items.add(reply_item['_id'])
                        yield reply_item
                has_replies = bool(comment.get('more_info') or comment.get('comments')
                                   or comment.get('total_number') or comment.get('more_info_type'))
                thread_key = (meta['tweet_id'], root_id)
                if has_replies and thread_key not in self.scheduled_reply_threads:
                    self.scheduled_reply_threads.add(thread_key)
                    count = self.reply_threads.get(meta['tweet_id'], 0)
                    if self.max_reply_threads == -1 or count < self.max_reply_threads:
                        self.reply_threads[meta['tweet_id']] = count + 1
                        request = self.make_request(meta['tweet_id'], root_id)
                        if request:
                            yield request
                    else:
                        self.streams[(meta['tweet_id'], root_id)] = 'reply_thread_limit'
                        self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': root_id,
                                           'event': 'reply_thread_limit'})
        if 'max_id' not in data:
            self.streams[stream] = 'missing_pagination_cursor'
            self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                               'event': 'missing_pagination_cursor'})
        elif next_cursor not in (0, '0', None, ''):
            if (self.max_pages and meta['page'] >= self.max_pages) or not comments:
                self.streams[stream] = 'page_limit' if comments else 'empty_page_with_cursor'
                self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                                   'event': self.streams[stream], 'cursor': str(next_cursor)})
            else:
                request = self.make_request(meta['tweet_id'], meta['root_comment_id'], next_cursor,
                                            meta['page'] + 1, data.get('max_id_type'))
                if request:
                    yield request
        elif next_cursor in (0, '0'):
            self.streams[stream] = 'exhausted'
        else:
            self.streams[stream] = 'invalid_pagination_cursor'
            self.audit.append({'tweet_id': meta['tweet_id'], 'root_comment_id': meta['root_comment_id'],
                               'event': 'invalid_pagination_cursor'})

    @staticmethod
    def parse_comment(data, tweet_id, root_comment_id=None):
        comment_id = str(data['id'])
        # Preserve the API value; rootid is not necessarily the directly replied-to comment.
        api_rootid = data.get('rootid')
        api_root = str(api_rootid) if api_rootid not in (None, 0, '0', '') else None
        if api_root == comment_id:
            api_root = None
        if root_comment_id and api_root and str(root_comment_id) != api_root:
            raise ValueError('API rootid disagrees with the requested reply thread')
        thread_root = str(root_comment_id) if root_comment_id else api_root
        reply = data.get('reply_comment') or {}
        user = data['user']
        content = data.get('text_raw')
        if content is None:
            content = html.unescape(re.sub(r'<[^>]+>', '', data.get('text', '')))
        return {
            '_id': comment_id, 'tweet_id': str(tweet_id), 'api_rootid':
                str(api_rootid) if api_rootid is not None else None,
            'root_comment_id': thread_root,
            'parent_comment_id': str(reply.get('id') or thread_root) if thread_root else None,
            'parent_relation_source': ('reply_comment' if reply.get('id') else 'thread_root_fallback')
                if thread_root else None,
            'comment_level': 2 if thread_root else 1,
            'content': content, 'created_at': parse_time(data['created_at']),
            'like_counts': data.get('like_counts', data.get('like_count')),
            'ip_location': data.get('source', ''),
            'comment_user': {'_id': str(user['id']), 'nick_name': user['screen_name'],
                             'avatar_hd': user.get('avatar_hd', user.get('profile_image_url')),
                             'verified': user.get('verified')},
        }
