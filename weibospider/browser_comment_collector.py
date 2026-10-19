"""Recoverable page-based collection. Never calls a comment API or reads credentials."""
import asyncio
from dataclasses import dataclass

from browser_comment_store import record_key


class CollectionStopped(Exception):
    pass


@dataclass
class BrowserLimits:
    max_passes: int = 500
    idle_passes: int = 4
    interval: float = 2.0
    max_reply_threads: int = 1000


class BrowserCommentCollector:
    def __init__(self, driver, store, limits=None, export_path=None):
        self.driver, self.store = driver, store
        self.limits = limits or BrowserLimits()
        self.export_path = export_path

    def flush(self):
        if self.export_path:
            self.store.export(self.export_path)

    async def run(self, targets):
        for target in targets:
            mid = target['tweet_id']
            if self.store.target_status(mid) == 'visible_exhausted':
                continue
            self.store.mark_target(mid, 'running')
            try:
                await self.driver.open(target['url'])
                await self.collect_post(mid)
            except CollectionStopped as exc:
                self.store.mark_target(mid, 'needs_attention', str(exc))
                self.flush()
                break
            except asyncio.CancelledError:
                self.store.mark_target(mid, 'incomplete', 'interrupted')
                self.flush()
                raise
            except Exception as exc:
                # Exception messages can contain request details. Persist type only.
                self.store.mark_target(mid, 'incomplete', type(exc).__name__)
                self.flush()
                break
            self.flush()

    @staticmethod
    def check_access(snapshot):
        if snapshot.get('verification_visible'):
            raise CollectionStopped('manual_verification_required')
        if snapshot.get('login_required'):
            raise CollectionStopped('manual_login_required')

    async def collect_post(self, mid):
        idle = 0
        checked_threads = set()
        observed_keys = set()
        for passes in range(1, self.limits.max_passes + 1):
            snap = await self.driver.snapshot()
            self.check_access(snap)
            keys = {record_key(mid, r) for r in snap['comments']}
            progressing = bool(keys - observed_keys)
            observed_keys.update(keys)
            self.store.save(mid, snap['comments'])
            self.store.mark_stream(mid, '', 'running', passes=passes)
            self.flush()
            idle = 0 if progressing else idle + 1
            # Virtualized rows disappear after scrolling: save them before any action.
            for root in snap['comments']:
                if not root.get('reply_count_display'):
                    continue
                key = record_key(mid, root)
                if self.store.stream_status(mid, key) == 'visible_exhausted' or key in checked_threads:
                    continue
                if len(checked_threads) >= self.limits.max_reply_threads:
                    self.store.mark_stream(mid, key, 'incomplete', 'reply_thread_budget')
                    continue
                checked_threads.add(key)
                # Re-find by visible identity; indexes from an old render are unsafe.
                opened = await self.driver.open_replies(root)
                if not opened:
                    self.store.mark_stream(mid, key, 'incomplete', 'reply_link_unavailable')
                    continue
                try:
                    await self.collect_replies(mid, key)
                finally:
                    await self.driver.close_replies()
            # The end marker must be visible in the viewport, not merely in the DOM.
            after = await self.driver.snapshot()
            self.check_access(after)
            # A delayed render may add rows while reply dialogs are closing.
            if {record_key(mid, r) for r in after['comments']} - observed_keys:
                continue
            if after.get('roots_end_visible'):
                self.store.mark_stream(mid, '', 'visible_exhausted', passes=passes)
                missing = self.store.unfinished_threads(mid)
                truncated = any(r.get('content_truncated') for r in self.store.records(mid))
                status = 'incomplete' if missing or truncated else 'visible_exhausted'
                reason = 'unfinished_reply_threads' if missing else ('truncated_content' if truncated else None)
                self.store.mark_target(mid, status, reason)
                return
            if idle >= self.limits.idle_passes:
                self.store.mark_stream(mid, '', 'incomplete', 'stalled_without_end_marker', passes)
                self.store.mark_target(mid, 'incomplete', 'stalled_without_end_marker')
                return
            await self.driver.scroll('roots')
            await asyncio.sleep(self.limits.interval)
        self.store.mark_stream(mid, '', 'incomplete', 'scroll_budget', passes)
        self.store.mark_target(mid, 'incomplete', 'scroll_budget')

    async def collect_replies(self, mid, root_key):
        idle = 0
        observed_keys = set()
        for passes in range(1, self.limits.max_passes + 1):
            snap = await self.driver.snapshot()
            self.check_access(snap)
            keys = {record_key(mid, r, root_key) for r in snap['replies']}
            progressing = bool(keys - observed_keys)
            observed_keys.update(keys)
            self.store.save(mid, snap['replies'], root_key)
            idle = 0 if progressing else idle + 1
            self.store.mark_stream(mid, root_key, 'running', passes=passes)
            self.flush()
            if snap.get('replies_end_visible'):
                self.store.mark_stream(mid, root_key, 'visible_exhausted', passes=passes)
                return
            if idle >= self.limits.idle_passes:
                self.store.mark_stream(mid, root_key, 'incomplete', 'stalled_without_end_marker', passes)
                return
            await self.driver.scroll('replies')
            await asyncio.sleep(self.limits.interval)
        self.store.mark_stream(mid, root_key, 'incomplete', 'scroll_budget', passes)
