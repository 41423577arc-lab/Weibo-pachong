"""Preview or collect comments on all unique stored tweets with nonzero counts."""
from local_config import read_config
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from comment_run import run_outcome, select_targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', action='store_true', help='Without this flag only preview targets')
    parser.add_argument('--since', help='Earliest tweet collection time, local YYYY-MM-DD')
    parser.add_argument('--limit', type=int, help='Optional number of unique tweets; default all')
    parser.add_argument('--max-requests', type=int, default=1000)
    parser.add_argument('--max-pages', type=int, default=0, help='0 follows cursors until exhaustion')
    parser.add_argument('--max-reply-threads', type=int, default=-1, help='-1 visits all exposed reply threads')
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or args.max_requests < 1 or args.max_pages < 0 or args.max_reply_threads < -1:
        parser.error('Invalid limits')
    since = datetime.strptime(args.since, '%Y-%m-%d').timestamp() if args.since else None
    project = Path(__file__).resolve().parents[1]
    config = read_config()
    for key in ('WEIBO_COOKIE', 'WEIBO_DB_USER', 'WEIBO_DB_PASSWORD', 'WEIBO_DB_HOST', 'WEIBO_DB_PORT'):
        if key in config:
            os.environ[key] = str(config[key])
    os.environ['WEIBO_DB_NAME'] = 'weibo_data'
    import pymysql
    conn = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
                           port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
                           password=config['WEIBO_DB_PASSWORD'], database='weibo_data',
                           charset='utf8mb4', connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT spider,item_id,crawl_time,payload FROM crawl_items WHERE spider LIKE 'tweet%'")
            targets = select_targets(cur.fetchall(), since, args.limit)
    finally:
        conn.close()
    logs = project / 'logs'
    logs.mkdir(exist_ok=True)
    (logs / 'comment-targets.json').write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'target_count': len(targets), 'targets': targets}, ensure_ascii=False))
    if not args.collect:
        return 0
    if not targets:
        return 5
    os.chdir(Path(__file__).resolve().parent)
    os.environ['SCRAPY_SETTINGS_MODULE'] = 'settings'
    from scrapy import signals
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings
    from spiders.comment import CommentSpider
    settings = get_project_settings()
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
    for key, value in {'DOWNLOAD_TIMEOUT': 25, 'RANDOMIZE_DOWNLOAD_DELAY': False,
                       'LOG_LEVEL': 'INFO', 'LOG_FILE': str(logs / f'comments-{run_id}.log')}.items():
        settings.set(key, value, priority='cmdline')
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(CommentSpider)
    errors = []
    crawler.signals.connect(lambda failure, response, spider: errors.append(type(failure.value).__name__),
                            signal=signals.spider_error, weak=False)
    process.crawl(crawler, tweet_ids=[t['tweet_id'] for t in targets],
                  author_ids={t['tweet_id']: t['author_id'] for t in targets},
                  max_requests=args.max_requests, max_pages=args.max_pages,
                  max_reply_threads=args.max_reply_threads)
    process.start()
    # A yielded item is only a successful collection after it can be read back.
    conn = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
                           port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
                           password=config['WEIBO_DB_PASSWORD'], database='weibo_data',
                           charset='utf8mb4', connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT item_id,payload FROM crawl_items WHERE spider='comment'")
            captured = [json.loads(payload) for mid, payload in cur.fetchall() if mid in crawler.spider.seen_items]
    finally:
        conn.close()
    stats = crawler.stats.get_stats()
    result = {'run_id': run_id, 'targets': targets, 'audit': crawler.spider.audit,
              'stats': stats, 'mysql_captured_rows': len(captured), 'parse_errors': errors,
              **run_outcome(crawler.spider, stats, len(captured), errors)}
    (logs / f'comments-{run_id}-verification.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result['collection_verified'] else 5


if __name__ == '__main__':
    raise SystemExit(main())
