"""Transactional browser records/checkpoints; synthetic keys are never platform IDs."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_targets(targets):
    import re
    unique = {}
    for target in targets:
        mid, url = str(target.get('tweet_id', '')), target.get('url', '')
        parsed = urlparse(url)
        if (not mid.isdigit() or parsed.scheme != 'https' or parsed.netloc != 'weibo.com'
                or not re.fullmatch(r'/(?:\d+/[a-zA-Z0-9]+|detail/\d+)', parsed.path)
                or parsed.query or parsed.fragment):
            raise ValueError('Targets require a numeric tweet_id and a public HTTPS weibo.com post URL')
        if mid in unique and unique[mid]['url'] != url:
            raise ValueError('Conflicting URLs for the same tweet ID')
        unique[mid] = {**target, 'tweet_id': mid}
    return list(unique.values())


def record_key(tweet_id, record, root_key=''):
    values = [str(tweet_id), record.get('comment_level'), root_key,
              record.get('user_id'), record.get('created_at_display'), record.get('content')]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode('utf-8')).hexdigest()


class BrowserCommentStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS targets (
              tweet_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', reason TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments (
              record_key TEXT PRIMARY KEY, tweet_id TEXT NOT NULL, root_record_key TEXT,
              payload TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS streams (
              tweet_id TEXT NOT NULL, root_record_key TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, reason TEXT, passes INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL, PRIMARY KEY(tweet_id, root_record_key));
        ''')

    def register(self, targets, refresh=False):
        with self.conn:
            for t in targets:
                self.conn.execute('INSERT OR IGNORE INTO targets(tweet_id,payload,updated_at) VALUES (?,?,?)',
                                  (t['tweet_id'], json.dumps(t, ensure_ascii=False), utc_now()))
                if refresh:
                    self.conn.execute("UPDATE targets SET status='pending',reason=NULL WHERE tweet_id=?", (t['tweet_id'],))
                    self.conn.execute("UPDATE streams SET status='pending',reason=NULL WHERE tweet_id=?", (t['tweet_id'],))

    def target_status(self, mid):
        return self.conn.execute('SELECT status FROM targets WHERE tweet_id=?', (mid,)).fetchone()[0]

    def mark_target(self, mid, status, reason=None):
        with self.conn:
            self.conn.execute('UPDATE targets SET status=?,reason=?,updated_at=? WHERE tweet_id=?',
                              (status, reason, utc_now(), mid))

    def stream_status(self, mid, root_key=''):
        row = self.conn.execute('SELECT status FROM streams WHERE tweet_id=? AND root_record_key=?',
                                (mid, root_key)).fetchone()
        return row[0] if row else 'pending'

    def mark_stream(self, mid, root_key, status, reason=None, passes=0):
        with self.conn:
            self.conn.execute('''INSERT INTO streams VALUES (?,?,?,?,?,?)
              ON CONFLICT(tweet_id,root_record_key) DO UPDATE SET status=excluded.status,
              reason=excluded.reason,passes=excluded.passes,updated_at=excluded.updated_at''',
                              (mid, root_key, status, reason, passes, utc_now()))

    def save(self, mid, records, root_key=''):
        new = 0
        with self.conn:
            for record in records:
                if not record.get('user_id') or not record.get('content'):
                    continue
                key = record_key(mid, record, root_key)
                payload = {k: v for k, v in record.items() if k != 'dom_index'}
                payload.update(tweet_id=mid, record_key=key, root_record_key=root_key or None,
                               source='browser_visible_dom', identity_source='dom_fingerprint',
                               platform_comment_id_available=False)
                now = utc_now()
                inserted = self.conn.execute('INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?,?)',
                    (key, mid, root_key or None, json.dumps(payload, ensure_ascii=False), now, now)).rowcount
                new += inserted
                if not inserted:
                    self.conn.execute('UPDATE comments SET last_seen=?,payload=? WHERE record_key=?',
                                      (now, json.dumps(payload, ensure_ascii=False), key))
        return new

    def records(self, mid, level=None):
        rows = self.conn.execute('SELECT payload FROM comments WHERE tweet_id=? ORDER BY first_seen,record_key', (mid,))
        values = [json.loads(r[0]) for r in rows]
        return values if level is None else [r for r in values if r['comment_level'] == level]

    def unfinished_threads(self, mid):
        return [r for r in self.records(mid, 1) if r.get('reply_count_display', 0) > 0
                and self.stream_status(mid, r['record_key']) != 'visible_exhausted']

    def export(self, path):
        pages = []
        for row in self.conn.execute('SELECT * FROM targets ORDER BY rowid'):
            mid = row['tweet_id']
            streams = [dict(r) for r in self.conn.execute('SELECT * FROM streams WHERE tweet_id=?', (mid,))]
            pages.append({**json.loads(row['payload']), 'status': row['status'], 'reason': row['reason'],
                          'historical_total_complete': None, 'streams': streams, 'comments': self.records(mid)})
        payload = {'schema_version': 1, 'exported_at': utc_now(),
                   'coverage': 'browser_visible_only',
                   'identity_note': 'DOM fingerprints can merge identical same-user same-time text; not platform IDs',
                   'targets': pages}
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix(dest.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(dest)
        return payload

    def close(self):
        self.conn.close()
