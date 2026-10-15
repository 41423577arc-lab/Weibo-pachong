"""Bounded five-tweet test; preserve existing rows and verify MySQL associations."""
from local_config import read_config
import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import pymysql
from comment_run import run_outcome, select_targets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tweet-id', help='Test only one already stored numeric tweet ID')
    parser.add_argument('--max-requests', type=int, default=20)
    parser.add_argument('--max-pages', type=int, default=2)
    parser.add_argument('--max-reply-threads', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 20 or not 1 <= args.max_pages <= 2 or not 0 <= args.max_reply_threads <= 2:
        parser.error('Limits must stay within the small-test bounds')
    project = Path(__file__).resolve().parents[1]
    config = read_config()
    for key in ('WEIBO_COOKIE', 'WEIBO_DB_USER', 'WEIBO_DB_PASSWORD', 'WEIBO_DB_HOST', 'WEIBO_DB_PORT'):
        if key in config:
            os.environ[key] = str(config[key])
    os.environ['WEIBO_DB_NAME'] = 'weibo_data'
    conn = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
                           port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
                           password=config['WEIBO_DB_PASSWORD'], database='weibo_data', charset='utf8mb4',
                           connect_timeout=10, autocommit=True)
    def snapshot():
        with conn.cursor() as cur:
            cur.execute('SELECT spider,item_id,keyword,crawl_time,payload FROM crawl_items ORDER BY spider,item_id,keyword')
            rows = cur.fetchall()
        return {(row[0],row[1],row[2]): hashlib.sha256(json.dumps(row,ensure_ascii=False).encode()).hexdigest()
                for row in rows}
    before = snapshot()
    with conn.cursor() as cur:
        if args.tweet_id:
            cur.execute("SELECT item_id,payload FROM crawl_items WHERE spider='tweet_spider_by_keyword' "
                        "AND item_id=%s LIMIT 1", (args.tweet_id,))
        else:
            cur.execute("SELECT spider,item_id,crawl_time,payload FROM crawl_items "
                        "WHERE spider LIKE 'tweet%'")
            selected = select_targets(cur.fetchall(), limit=5)
        targets=[]
        source_rows = cur.fetchall() if args.tweet_id else []
        for mid,payload in source_rows:
            d=json.loads(payload)
            targets.append({'tweet_id':str(mid),'mblogid':d.get('mblogid'),
                            'author_id':d.get('user',{}).get('_id'),
                            'comments_count_at_tweet_crawl':d.get('comments_count'),
                            'content_excerpt':d.get('content','')[:160],'url':d.get('url')})
        if not args.tweet_id:
            targets = selected
    if not targets:
        raise RuntimeError('Required existing tweet records were not found')
    os.chdir(Path(__file__).resolve().parent)
    os.environ['SCRAPY_SETTINGS_MODULE']='settings'
    from scrapy import signals
    from scrapy.crawler import CrawlerProcess
    from scrapy.utils.project import get_project_settings
    from spiders.comment import CommentSpider
    logs=project/'logs'
    logs.mkdir(exist_ok=True)
    run_id=datetime.now().strftime('%Y%m%d-%H%M%S')
    settings=get_project_settings()
    settings.set('ITEM_PIPELINES',{'comment_pipeline.CommentMySQLPipeline':300},priority='cmdline')
    for key,value in {'CONCURRENT_REQUESTS':1,'CONCURRENT_REQUESTS_PER_DOMAIN':1,'DOWNLOAD_DELAY':5,
                      'RANDOMIZE_DOWNLOAD_DELAY':False,'DOWNLOAD_TIMEOUT':25,'RETRY_TIMES':0,
                      'TELNETCONSOLE_ENABLED':False,'LOG_LEVEL':'INFO',
                      'LOG_FILE':str(logs/f'comments-{run_id}.log')}.items():
        settings.set(key,value,priority='cmdline')
    process=CrawlerProcess(settings)
    crawler=process.create_crawler(CommentSpider)
    parse_errors=[]
    def parse_error(failure,response,spider):
        parse_errors.append({'tweet_id':response.meta.get('tweet_id'),
                             'root_comment_id':response.meta.get('root_comment_id'),
                             'error_type':failure.type.__name__})
    crawler.signals.connect(parse_error,signal=signals.spider_error,weak=False)
    process.crawl(crawler,tweet_ids=[t['tweet_id'] for t in targets],
                  author_ids={t['tweet_id']:t['author_id'] for t in targets},
                  max_requests=args.max_requests,max_pages=args.max_pages,
                  max_reply_threads=args.max_reply_threads)
    process.start()
    after=snapshot()
    old_unchanged=all(after.get(key)==value for key,value in before.items())
    new_keys=set(after)-set(before)
    seen_ids=crawler.spider.seen_items
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM crawl_items WHERE spider='comment'")
        items=[json.loads(row[0]) for row in cur.fetchall()]
    conn.close()
    captured=[d for d in items if d['_id'] in seen_ids]
    by_tweet={}
    for target in targets:
        records=[d for d in captured if d['tweet_id']==target['tweet_id']]
        by_tweet[target['tweet_id']]={'level_1':sum(d['comment_level']==1 for d in records),
                                     'level_2':sum(d['comment_level']==2 for d in records)}
    required=['_id','tweet_id','parent_comment_id','root_comment_id','comment_level',
              'content','created_at','like_counts','comment_user','crawl_time']
    missing={key:sum(key not in item or (item[key] is None and key not in ('parent_comment_id','root_comment_id'))
                     for item in captured) for key in required}
    parent_index={d['_id']:d['tweet_id'] for d in captured}
    links_valid=all(d['tweet_id'] in by_tweet and (d['comment_level']==1 and d['parent_comment_id'] is None
                      or d['comment_level']==2 and parent_index.get(d['root_comment_id'])==d['tweet_id'])
                    for d in captured)
    stats=crawler.stats.get_stats()
    result={'run_id':run_id,'targets':targets,'limits':{'max_requests':args.max_requests,'max_pages_per_stream':args.max_pages,
            'max_reply_threads_per_tweet':args.max_reply_threads,'concurrency':1,'download_delay_seconds':5},
            'fetched_unique_comments':len(seen_ids),'mysql_captured_rows':len(captured),
            'new_mysql_rows':len(new_keys),'existing_rows_before':len(before),
            'existing_rows_unchanged':old_unchanged,'levels':dict(Counter(str(d['comment_level']) for d in captured)),
            'by_tweet':by_tweet,'association_valid':links_valid if captured else None,
            'field_missing_or_null_counts':missing if captured else None,
            'user_fields_complete':all(d['comment_user'].get('_id') and d['comment_user'].get('nick_name')
                                       for d in captured) if captured else None,
            'parse_errors':parse_errors,'audit':crawler.spider.audit,'stats':stats}
    result.update(run_outcome(crawler.spider, stats, len(captured), parse_errors, old_unchanged))
    output=logs/f'comments-{run_id}-verification.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0 if result['collection_verified'] and links_valid else 5


if __name__=='__main__':
    raise SystemExit(main())
