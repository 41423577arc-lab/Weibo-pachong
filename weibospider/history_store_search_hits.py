"""Store already observed search-link evidence, not unverified tweet bodies.

Reads checkpoints only; makes no platform requests. Uses a distinct spider namespace.
"""
from local_config import read_config
import json
import time
from urllib.parse import parse_qs, urlparse

import pymysql
from history_collect import PROJECT, STATE_PATH, write_json


def main():
    state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    config = read_config()
    db = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
        port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
        password=config['WEIBO_DB_PASSWORD'], database='weibo_data', charset='utf8mb4', connect_timeout=10)
    records = []
    for url, response in state['request_results'].items():
        if urlparse(url).hostname != 's.weibo.com' or 'links' not in response:
            continue
        query = parse_qs(urlparse(url).query)
        keyword = query['q'][0]
        requested_date = query['timescope'][0].split(':')[1][:10]
        for link in response['links']:
            records.append({**link, '_id': f"{link['tweet_id']}_{requested_date}",
                'keyword': keyword, 'requested_date': requested_date,
                'url': f"https://weibo.com/{link['author_id']}/{link['mblogid']}",
                'source': 'observed_weibo_search_link', 'source_search_url': url,
                'search_page': int(query.get('page', ['1'])[0]),
                'publication_date_verified': False, 'tweet_body_available': False,
                'crawl_time': int(time.time())})
    try:
        with db.cursor() as cur:
            added = 0
            for item in records:
                cur.execute('INSERT IGNORE INTO crawl_items (spider,item_id,keyword,crawl_time,payload) '
                    'VALUES (%s,%s,%s,%s,%s)', ('history_search_hit', item['_id'], item['keyword'],
                    item['crawl_time'], json.dumps(item, ensure_ascii=False)))
                added += cur.rowcount
        db.commit()
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*),COUNT(DISTINCT payload->>'$.tweet_id') FROM crawl_items WHERE spider='history_search_hit'")
            count, unique_ids = cur.fetchone()
    finally:
        db.close()
    result = {'observed_link_records': len(records), 'new_search_index_rows': added,
              'stored_search_index_rows': count, 'distinct_observed_tweet_ids': unique_ids,
              'tweet_bodies_saved': False, 'dates_verified': False, 'network_requests': 0}
    write_json(PROJECT / 'logs/history-search-index-verification.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
