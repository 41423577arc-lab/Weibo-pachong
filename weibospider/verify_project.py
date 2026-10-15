"""Read MySQL back and report actual stored fields without exposing credentials."""
from local_config import read_config
import collections
import json
from pathlib import Path

import pymysql


def main():
    project = Path(__file__).resolve().parents[1]
    config = read_config()
    stats = json.loads((project / 'logs/last-result.json').read_text(encoding='utf-8'))
    task = stats['task']
    connection = pymysql.connect(
        host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
        port=int(config.get('WEIBO_DB_PORT') or 3306),
        user=config['WEIBO_DB_USER'], password=config['WEIBO_DB_PASSWORD'],
        database=config.get('WEIBO_DB_NAME') or 'weibo_data', charset='utf8mb4', connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM crawl_items WHERE spider=%s AND keyword=%s "
                           "AND payload->>'$.created_at'>=%s AND payload->>'$.created_at'<%s",
                           ('tweet_spider_by_keyword', task['keyword'], task['start_date'] + ' 00:00:00',
                            task['end_date_exclusive'] + ' 00:00:00'))
            items = [json.loads(row[0]) for row in cursor.fetchall()]
    finally:
        connection.close()
    fields = collections.Counter(key for item in items for key in item)
    user_fields = collections.Counter(key for item in items for key in item.get('user', {}))
    times = [item['created_at'] for item in items]
    result = {
        'task': task,
        'database': config.get('WEIBO_DB_NAME') or 'weibo_data',
        'table': 'crawl_items',
        'stored_unique_items': len(items),
        'distinct_ids': len({str(item['_id']) for item in items}),
        'successful_item_commits': stats.get('database/items_committed', 0),
        'database_write_failures': stats.get('database/write_failures', 0),
        'failed_requests_after_retries': stats.get('requests/failed_terminal', 0),
        'parse_failed_requests': stats.get('requests/parse_failed', 0),
        'failed_requests_total': stats.get('requests/failed_total', 0),
        'request_attempts': stats.get('downloader/request_count', 0),
        'retries': stats.get('retry/count', 0),
        'http_response_status_counts': {key.rsplit('/', 1)[-1]: value for key, value in stats.items()
                                        if key.startswith('downloader/response_status_count/')},
        'finish_reason': stats.get('finish_reason'),
        'first_publish_time': min(times) if times else None,
        'last_publish_time': max(times) if times else None,
        'field_presence_counts': dict(sorted(fields.items())),
        'user_field_presence_counts': dict(sorted(user_fields.items())),
    }
    (project / 'logs/database-verification.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
