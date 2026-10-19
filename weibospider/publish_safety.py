"""Conservative upload checks; findings never contain credential values."""
import fnmatch
from pathlib import PurePosixPath
import re

PRIVATE_DIRS = {'.git', '.venv', '.browser-venv', '__pycache__', 'output', 'logs',
                'browser-profile', 'browser_profile', 'user-data-dir', 'user data',
                'publish', '.secrets', '.credentials'}
PRIVATE_FILES = {'cookie.txt', 'cookies.txt', 'cookies.json', 'credentials.json',
                 'cookies', 'login data', 'local state', 'cookie.png'}


def private_path(name):
    path = PurePosixPath(name.replace('\\', '/'))
    parts = [part.lower() for part in path.parts]
    if any(part in PRIVATE_DIRS for part in parts[:-1]):
        return True
    base = parts[-1] if parts else ''
    if base in PRIVATE_FILES:
        return True
    if base in ('local-secrets.example.json', '.env.example'):
        return False
    return (base == 'local-secrets.json' or fnmatch.fnmatch(base, 'local-secrets.*.json')
            or base == '.env' or base.startswith('.env.') or
            any(fnmatch.fnmatch(base, pattern) for pattern in
                ('*.pem', '*.key', '*.p12', '*.pfx', '*.sqlite*', '*.db', '*.log', '*.dump', '*.sql.gz', '*.bak', '*.zip')))


def known_secret_values(config):
    values = []
    for key in ('WEIBO_COOKIE', 'WEIBO_DB_PASSWORD'):
        value = str(config.get(key) or '')
        if value:
            values.append((key, value.encode('utf-8')))
    for part in str(config.get('WEIBO_COOKIE') or '').split(';'):
        name, sep, value = part.strip().partition('=')
        if sep and len(value) >= 12:
            values.append(('Cookie component ' + name, value.encode('utf-8')))
    return values


def content_findings(data, secrets=()):
    findings = {label for label, value in secrets if value and value in data}
    text = data.decode('utf-8', errors='replace')
    if re.search(r'\b(?:SUB|SUBP|SCF|WBPSESS|XSRF-TOKEN)\s*=\s*[A-Za-z0-9%._~+/=-]{16,}', text):
        findings.add('cookie-shaped literal')
    assignment = re.compile(r'''(?:WEIBO_DB_PASSWORD|password|passwd|api_key|secret_key)["']?\s*[:=]\s*["']([^"'\r\n]+)["']''', re.I)
    for match in assignment.finditer(text):
        value = match.group(1)
        if value.lower() not in {'test-password', 'example', 'your_password', 'password', 'placeholder'}:
            findings.add('possible hardcoded credential')
    return sorted(findings)
