"""Check prospective Git files and optional history without printing secrets."""
import argparse
import json
from pathlib import Path
import subprocess

from local_config import read_config
from publish_safety import private_path, known_secret_values, content_findings


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def check(root, history=False, config=None):
    secrets = known_secret_values(read_config(project=root) if config is None else config)
    findings = []
    names = set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8').split('\0')) - {''}
    for name in sorted(names):
        path = root / name
        if not path.exists():
            continue
        if private_path(name):
            findings.append({'path': name, 'reason': 'private file included in prospective upload'})
        elif path.is_symlink():
            findings.append({'path': name, 'reason': 'symlink requires manual review'})
        else:
            for reason in content_findings(path.read_bytes(), secrets):
                findings.append({'path': name, 'reason': reason})
    blobs = 0
    if history:
        seen = set()
        for line in git(root, 'rev-list', '--objects', '--all').decode('utf-8').splitlines():
            oid, _, name = line.partition(' ')
            if not name or oid in seen:
                continue
            seen.add(oid)
            if git(root, 'cat-file', '-t', oid).strip() != b'blob':
                continue
            blobs += 1
            if private_path(name):
                findings.append({'history_object': oid[:12], 'path': name, 'reason': 'private file in Git history'})
            else:
                for reason in content_findings(git(root, 'cat-file', 'blob', oid), secrets):
                    findings.append({'history_object': oid[:12], 'path': name, 'reason': reason})
    return {'passed': not findings, 'prospective_files_checked': len(names), 'history_blobs_checked': blobs,
            'findings': findings, 'note': 'Heuristic check plus known local secrets; not a guarantee against every possible secret'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--history', action='store_true')
    args = parser.parse_args()
    result = check(args.root.resolve(), args.history)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
