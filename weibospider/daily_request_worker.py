"""Use the existing checkpointed HTTP collector for one daily job."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import history_collect as history


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding='utf-8'))
    history.KEYWORDS = [job['keyword']]
    history.DATES = [job['start']]
    history.START = job['start'] + ' 00:00:00'
    history.END = job['end'] + ' 00:00:00'
    history.STATE_PATH = args.job.parent / 'http-state.json'
    options = SimpleNamespace(access_restored=job.get('access_restored'), max_requests=job['max_requests'],
        max_seconds=86400, max_pages=100000, delay=job['delay'],
        inventory_only=False, pilot_only=False)
    collector = history.Collector(options)
    if collector.state['blocked_routes']:
        history.write_json(args.result, dict(complete=False,
            stats=dict(finish_reason='saved_access_limit_requires_manual_resolution')))
        collector.db.close()
        return 5
    collector.run()
    state = collector.state
    omissions = (state['queue'] or state['deferred_tasks'] or state['blocked_routes'] or
        any(e['event'] in ('detail_parse_error', 'pagination_loop', 'page_cap_pending')
            for e in state['events']))
    complete = not omissions and state.get('stop_reason') == 'queue_exhausted_with_recorded_omissions'
    result = dict(complete=complete, stats=dict(finish_reason='finished' if complete else state.get('stop_reason'),
        requests=state['requests'], http_status=state['http_status']),
        blocked_routes=state['blocked_routes'], pending_tasks=len(state['queue']),
        deferred_tasks=len(state['deferred_tasks']), historical_total_complete=None)
    history.write_json(args.result, result)
    return 0 if complete else 5


if __name__ == '__main__':
    raise SystemExit(main())
