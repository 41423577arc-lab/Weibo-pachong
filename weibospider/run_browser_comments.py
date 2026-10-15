"""Run a dedicated Chrome profile and collect only normally rendered comments."""
from local_config import read_config
import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from browser_comment_collector import BrowserCommentCollector, BrowserLimits, CollectionStopped
from browser_comment_store import BrowserCommentStore, validate_targets


PROJECT = Path(__file__).resolve().parents[1]


class PlaywrightDriver:
    def __init__(self, page, timeout_ms=30000):
        self.page, self.timeout_ms = page, timeout_ms
        self.expected_url = None
        source = Path(__file__).with_name('browser_comments_dom.mjs').read_text(encoding='utf-8')
        self.extractor = '() => {\n' + source.replace('export function ', 'function ') + '\nreturn readVisibleComments();\n}'

    async def open(self, url):
        self.expected_url = url
        try:
            response = await self.page.goto(url, wait_until='domcontentloaded', timeout=self.timeout_ms)
        except Exception as exc:
            # Preserve only the browser network error code, never raw request details.
            error = re.search(r'net::(ERR_[A-Z_]+)', str(exc))
            if error:
                raise CollectionStopped('browser_navigation_' + error.group(1)) from None
            raise
        if response and response.status in (401, 403, 418, 429):
            raise CollectionStopped(f'navigation_access_limited_{response.status}')
        await self.ready(False)

    async def snapshot(self):
        host = urlparse(self.page.url).hostname
        if urlparse(self.page.url).scheme == 'chrome-error':
            raise CollectionStopped('browser_error_page')
        if host not in ('weibo.com', 'www.weibo.com'):
            raise CollectionStopped('unexpected_destination_or_login_redirect')
        if self.expected_url and urlparse(self.page.url).path != urlparse(self.expected_url).path:
            raise CollectionStopped('post_redirected_login_or_removed')
        return await self.page.evaluate(self.extractor)

    async def ready(self, replies):
        for _ in range(max(1, self.timeout_ms // 500)):
            snap = await self.snapshot()
            BrowserCommentCollector.check_access(snap)
            if replies:
                if snap['replies'] or snap['replies_end_visible']:
                    return
            elif snap['comments'] or snap['all_loaded'] or snap.get('no_comments_rendered') or snap.get('no_comments_visible'):
                return
            await asyncio.sleep(0.5)
        raise CollectionStopped('comments_not_loaded_login_or_layout_check_required')

    async def open_replies(self, root):
        snap = await self.snapshot()
        match = next((r for r in snap['comments'] if
                      (r['user_id'], r['content'], r['created_at_display']) ==
                      (root['user_id'], root['content'], root['created_at_display'])), None)
        if match is None:
            return False
        row = self.page.locator('main .item1').nth(match['dom_index'])
        link = row.get_by_role('link', name=re.compile(r'^共\d+条回复'))
        if await link.count() != 1:
            return False
        await link.click(timeout=self.timeout_ms)
        await self.ready(True)
        return True

    async def close_replies(self):
        if not await self.page.locator('.wbpro-layer').count():
            return
        await self.page.keyboard.press('Escape')
        if await self.page.locator('.wbpro-layer').count():
            # The mask is observed alongside woo-modal-main. Click its outside corner.
            await self.page.locator('.woo-modal-mask').last.click(position={'x': 4, 'y': 4}, timeout=self.timeout_ms)
        await self.page.locator('.wbpro-layer').wait_for(state='hidden', timeout=self.timeout_ms)

    async def scroll(self, stream):
        selector = '.wbpro-layer .item2' if stream == 'replies' else 'main .item1'
        rows = self.page.locator(selector)
        if await rows.count():
            await rows.last.hover(timeout=self.timeout_ms)
        else:
            await self.page.locator('main').hover(timeout=self.timeout_ms)
        await self.page.mouse.wheel(0, 550)


def load_targets(args):
    if args.from_db:
        from comment_run import select_targets
        from datetime import datetime
        import pymysql
        config = read_config()
        # Database settings only: no browser Cookie is loaded into the program.
        conn = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
                              port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
                              password=config['WEIBO_DB_PASSWORD'], database='weibo_data',
                              charset='utf8mb4', connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT spider,item_id,crawl_time,payload FROM crawl_items WHERE spider LIKE 'tweet%'")
                since = datetime.strptime(args.since, '%Y-%m-%d').timestamp() if args.since else None
                targets = select_targets(cur.fetchall(), since=since)
        finally:
            conn.close()
    else:
        targets = json.loads(args.targets.read_text(encoding='utf-8'))
    targets = validate_targets(targets)
    return targets if args.limit is None else targets[:args.limit]


class RunLock:
    """OS releases the lock after crashes; no stale PID file blocks resume."""
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another collector is using this output directory') from None
        return self

    def __exit__(self, *args):
        self.file.close()


async def run(args, targets):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('Install requirements-browser.txt in your active Python environment first.')
        return 2
    profile = args.profile or args.output / 'browser-profile'
    resolved = profile.resolve().as_posix().lower()
    if '/chrome/user data' in resolved or '/edge/user data' in resolved:
        print('Use a dedicated collector profile, not your everyday browser profile.')
        return 2
    with RunLock(args.output / 'collector.lock'):
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), channel='chrome', headless=args.headless,
                accept_downloads=False, viewport={'width': 1280, 'height': 900})
            try:
                page = await context.new_page()
                if args.login:
                    try:
                        await page.goto('https://weibo.com/', wait_until='domcontentloaded')
                    except Exception as exc:
                        error = re.search(r'net::(ERR_[A-Z_]+)', str(exc))
                        print('Chrome navigation failed: ' + (error.group(1) if error else type(exc).__name__))
                        print('Browser remains open. Check whether you can open weibo.com manually.')
                    print('Complete login manually in this Chrome window. Press Enter here when done.')
                    await asyncio.to_thread(input)
                    return 0
                store_class = BrowserCommentStore
                if getattr(args, 'mysql', False):
                    from workflow_store import MirroredBrowserCommentStore
                    store_class = MirroredBrowserCommentStore
                store = store_class(args.output / 'comments.sqlite3')
                try:
                    store.register(targets, refresh=args.refresh)
                    collector = BrowserCommentCollector(PlaywrightDriver(page, args.timeout * 1000), store,
                        BrowserLimits(args.max_passes, args.idle_passes, args.interval, args.max_reply_threads),
                        args.output / 'comments.json')
                    try:
                        await collector.run(targets)
                    finally:
                        result = store.export(args.output / 'comments.json')
                    selected = [r for r in result['targets'] if r['tweet_id'] in {t['tweet_id'] for t in targets}]
                    summary = {'targets': len(selected),
                               'visible_comments_and_replies': sum(len(r['comments']) for r in selected),
                               'targets_exhausted': sum(r['status'] == 'visible_exhausted' for r in selected),
                               'targets_needing_attention': sum(r['status'] != 'visible_exhausted' for r in selected),
                               'historical_total_complete': None,
                               'output': str(args.output / 'comments.json')}
                    print(json.dumps(summary, ensure_ascii=False))
                    return 0 if all(r['status'] == 'visible_exhausted' for r in selected) else 5
                finally:
                    store.close()
            finally:
                await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', type=Path, default=PROJECT / 'logs' / 'comment-targets.json')
    parser.add_argument('--from-db', action='store_true', help='Read existing MySQL tweets; never write MySQL')
    parser.add_argument('--since', help='Earliest collection date, used with --from-db')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', type=Path, default=PROJECT / 'output' / 'browser-comments')
    parser.add_argument('--profile', type=Path, help='Dedicated Chrome profile; defaults under output')
    parser.add_argument('--login', action='store_true', help='Open Chrome for manual login, without collecting')
    parser.add_argument('--preview', action='store_true', help='List targets without starting a browser')
    parser.add_argument('--refresh', action='store_true', help='Revisit completed targets, retaining previous records')
    parser.add_argument('--mysql', action='store_true', help='Mirror every saved comment/reply to the main MySQL database')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--max-passes', type=int, default=500)
    parser.add_argument('--idle-passes', type=int, default=4)
    parser.add_argument('--max-reply-threads', type=int, default=1000)
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--timeout', type=int, default=30, help='Page operation timeout in seconds')
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or min(args.max_passes, args.idle_passes, args.timeout) < 1 or args.max_reply_threads < 0 or args.interval < 0.5:
        parser.error('Invalid limits; interval must be at least 0.5 seconds')
    if args.login and args.headless:
        parser.error('Manual login requires a visible browser')
    try:
        targets = [] if args.login else load_targets(args)
        if args.preview:
            print(json.dumps({'target_count': len(targets), 'targets': targets}, ensure_ascii=False))
            return 0
        if not targets and not args.login:
            print('No targets with stored comment counts were found.')
            return 5
        return asyncio.run(run(args, targets))
    except KeyboardInterrupt:
        print('Interrupted. Saved records remain; rerun the same command to resume.')
        return 130
    except Exception as exc:
        print(f'Collector stopped: {type(exc).__name__}. No credentials or raw request details logged.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
