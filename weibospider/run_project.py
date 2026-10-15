"""Run the project keyword task with local credentials and a database preflight."""
import argparse
import json
import os
from pathlib import Path
from local_config import load_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keyword', default='第五人格亚运会')
    parser.add_argument('--start-date', default='2026-10-01')
    parser.add_argument('--end-date', default='2026-10-03', help='exclusive end date')
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    load_environment()
    missing = [key for key in ('WEIBO_COOKIE', 'WEIBO_DB_USER') if not os.environ.get(key)]
    if missing:
        print('Missing local configuration: ' + ', '.join(missing))
        return 2

    import pymysql
    database = os.environ.get('WEIBO_DB_NAME', 'weibo_data')
    if not database.replace('_', '').isalnum() or len(database) > 64:
        print('Invalid database name')
        return 2
    try:
        connection = pymysql.connect(
            host=os.environ.get('WEIBO_DB_HOST', '127.0.0.1'),
            port=int(os.environ.get('WEIBO_DB_PORT', '3306')),
            user=os.environ['WEIBO_DB_USER'], password=os.environ.get('WEIBO_DB_PASSWORD', ''),
            charset='utf8mb4', connect_timeout=10)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        finally:
            connection.close()
    except pymysql.MySQLError as exc:
        print(f'MySQL preflight failed; error code: {exc.args[0]} (credentials not printed)')
        return 3
    print('Local configuration and MySQL database preflight passed.')
    if args.preflight_only:
        return 0

    os.chdir(Path(__file__).resolve().parent)
    os.environ['SCRAPY_SETTINGS_MODULE'] = 'settings'
    from scrapy.crawler import CrawlerProcess
    from scrapy import signals
    from scrapy.utils.project import get_project_settings
    from spiders.tweet_by_keyword import TweetSpiderByKeyword
    logs = project / 'logs'
    logs.mkdir(exist_ok=True)
    settings = get_project_settings()
    settings.set('CONCURRENT_REQUESTS', 2, priority='cmdline')
    settings.set('DOWNLOAD_DELAY', 3, priority='cmdline')
    settings.set('DOWNLOAD_TIMEOUT', 30, priority='cmdline')
    settings.set('RETRY_TIMES', 1, priority='cmdline')
    settings.set('LOG_LEVEL', 'INFO', priority='cmdline')
    settings.set('LOG_FILE', str(logs / 'keyword-crawl.log'), priority='cmdline')
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(TweetSpiderByKeyword)
    parse_failed_urls = set()
    def parsing_failed(failure, response, spider):
        parse_failed_urls.add(response.url)
        crawler.stats.set_value('requests/parse_failed', len(parse_failed_urls))
    crawler.signals.connect(parsing_failed, signal=signals.spider_error, weak=False)
    process.crawl(crawler, keyword=args.keyword, start_date=args.start_date, end_date=args.end_date)
    process.start()
    stats = crawler.stats.get_stats()
    result = dict(stats)
    result['task'] = {'keyword': args.keyword, 'start_date': args.start_date, 'end_date_exclusive': args.end_date}
    result['requests/failed_total'] = len(parse_failed_urls | crawler.spider.failed_request_urls)
    (logs / 'last-result.json').write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if stats.get('finish_reason') == 'finished' else 4


if __name__ == '__main__':
    raise SystemExit(main())
