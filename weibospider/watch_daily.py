"""Display daily collection checkpoints and live database counts in a terminal."""
import json
from pathlib import Path
import time

from local_config import read_config
from workflow_store import connect

HOME = Path(__file__).resolve().parents[1] / 'output/weibo_identityv_20260924_20261007'


def main():
    import os
    config = read_config()
    os.environ.update(config)
    os.environ['WEIBO_DB_NAME'] = 'weibo_identityv_20260924_20261007'
    print('Weibo daily crawler | keyword: 第五人格 | 2026-09-24 through 2026-10-07', flush=True)
    print('Read-only monitor; Ctrl+C closes monitoring. The crawler keeps running.', flush=True)
    while True:
        try:
            state = json.loads((HOME / 'daily-state.json').read_text(encoding='utf-8'))
            current = next((d for d in state['days'] if d['status'] == 'running'), None)
            completed = sum(d['status'] == 'complete' for d in state['days'])
            db = connect()
            try:
                with db.cursor() as cur:
                    cur.execute('SELECT COUNT(DISTINCT item_id) FROM crawl_items')
                    saved = cur.fetchone()[0]
            finally:
                db.close()
            message = f"{time.strftime('%H:%M:%S')} status={state['status']} days={completed}/14 total_tweets={saved}"
            if current:
                checkpoint = HOME / current['date'] / 'browser/browser-tweets-state.json'
                message += f" current_day={current['date']}"
                if checkpoint.exists():
                    browser = json.loads(checkpoint.read_text(encoding='utf-8'))
                    message += (f" search_pages={len(browser['seen_pages'])}"
                        f" browser_saved_today={len(browser['saved_posts'])}"
                        f" queued_tasks={len(browser['queue'])} stop={browser.get('stop_reason')}")
            else:
                attention = next((d for d in state['days'] if d['status'] == 'needs_attention'), None)
                if attention:
                    message += f" day={attention['date']} reason={attention.get('stop_reason')}"
            print(message, flush=True)
        except Exception as exc:
            print(f'Monitor: {type(exc).__name__}; retrying in 5 seconds', flush=True)
        time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nMonitor closed. Collection was not interrupted.')
