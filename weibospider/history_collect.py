"""Authorized, bounded historical collection with durable per-request checkpoints.

No browser authentication extraction, fingerprint rotation, or access-limit retries.
"""
from local_config import read_config
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo
from dateutil.parser import parse as parse_datetime

import pymysql
import requests
from parsel import Selector
from spiders.common import parse_tweet_info, parse_time, url_to_mid

PROJECT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT / 'logs/history-state.json'
KEYWORDS = ['第五人格', '第五人格亚运会', '第五人格名古屋亚运会']
DATES = ['2026-09-30', '2026-10-01', '2026-10-02']
SHANGHAI = ZoneInfo('Asia/Shanghai')
START = '2026-09-30 00:00:00'
END = '2026-10-03 00:00:00'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def fresh_state():
    queue = []
    # Narrow queries first; rotate among all keyword/date batches rather than exhaust one batch.
    for keyword in KEYWORDS[1:] + KEYWORDS[:1]:
        for date in DATES:
            queue.append({'kind': 'search', 'keyword': keyword, 'date': date, 'page': 1})
    return {'version': 1, 'queue': queue, 'requests': 0, 'spent_seconds': 0,
            'http_status': {}, 'events': [], 'raw_search_hits': {}, 'search_pages': {},
            'successful_search_urls': [], 'blocked_routes': [], 'pilot_passed': False,
            'comments_status': 'blocked_http403_and_browser_connection_unavailable',
            'cache': {}, 'inflight': None, 'request_results': {}, 'seen_cursors': [],
            'deferred_tasks': [],
            'batch_status': {f'{k}|{d}': 'pending' for k in KEYWORDS for d in DATES}}


def search_url(task):
    next_date = (dt.date.fromisoformat(task['date']) + dt.timedelta(days=1)).isoformat()
    return 'https://s.weibo.com/weibo?' + urlencode({
        'q': task['keyword'], 'timescope': f"custom:{task['date']}-00:{next_date}-00", 'page': task['page']})


def parse_search(body, url):
    if any(marker in body for marker in ('新浪通行证', '访问频次过高', '访问异常', '请输入验证码')):
        return {'blocked': True}
    if '抱歉，未找到相关结果' in body:
        return {'links': [], 'raw_hits': 0, 'next_url': None, 'empty': True}
    selector = Selector(text=body)
    links = []
    for href in selector.css('.from a::attr(href)').getall():
        match = re.search(r'weibo\.com/(\d+)/([A-Za-z0-9]+)', href)
        if match:
            links.append({'author_id': match.group(1), 'mblogid': match.group(2),
                          'tweet_id': str(url_to_mid(match.group(2)))})
    if not links:
        return {'unrecognized': True}
    next_url = None
    for href in selector.css('a.next::attr(href)').getall():
        candidate = urljoin(url, href)
        parsed = urlparse(candidate)
        if parsed.scheme in ('http', 'https') and parsed.hostname == 's.weibo.com':
            next_url = candidate
            break
    return {'links': list({v['tweet_id']: v for v in links}.values()),
            'raw_hits': len(links), 'next_url': next_url, 'empty': False}


def normalized_tweet(raw, keyword):
    item = parse_tweet_info(raw)
    timestamp = parse_datetime(raw['created_at'])
    if timestamp.tzinfo is None:
        raise ValueError('API timestamp has no timezone')
    item['created_at'] = timestamp.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    item['keyword'] = keyword
    item['raw_api_json'] = raw
    item['crawl_time'] = int(time.time())
    if raw.get('isLongText'):
        item['isLongText'] = True
        item['long_text_complete'] = False
    return item


class Collector:
    def __init__(self, args):
        self.args = args
        self.state = json.loads(STATE_PATH.read_text(encoding='utf-8')) if STATE_PATH.exists() else fresh_state()
        config = read_config()
        self.db = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
            port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
            password=config['WEIBO_DB_PASSWORD'], database=config.get('WEIBO_DB_NAME', 'weibo_data'), charset='utf8mb4',
            connect_timeout=10, read_timeout=30, write_timeout=30, autocommit=True)
        self.session = requests.Session()
        # Keep the existing project request identity; do not rotate identities after denial.
        self.session.headers.update({'Cookie': config['WEIBO_COOKIE'],
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.13; rv:61.0) Gecko/20100101 Firefox/61.0'})
        self.started = time.monotonic()
        self.prior_spent = self.state['spent_seconds']
        self.last_request = None
        if args.access_restored:
            # Explicit operator assertion required. Never clear a denial automatically.
            route = args.access_restored
            route_path = {'detail': '/ajax/statuses/show', 'longtext': '/ajax/statuses/longtext',
                          'search': '/weibo'}[route]
            for url, result in list(self.state['request_results'].items()):
                if urlparse(url).path == route_path and any(key in result for key in
                        ('status', 'error_type', 'invalid_json', 'blocked', 'unrecognized')):
                    self.state.setdefault('past_denied_responses', []).append({'url': url, 'result': result})
                    del self.state['request_results'][url]
            replay = [t for t in self.state['deferred_tasks'] if t['kind'] == route]
            self.state['deferred_tasks'] = [t for t in self.state['deferred_tasks'] if t['kind'] != route]
            self.state['queue'] = replay + self.state['queue']
            self.state['blocked_routes'] = [r for r in self.state['blocked_routes'] if r != route]
            self.state['events'].append({'event': 'operator_asserted_normal_access_restored', 'route': route})
            self.save()

    def save(self):
        self.state['spent_seconds'] = self.prior_spent + time.monotonic() - self.started
        write_json(STATE_PATH, self.state)

    def event(self, task, event, **details):
        self.state['events'].append({'kind': task['kind'], 'keyword': task.get('keyword'),
            'date': task.get('date'), 'page': task.get('page'), 'tweet_id': task.get('tweet_id'),
            'event': event, **details})

    def route(self, task):
        return task['kind']

    def request(self, task, url):
        route = self.route(task)
        if route in self.state['blocked_routes']:
            self.event(task, 'route_already_blocked')
            self.state['deferred_tasks'].append(task.copy())
            return None
        if url in self.state['request_results']:
            return self.state['request_results'][url]
        # An interrupted, unanswered request is not silently reissued.
        if self.state['inflight']:
            self.event(task, 'unresolved_inflight_request')
            raise RuntimeError('Unresolved interrupted request; inspect checkpoint before continuing')
        elapsed = self.prior_spent + time.monotonic() - self.started
        if self.state['requests'] >= self.args.max_requests or elapsed >= self.args.max_seconds:
            raise TimeoutError('collection_budget_reached')
        if self.last_request is not None:
            remaining = self.args.delay - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        if self.prior_spent + time.monotonic() - self.started >= self.args.max_seconds:
            raise TimeoutError('collection_budget_reached')
        self.state['requests'] += 1
        self.state['inflight'] = {'url': url, 'task': task}
        self.save()
        self.last_request = time.monotonic()
        try:
            response = self.session.get(url, timeout=(10, 25), allow_redirects=False)
        except requests.RequestException as exc:
            result = {'error_type': type(exc).__name__}
            self.event(task, 'network_failure', error_type=type(exc).__name__)
        else:
            status = str(response.status_code)
            self.state['http_status'][status] = self.state['http_status'].get(status, 0) + 1
            if response.status_code != 200:
                result = {'status': response.status_code}
                self.event(task, 'http_failure', status=response.status_code)
                if response.status_code in (401, 403, 418, 429, 301, 302):
                    self.state['blocked_routes'].append(route)
            elif task['kind'] == 'search':
                result = parse_search(response.text, url)
                if result.get('blocked') or result.get('unrecognized'):
                    self.state['blocked_routes'].append(route)
                    self.event(task, 'blocked_or_unrecognized_search')
            else:
                try:
                    result = {'json': response.json()}
                    if not isinstance(result['json'], dict) or ('ok' in result['json'] and result['json']['ok'] == 0):
                        raise ValueError('unexpected_api_response')
                except ValueError:
                    result = {'invalid_json': True}
                    self.state['blocked_routes'].append(route)
                    self.event(task, 'invalid_json_or_api_denial')
        self.state['request_results'][url] = result
        if result.get('status') or result.get('error_type') or result.get('invalid_json') or result.get('blocked') or result.get('unrecognized'):
            self.state['deferred_tasks'].append(task.copy())
        self.state['inflight'] = None
        self.save()
        return result

    def insert(self, item):
        # Existing records are never updated, even on an identical key.
        with self.db.cursor() as cursor:
            cursor.execute('INSERT IGNORE INTO crawl_items (spider,item_id,keyword,crawl_time,payload) '
                'VALUES (%s,%s,%s,%s,%s)', ('tweet_spider_by_keyword', str(item['_id']),
                item['keyword'], item['crawl_time'], json.dumps(item, ensure_ascii=False)))
            return cursor.rowcount

    def handle(self, task):
        batch = f"{task['keyword']}|{task['date']}"
        if task['kind'] == 'search':
            result = self.request(task, task.get('url') or search_url(task))
            if not result or 'links' not in result:
                self.state['batch_status'][batch] = 'access_blocked_or_failed'
                return
            self.state['raw_search_hits'][batch] = self.state['raw_search_hits'].get(batch, 0) + result['raw_hits']
            self.state['search_pages'][batch] = self.state['search_pages'].get(batch, 0) + 1
            for link in result['links']:
                self.state['queue'].append({**task, **link, 'kind': 'detail'})
            if result['next_url']:
                if task['page'] >= self.args.max_pages:
                    self.state['batch_status'][batch] = 'page_cap_pending'
                    self.event(task, 'page_cap_pending', next_url=result['next_url'])
                elif result['next_url'] in self.state['seen_cursors']:
                    self.state['batch_status'][batch] = 'pagination_loop'
                    self.event(task, 'pagination_loop', next_url=result['next_url'])
                else:
                    self.state['seen_cursors'].append(result['next_url'])
                    self.state['queue'].append({**task, 'page': task['page'] + 1, 'url': result['next_url']})
                    self.state['batch_status'][batch] = 'pagination_pending'
            else:
                self.state['batch_status'][batch] = 'visible_search_end_not_completeness_proof'
            return
        if task['kind'] == 'detail':
            raw = self.state['cache'].get(task['tweet_id'])
            if raw is None:
                with self.db.cursor() as cur:
                    cur.execute("SELECT payload FROM crawl_items WHERE spider='tweet_spider_by_keyword' AND item_id=%s LIMIT 1",
                                (task['tweet_id'],))
                    row = cur.fetchone()
                if row:
                    raw = json.loads(row[0]).get('raw_api_json')
            if raw is None:
                result = self.request(task, 'https://weibo.com/ajax/statuses/show?' + urlencode({'id': task['mblogid']}))
                if not result or 'json' not in result:
                    return
                raw = result['json']
            try:
                item = normalized_tweet(raw, task['keyword'])
                if str(item['_id']) != task['tweet_id']:
                    raise ValueError('returned_mid_mismatch')
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                self.event(task, 'detail_parse_error', error_type=type(exc).__name__)
                return
            self.state['cache'][task['tweet_id']] = raw
            if not START <= item['created_at'] < END or item['created_at'][:10] != task['date']:
                self.event(task, 'outside_requested_day', actual_date=item['created_at'][:10])
                return
            if raw.get('isLongText'):
                self.state['queue'].append({**task, 'kind': 'longtext', 'pending_item': item})
            else:
                inserted = self.insert(item)
                self.event(task, 'inserted' if inserted else 'already_stored', long_text_pending=False)
                self.state['pilot_passed'] = True
            return
        # Save the entire long-text response, then insert once without updating existing records.
        result = self.request(task, 'https://weibo.com/ajax/statuses/longtext?' + urlencode({'id': task['mblogid']}))
        item = task['pending_item']
        if result and 'json' in result and isinstance(result['json'].get('data'), dict):
            body = result['json']['data'].get('longTextContent')
            item['raw_longtext_json'] = result['json']
            if isinstance(body, str):
                item['content'] = body
                item['long_text_complete'] = True
        inserted = self.insert(item)
        self.event(task, 'inserted' if inserted else 'already_stored', long_text_pending=not item.get('long_text_complete'))
        self.state['pilot_passed'] = True

    def run(self):
        try:
            if self.args.inventory_only:
                # Diagnose unaffected search availability; never retry refused detail/comment routes.
                for task in list(self.state['queue']):
                    if task['kind'] != 'search' or task['page'] != 1:
                        continue
                    if 'search' in self.state['blocked_routes']:
                        self.state['stop_reason'] = 'search_inventory_access_blocked'
                        break
                    self.handle(task)
                    self.state['queue'].remove(task)
                    self.save()
                else:
                    self.state['stop_reason'] = 'search_inventory_done_detail_still_blocked'
                return
            while self.state['queue']:
                task = self.state['queue'][0]
                # Before the pilot passes, process one search result immediately rather than expand batches.
                self.handle(task)
                self.state['queue'].pop(0)
                if not self.state['pilot_passed']:
                    for index, pending in enumerate(self.state['queue']):
                        if pending['kind'] in ('detail', 'longtext'):
                            self.state['queue'].insert(0, self.state['queue'].pop(index))
                            break
                self.save()
                if not self.state['pilot_passed'] and 'detail' in self.state['blocked_routes']:
                    self.state['stop_reason'] = 'pilot_detail_access_blocked'
                    break
                if self.args.pilot_only and self.state['pilot_passed']:
                    self.state['stop_reason'] = 'pilot_passed'
                    break
                if 'search' in self.state['blocked_routes'] and 'detail' in self.state['blocked_routes']:
                    self.state['stop_reason'] = 'all_tweet_routes_blocked'
                    break
            else:
                self.state['stop_reason'] = 'queue_exhausted_with_recorded_omissions'
        except TimeoutError:
            self.state['stop_reason'] = 'budget_reached'
        except Exception as exc:
            self.state['stop_reason'] = 'local_error_' + type(exc).__name__
            self.save()
            raise
        finally:
            self.save()
            self.db.close()
        print(json.dumps({'requests': self.state['requests'], 'http_status': self.state['http_status'],
            'pilot_passed': self.state['pilot_passed'], 'blocked_routes': self.state['blocked_routes'],
            'pending_tasks': len(self.state['queue']), 'stop_reason': self.state.get('stop_reason')}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pilot-only', action='store_true')
    parser.add_argument('--inventory-only', action='store_true',
                        help='Check remaining search first pages only; keep all detail/page tasks pending')
    parser.add_argument('--access-restored', choices=['search', 'detail', 'longtext'],
                        help='Operator explicitly confirms normal authorized access has been restored; preserves total budget')
    parser.add_argument('--max-requests', type=int, default=2000)
    parser.add_argument('--max-seconds', type=int, default=7200)
    parser.add_argument('--max-pages', type=int, default=50)
    parser.add_argument('--delay', type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 2000 or not 1 <= args.max_seconds <= 7200 or args.delay < 5 or not 1 <= args.max_pages <= 50:
        parser.error('Limits: at most 2000 requests/7200 seconds/50 pages, at least 5-second delay')
    Collector(args).run()


if __name__ == '__main__':
    main()
