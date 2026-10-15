"""Shared target selection and honest run outcome checks (no network access)."""
import json


def select_targets(rows, since=None, limit=None):
    """Select unique stored tweets with comments, newest collection first."""
    targets = {}
    for spider, mid, crawl_time, payload in rows:
        if not spider.startswith('tweet') or (since is not None and crawl_time < since):
            continue
        data = json.loads(payload) if isinstance(payload, str) else payload
        author = str(data.get('user', {}).get('_id') or '')
        try:
            count = int(data.get('comments_count') or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0 or not str(mid).isdigit() or not author.isdigit():
            continue
        target = {'tweet_id': str(mid), 'author_id': author, 'url': data.get('url'),
                  'mblogid': data.get('mblogid'), 'published': data.get('created_at'),
                  'crawl_time': crawl_time, 'comments_count_at_tweet_crawl': count}
        if str(mid) not in targets or crawl_time > targets[str(mid)]['crawl_time']:
            targets[str(mid)] = target
    ordered = sorted(targets.values(), key=lambda d: (d['crawl_time'], d['tweet_id']), reverse=True)
    return ordered if limit is None else ordered[:limit]


def run_outcome(spider, stats, captured_count, parse_errors=(), existing_rows_unchanged=True):
    """Cursor exhaustion means exposed streams ended, not all historical comments exist."""
    failures = [event for event in spider.audit if event['event'] != 'page']
    streams_ended = bool(spider.streams) and all(s == 'exhausted' for s in spider.streams.values())
    roots_ended = all(spider.streams.get((str(mid), None)) == 'exhausted' for mid in spider.tweet_ids)
    validated = (stats.get('finish_reason') == 'finished' and not spider.access_limited
                 and not failures and not parse_errors and streams_ended and roots_ended
                 and existing_rows_unchanged and not stats.get('database/write_failures', 0)
                 and captured_count == len(spider.seen_items))
    if validated and captured_count:
        outcome = 'visible_streams_exhausted'
    elif validated:
        outcome = 'empty_visible_result'
    else:
        outcome = 'incomplete_or_failed'
    return {'outcome': outcome, 'collection_verified': validated and captured_count > 0,
            'visible_streams_exhausted': validated, 'historical_total_complete': None}
