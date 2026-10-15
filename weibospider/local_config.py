"""Read private local settings without embedding credentials in source code."""
import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
KEYS = ("WEIBO_COOKIE", "WEIBO_DB_HOST", "WEIBO_DB_PORT", "WEIBO_DB_NAME",
        "WEIBO_DB_USER", "WEIBO_DB_PASSWORD")


def read_config(config_file=None, *, project=None, environ=None):
    project = Path(project) if project is not None else PROJECT
    environment = os.environ if environ is None else environ
    explicit = config_file or environment.get("WEIBO_CONFIG_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError("The explicitly selected local configuration file does not exist")
    else:
        path = next((p for p in (project / "local-secrets.json", project.parent / "local-secrets.json")
                     if p.is_file()), None)
    values = {}
    if path is not None:
        try:
            content = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid local configuration JSON; contents are not logged") from None
        if not isinstance(content, dict):
            raise ValueError("Local configuration must be a JSON object")
        values = {k: str(content[k]) for k in KEYS if k in content and content[k] is not None}
    values.update({k: environment[k] for k in KEYS if k in environment})
    # Backward compatibility for an ignored private Cookie file, never shipped.
    legacy_cookie = project / "weibospider/cookie.txt"
    if "WEIBO_COOKIE" not in values and legacy_cookie.is_file():
        values["WEIBO_COOKIE"] = legacy_cookie.read_text(encoding="utf-8-sig").strip()
    return values


def load_environment():
    config = read_config()
    for key, value in config.items():
        os.environ.setdefault(key, value)
    return config
