"""Sequential, resumable daily keyword collection into a separate MySQL database."""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from crawl_workflow import WorkflowLock, now, write_json
from local_config import read_config

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2026-09-24')
    parser.add_argument('--end', default='2026-10-07', help='Inclusive final day')
    parser.add_argument('--keyword', default='第五人格')
    parser.add_argument('--database', default='weibo_identityv_20260924_20261007')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--delay', type=float, default=5)
    parser.add_argument('--max-requests', type=int, default=100000)
    parser.add_argument('--backend', choices=['browser', 'http'], default='browser')
    parser.add_argument('--profile', type=Path,
        default=PROJECT / 'output/browser-comments/browser-profile')
    parser.add_argument('--browser-python', type=Path,
        default=PROJECT / '.browser-venv/Scripts/python.exe')
    parser.add_argument('--access-restored', choices=['search', 'detail', 'longtext'],
        help='Explicitly confirm normal access is restored before replaying a refused route')
    args = parser.parse_args()
    first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if first > last or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,63}', args.database):
        parser.error('Invalid date range or database name')
    if args.delay < 1 or args.max_requests < 1:
        parser.error('Positive request budget and delay >= 1 required')
    home = args.output or PROJECT / 'output' / args.database
    home = home.resolve()
    config = read_config()
    os.environ.update(config)
    os.environ['WEIBO_DB_NAME'] = args.database
    import pymysql
    with WorkflowLock(home):
        state_path = home / 'daily-state.json'
        identity = dict(database=args.database, keyword=args.keyword, start=args.start, end=args.end)
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if any(state.get(k) != v for k, v in identity.items()):
                raise ValueError('Saved collection configuration mismatch')
        else:
            days = []
            current = first
            while current <= last:
                days.append(dict(date=current.isoformat(), status='pending', attempts=0))
                current += timedelta(days=1)
            state = dict(**identity, created_at=now(), status='ready', days=days)
        db = pymysql.connect(host=config.get('WEIBO_DB_HOST', '127.0.0.1'),
            port=int(config.get('WEIBO_DB_PORT', 3306)), user=config.get('WEIBO_DB_USER'),
            password=config.get('WEIBO_DB_PASSWORD', ''), charset='utf8mb4',
            connect_timeout=10, autocommit=True)
        try:
            with db.cursor() as cur:
                cur.execute(f'CREATE DATABASE IF NOT EXISTS `{args.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
                db.select_db(args.database)
                cur.execute('''CREATE TABLE IF NOT EXISTS crawl_items (
                    spider VARCHAR(64) NOT NULL, item_id VARCHAR(128) NOT NULL,
                    keyword VARCHAR(255) NOT NULL DEFAULT '', crawl_time BIGINT NOT NULL,
                    payload JSON NOT NULL, PRIMARY KEY(spider,item_id,keyword),
                    KEY idx_crawl_time(crawl_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
            if args.backend == 'browser' and (not args.browser_python.is_file() or not args.profile.is_dir()):
                raise ValueError('Existing browser runtime and logged-in profile are required')
            state.setdefault('events', []).append(dict(at=now(), event='collection_started', backend=args.backend))
            state.update(status='running', updated_at=now(), backend=args.backend)
            write_json(state_path, state)
            for day in state['days']:
                if day['status'] == 'complete':
                    continue
                folder = home / day['date']
                folder.mkdir(parents=True, exist_ok=True)
                day.update(status='running', attempts=day['attempts'] + 1, started_at=now(), backend=args.backend)
                job_path = folder / 'job.json'
                result_path = folder / f"result-{day['attempts']}.json"
                job = dict(kind='tweet', keyword=args.keyword, start=day['date'],
                    end=(date.fromisoformat(day['date']) + timedelta(days=1)).isoformat(),
                    round_id=f"{args.database}-{day['date']}", delay=args.delay,
                    max_requests=args.max_requests, split_hours=24)
                job['access_restored'] = args.access_restored
                write_json(job_path, job)
                write_json(state_path, state)
                print(f"{now()} collecting {day['date']}", flush=True)
                if args.backend == 'browser':
                    command = [str(args.browser_python.resolve()),
                        str(Path(__file__).with_name('run_browser_tweets.py')),
                        '--job', str(job_path), '--result', str(result_path),
                        '--output', str(folder / 'browser'), '--profile', str(args.profile.resolve()),
                        '--cache', str(home / 'post-date-cache.json')]
                else:
                    command = [sys.executable, str(Path(__file__).with_name('daily_request_worker.py')),
                        '--job', str(job_path), '--result', str(result_path)]
                with (folder / f"worker-{day['attempts']}.txt").open('w', encoding='utf-8') as output:
                    child = subprocess.run(command,
                        cwd=Path(__file__).parent, stdout=output, stderr=subprocess.STDOUT)
                result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
                complete = child.returncode == 0 and result.get('complete') is True
                with db.cursor() as cur:
                    cur.execute("SELECT COUNT(DISTINCT item_id) FROM crawl_items WHERE keyword=%s AND "
                        "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.created_at')) >= %s AND "
                        "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.created_at')) < %s",
                        (args.keyword, job['start'] + ' 00:00:00', job['end'] + ' 00:00:00'))
                    count = cur.fetchone()[0]
                day.update(status='complete' if complete else 'needs_attention', finished_at=now(),
                    saved_tweets=count, result=str(result_path), returncode=child.returncode,
                    stop_reason=('finished' if complete else result.get('stop_reason')
                        or result.get('stats', {}).get('finish_reason', 'worker_failed')))
                state.update(updated_at=now())
                if not complete:
                    state['status'] = 'needs_attention'
                    write_json(state_path, state)
                    print(json.dumps(day, ensure_ascii=False), flush=True)
                    return 5
                write_json(state_path, state)
            state.update(status='complete', finished_at=now())
            write_json(state_path, state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
            return 0
        finally:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
