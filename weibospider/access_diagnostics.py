"""Credential-free response classification, reusable by real collection workers."""
import json
from urllib.parse import parse_qs, urlparse


def response_diagnostics(url, status, headers, body):
    parsed = urlparse(url)
    result = {"host": parsed.hostname, "path": parsed.path, "status": status,
              "id": parse_qs(parsed.query).get("id", [None])[0],
              "content_type": str(headers.get("Content-Type", "")).split(";")[0],
              "body_bytes": len(body.encode("utf-8")), "json_response": False}
    destination = headers.get("Location")
    if destination:
        target = urlparse(destination)
        result["redirect_host"] = target.hostname
        result["redirect_path"] = target.path
    messages = []
    try:
        data = json.loads(body)
        result["json_response"] = True
        if isinstance(data, dict):
            for key in ("ok", "error_code", "errno", "code"):
                if isinstance(data.get(key), (int, bool)):
                    result[key] = data[key]
            for key in ("msg", "message", "error", "error_description"):
                if isinstance(data.get(key), str):
                    messages.append(data[key])
            if isinstance(data.get("data"), list):
                result["returned_records"] = len(data["data"])
            if isinstance(data.get("comments_count"), int):
                result["comments_count"] = data["comments_count"]
    except (ValueError, TypeError):
        messages.append(body[:8192])
    text = " ".join(messages).lower()
    result["reason_signals"] = {"login": any(s in text for s in ("登录", "login", "passport")),
        "rate_limit": any(s in text for s in ("频次", "频率", "too many", "rate limit")),
        "verification": any(s in text for s in ("验证", "验证码", "captcha", "verify")),
        "removed_or_missing": any(s in text for s in ("不存在", "已被删除", "not found")),
        "permission": any(s in text for s in ("权限", "无权", "forbidden", "permission"))}
    return result
