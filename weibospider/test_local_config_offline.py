"""Configuration isolation and clean publication checks using temporary repos."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from local_config import read_config
from publish_safety import private_path, known_secret_values, content_findings
from prepare_publish import prepare
from check_publish import check


class ConfigurationTests(unittest.TestCase):
    def test_parent_compatibility_root_precedence_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'project'
            root.mkdir()
            (parent / 'local-secrets.json').write_text(json.dumps({'WEIBO_DB_USER':'legacy'}), encoding='utf-8')
            self.assertEqual(read_config(project=root, environ={})['WEIBO_DB_USER'], 'legacy')
            (root / 'local-secrets.json').write_text(json.dumps({'WEIBO_DB_USER':'project'}), encoding='utf-8-sig')
            self.assertEqual(read_config(project=root, environ={})['WEIBO_DB_USER'], 'project')
            self.assertEqual(read_config(project=root, environ={'WEIBO_DB_USER':'environment'})['WEIBO_DB_USER'], 'environment')

    def test_explicit_missing_config_does_not_silently_use_other_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                read_config(root / 'missing.json', project=root, environ={})
            (root / 'bad.json').write_text('{"private":invalid}', encoding='utf-8')
            with self.assertRaises(ValueError) as raised:
                read_config(root / 'bad.json', project=root, environ={})
            self.assertNotIn('private', str(raised.exception))

    def test_no_config_is_valid_for_import_and_legacy_cookie_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            (root / 'weibospider').mkdir(parents=True)
            self.assertEqual(read_config(project=root, environ={}), {})
            (root / 'weibospider/cookie.txt').write_text('synthetic-cookie', encoding='utf-8')
            self.assertEqual(read_config(project=root, environ={})['WEIBO_COOKIE'], 'synthetic-cookie')
            self.assertEqual(read_config(project=root, environ={'WEIBO_COOKIE':''})['WEIBO_COOKIE'], '')

    def test_private_paths_and_public_template(self):
        for name in ['local-secrets.json', 'weibospider/cookie.txt', 'output/x.json',
                     'arbitrary/browser-profile/Default/Cookies', '.env.production', '.github/cookie.png']:
            self.assertTrue(private_path(name), name)
        self.assertFalse(private_path('local-secrets.example.json'))
        self.assertFalse(private_path('database.sql'))

    def test_secret_findings_do_not_echo_values(self):
        secret = '-'.join(['synthetic', 'database', 'credential'])
        config = dict(zip(['WEIBO_DB_PASSWORD'], [secret]))
        result = content_findings(secret.encode(), known_secret_values(config))
        self.assertIn('WEIBO_DB_PASSWORD', result)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn('cookie-shaped literal', content_findings(('SUB=' + 'x' * 40).encode()))

    def test_export_has_no_old_history_or_private_files_and_source_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            root.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            git('init', '-b', 'main')
            (root / '.gitignore').write_text('local-secrets.json\noutput/\n', encoding='utf-8')
            (root / 'README.md').write_text('Public code', encoding='utf-8')
            (root / 'cookie.txt').write_text('SUB=' + 'z' * 40, encoding='utf-8')
            (root / 'local-secrets.json').write_text('{}', encoding='utf-8')
            git('add', '.gitignore', 'README.md', 'cookie.txt')
            git('-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid', 'commit', '-m', 'fixture')
            self.assertFalse(check(root, history=True, config={})['passed'])
            target = Path(directory) / 'clean'
            result = prepare(root, target)
            self.assertTrue(result['check']['passed'])
            self.assertTrue((root / 'cookie.txt').exists())
            self.assertFalse((target / 'cookie.txt').exists())
            self.assertFalse((target / 'local-secrets.json').exists())
            count = subprocess.check_output(['git', '-C', str(target), 'rev-list', '--all']).strip()
            self.assertEqual(count, b'')
            with self.assertRaises(ValueError):
                prepare(root, target)


if __name__ == '__main__':
    unittest.main()
