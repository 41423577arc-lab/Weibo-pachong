"""Export clean current code into a new local Git repository without old history."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from check_publish import check, git
from local_config import PROJECT, read_config
from publish_safety import private_path, known_secret_values, content_findings


def prepare(root, destination):
    root, destination = root.resolve(), destination.resolve()
    if destination == root or destination in root.parents:
        raise ValueError('The export destination must not be the project or its parent')
    if destination.exists():
        raise ValueError('Use a new export directory; existing files are never replaced')
    config = read_config(project=root)
    secrets = known_secret_values(config)
    files = []
    for name in sorted(set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8').split('\0')) - {''}):
        path = root / name
        if private_path(name) or not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Export refused a symlink or external path: ' + name)
        reasons = content_findings(path.read_bytes(), secrets)
        if reasons:
            raise ValueError('Export blocked: ' + name + ' (' + ', '.join(reasons) + ')')
        files.append((name, path))
    destination.mkdir(parents=True)
    for name, source in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    subprocess.run(['git', 'init', '-b', 'main', str(destination)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(destination), 'add', '--all'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(destination), 'config', 'core.hooksPath', '.githooks'], check=True, capture_output=True)
    if (destination / '.githooks/pre-push').exists():
        subprocess.run(['git', '-C', str(destination), 'update-index', '--chmod=+x', '.githooks/pre-push'],
                       check=True, capture_output=True)
    result = check(destination, history=True, config=config)
    if not result['passed']:
        raise ValueError('Export failed the final safety check; do not upload this directory')
    return {'directory': str(destination), 'files_exported': len(files), 'old_history_included': False,
            'credentials_included': False, 'committed': False, 'pushed': False, 'check': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PROJECT.parent / 'publish/WeiboSpider')
    args = parser.parse_args()
    try:
        result = prepare(PROJECT, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Git export failed; no credential output logged')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
