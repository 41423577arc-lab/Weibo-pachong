"""Read normal post DOM and passive response metadata in the dedicated profile.

Never reads cookies, tokens, browser storage or request headers. Does not retry
denied endpoints. Diagnostics exclude raw response bodies and user identities.
"""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from extract_topic_keywords import PROJECT
from run_browser_comments import PlaywrightDriver, RunLock


async def diagnose(args):
    from playwright.async_api import async_playwright
    args.output.mkdir(parents=True, exist_ok=True)
    events = []
    pending = []

    async def observed(response):
        parsed = urlparse(response.url)
        if parsed.hostname not in {"weibo.com", "www.weibo.com"} or not parsed.path.startswith("/ajax/"):
            return
        if not any(name in parsed.path for name in ("statuses/show", "buildComments", "config")):
            return
        event = {"path": parsed.path, "status": response.status,
                 "id": parse_qs(parsed.query).get("id", [None])[0]}
        try:
            body = await response.json()
            if isinstance(body, dict):
                event["json_keys"] = sorted(body)
                for key in ("ok", "error_code", "errno", "code"):
                    if isinstance(body.get(key), (int, bool)):
                        event[key] = body[key]
                if isinstance(body.get("data"), list):
                    event["returned_records"] = len(body["data"])
                if isinstance(body.get("comments_count"), int):
                    event["comments_count"] = body["comments_count"]
        except Exception:
            event["json_readable"] = False
        events.append(event)

    with RunLock(args.output / "diagnostic.lock"):
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(args.profile.resolve()),
                channel="chrome", headless=True, accept_downloads=False, viewport={"width": 1280, "height": 900})
            try:
                page = await context.new_page()
                page.on("response", lambda response: pending.append(asyncio.create_task(observed(response))))
                result = {"url": args.url, "profile": str(args.profile.resolve())}
                try:
                    response = await page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
                    result["navigation_status"] = response.status if response else None
                    await page.wait_for_timeout(12000)
                    result["final_path"] = urlparse(page.url).path
                    driver = PlaywrightDriver(page)
                    driver.expected_url = args.url
                    snapshot = await driver.snapshot()
                    result["snapshot"] = {k: v for k, v in snapshot.items() if k not in ("comments", "replies")}
                    result["parsed_comments"] = len(snapshot["comments"])
                    result["parsed_replies"] = len(snapshot["replies"])
                    result["dom_signals"] = await page.evaluate("""() => ({
                      title: document.title,
                      main: !!document.querySelector('main'),
                      article: !!document.querySelector('main article'),
                      root_rows: document.querySelectorAll('main .item1').length,
                      no_comments: /暂无评论|还没有评论|暂时没有评论|快来抢沙发/.test(document.body.innerText),
                      login_prompt: /登录后|请先登录/.test(document.body.innerText),
                      removed_post: /微博不存在|微博已被删除|抱歉，此微博已被删除/.test(document.body.innerText),
                      verification_prompt: /访问频次过高|访问异常|安全验证|完成验证/.test(document.body.innerText),
                      empty_labels: Array.from(document.querySelectorAll('main *')).filter(e =>
                        e.children.length === 0 && /暂无评论|还没有评论|暂时没有评论|快来抢沙发/.test(e.textContent))
                        .map(e => ({tag: e.tagName, class: e.className, text: e.textContent.trim().slice(0,80)})).slice(0,10)
                    })""")
                    await page.screenshot(path=str(args.output / "post.png"), full_page=False)
                except Exception as exc:
                    result["error_type"] = type(exc).__name__
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                result["passive_responses"] = events
                (args.output / "diagnostic.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False))
            finally:
                await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--profile", type=Path, default=PROJECT / "output/browser-comments/browser-profile")
    parser.add_argument("--output", type=Path, default=PROJECT / "output/access-diagnostic")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != "https" or parsed.hostname != "weibo.com" or parsed.query or parsed.fragment:
        parser.error("Use an existing public HTTPS weibo.com post URL without query/fragment")
    asyncio.run(diagnose(args))


if __name__ == "__main__":
    main()
