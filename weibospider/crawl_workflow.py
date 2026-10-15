"""Durable keyword -> tweet -> comment -> human review collection rounds."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from extract_topic_keywords import PROJECT, analyze, normalize, write_outputs
from workflow_store import (connect, read_records, totals, comment_targets,
                            import_browser_comments, default_comment_files)

DEFAULT_HOME = PROJECT / "output/keyword-workflow"
DEFAULT_START_DATE = "2026-10-08"
DEFAULT_END_DATE = "2026-10-09"


def date_range(state, start=None, end=None):
    window = state.get("collection_window") or {}
    return (start or window.get("start") or DEFAULT_START_DATE,
            end or window.get("end") or DEFAULT_END_DATE)


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


class WorkflowLock:
    def __init__(self, home):
        self.home = home

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.file = (self.home / "workflow.lock").open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another workflow command is running in this directory") from None
        return self

    def __exit__(self, *args):
        self.file.close()


def parse_review(text):
    decisions = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        excluded = line.startswith(("#", "＃"))
        keyword = normalize(line.lstrip("#＃").strip())
        if not keyword or "#" in keyword or "＃" in keyword:
            raise ValueError(f"Line {number}: use plain keywords; only a leading # is a rejection marker")
        status = "excluded" if excluded else "active"
        if keyword in decisions and decisions[keyword] != status:
            raise ValueError(f"Conflicting decisions for {keyword}; keep only one line")
        decisions[keyword] = status
    return decisions


def apply_decisions(state, decisions, origin):
    for keyword, status in decisions.items():
        entry = state["keywords"].setdefault(keyword, {"first_seen": now()})
        entry.update(status=status, reviewed_at=now(), decision_source=origin)
        if status == "active":
            entry["historical_seed"] = False
    state["events"].append({"at": now(), "event": "keyword_decisions", "source": origin,
                            "active": sum(v == "active" for v in decisions.values()),
                            "excluded": sum(v == "excluded" for v in decisions.values())})


def mirror_keywords(home, state):
    for status, filename in (("active", "search_keywords.txt"), ("excluded", "excluded_keywords.txt")):
        path = home / filename
        text = "".join(k + "\n" for k, entry in sorted(state["keywords"].items()) if entry["status"] == status)
        temp = path.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)


def save(home, state):
    state["updated_at"] = now()
    write_json(home / "state.json", state)
    mirror_keywords(home, state)


def refresh(home, state, records, force_review=False):
    result = analyze(records, list(state["keywords"]), "MySQL crawl_items (workflow readback)")
    for topic in result["topics"]:
        keyword = topic["keyword"]
        entry = state["keywords"].setdefault(keyword, {"status": "pending", "first_seen": now()})
        entry.update(tweet_count=topic["tweet_count"], tweet_ids=topic["tweet_ids"])
    state["totals"] = {"as_of": now(), **totals(records)}
    write_outputs(result, home / "extracted", 1)
    review = home / "review_keywords.txt"
    # Never regenerate an outstanding review: the user may be editing it.
    if (state.get("review") and review.exists() and not force_review
            and (state["review"]["candidates"] or review.read_text(encoding="utf-8-sig").strip())):
        pending = [k for k, entry in state["keywords"].items() if entry["status"] == "pending"]
        state["review"]["new_candidates_waiting"] = sorted(set(pending) - set(state["review"]["candidates"]))
    else:
        pending = sorted((k for k, entry in state["keywords"].items() if entry["status"] == "pending"),
                         key=lambda k: (-state["keywords"][k].get("tweet_count", 0), k))
        if state.get("review") and review.exists():
            # Explicit rebuild archives all prior edits first.
            backup = home / ("review-backup-" + uuid.uuid4().hex[:12] + ".txt")
            backup.write_bytes(review.read_bytes())
        review.write_text("".join(k + "\n" for k in pending), encoding="utf-8")
        state["review"] = {"created_at": now(), "candidates": pending, "new_candidates_waiting": []}
    write_json(home / "data_totals.json", state["totals"])
    metrics = state["totals"]
    active = sum(e["status"] == "active" for e in state["keywords"].values())
    excluded = sum(e["status"] == "excluded" for e in state["keywords"].values())
    pending_count = sum(e["status"] == "pending" for e in state["keywords"].values())
    report = ["# 关键词人工审核与当前数据量", "", f"统计时间：{metrics['as_of']}", "",
        f"微博（按 ID 去重）：**{metrics['unique_tweets']}**", "",
        f"评论（含楼中楼）：**{metrics['comments_including_replies']}**", "",
        f"微博 + 评论：**{metrics['tweet_plus_comment_total']}**", "",
        f"微博关键词命中记录：{metrics['tweet_keyword_rows']}；搜索索引 {metrics['search_index_rows_excluded_from_total']} 条不计入总量。", "",
        f"评论来源：{json.dumps(metrics['comment_sources'], ensure_ascii=False)}；层级：{json.dumps(metrics['comment_levels'], ensure_ascii=False)}。", "",
        f"检索词 {active} 个；排除词 {excluded} 个；待审核 {pending_count} 个。", "",
        "编辑 review_keywords.txt：每行一个词，不要的词在行首加 #，保留的词保持原样。保存后运行 approve。删除行不视为排除，会继续留待审核。",
        "search_keywords.txt 与 excluded_keywords.txt 是分流结果；需要改动已审核词时，用 approve --file 指定另一个同格式文本文件。",
        "排除仅影响后续检索，不过滤微博正文或评论，也不会删除数据库记录。正文中必须有 # 才提取话题，结尾 # 可省略。", "",
        "DOM 评论按保存的指纹去重，接口评论按平台 ID 去重；跨来源未证明同一性时分开计数。这是已保存记录量，不是微博平台全量。", "",
        "## 待审核话题及出现微博数", ""]
    for keyword in state["review"]["candidates"]:
        report.append(f"- {keyword}：{state['keywords'][keyword].get('tweet_count', 0)} 条")
    if state["review"].get("new_candidates_waiting"):
        report += ["", "审核文件保持了已有人工编辑，另有新话题待加入：", ""]
        report += ["- " + k for k in state["review"]["new_candidates_waiting"]]
        report += ["", "先 approve 当前文件，或 prepare --rebuild-review（会先备份编辑内容）。"]
    if metrics["comment_parent_ids_without_stored_body"]:
        report += ["", "存在缺少微博正文的评论所属 ID：" + "、".join(metrics["comment_parent_ids_without_stored_body"])]
    (home / "review_status.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    save(home, state)
    return result


def approve(home, state, path):
    decisions = parse_review(path.read_text(encoding="utf-8-sig"))
    apply_decisions(state, decisions, str(path.resolve()))
    snapshot = home / ("decisions-" + uuid.uuid4().hex[:12] + ".txt")
    snapshot.write_bytes(path.read_bytes())
    state["review"] = None
    save(home, state)


def runtime(kind, override=None):
    if override:
        return str(Path(override).resolve())
    candidate = (PROJECT / ".browser-venv/Scripts/python.exe" if kind == "browser" else
                 Path(os.environ.get("USERPROFILE", "~")) / "anaconda3/envs/identify_v/python.exe")
    return str(candidate) if candidate.exists() else sys.executable


def task_key(keyword, start, end):
    return hashlib.sha256(json.dumps([keyword, start, end], ensure_ascii=False).encode()).hexdigest()


def new_round(state, start, end, backend, max_requests, delay, max_keywords=None, include_existing=False,
              tweets_backend="api"):
    datetime.strptime(start, "%Y-%m-%d")
    datetime.strptime(end, "%Y-%m-%d")
    if start >= end:
        raise ValueError("End date must be after start date (exclusive)")
    if any(r["status"] != "complete" for r in state["rounds"]):
        raise ValueError("An unfinished round exists; use resume to continue its saved stages")
    if any(e["status"] == "pending" for e in state["keywords"].values()):
        raise ValueError("Pending keywords need human review; edit review_keywords.txt and run approve")
    keywords = [k for k, e in state["keywords"].items() if e["status"] == "active"
                and (include_existing or not e.get("historical_seed"))
                and task_key(k, start, end) not in state["completed_searches"]]
    if max_keywords:
        keywords = keywords[:max_keywords]
    if not keywords:
        raise ValueError("No uncollected approved keywords for this date range")
    round_id = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    result = {"round_id": round_id, "created_at": now(), "start": start, "end": end,
        "backend": backend, "tweets_backend": tweets_backend,
        "max_requests": max_requests, "delay": delay, "status": "ready",
        "tasks": [{"kind": "tweet", "keyword": k, "status": "ready"} for k in keywords] +
                 [{"kind": "comments", "status": "ready"}], "keywords": keywords}
    state["rounds"].append(result)
    state["collection_window"] = {"start": start, "end": end, "backend": backend,
                                  "max_requests": max_requests, "delay": delay, "tweets_backend": tweets_backend}
    return result


def execute_round(home, state, round_state, args, comments_only=False):
    folder = home / "rounds" / round_state["round_id"]
    folder.mkdir(parents=True, exist_ok=True)
    round_state["status"] = "running"
    save(home, state)
    db = connect()
    try:
        for number, task in enumerate(round_state["tasks"]):
            if comments_only and task["kind"] == "tweet":
                continue
            if task["status"] == "complete":
                continue
            if task["kind"] == "tweet" and state["keywords"][task["keyword"]]["status"] == "excluded":
                task["status"] = "complete"
                task["skipped"] = "manually_excluded_before_execution"
                save(home, state)
                continue
            job_path, result_path = folder / f"job-{number}.json", folder / f"result-{number}.json"
            job = {"kind": "tweet" if task["kind"] == "tweet" else "comment",
                "round_id": round_state["round_id"], "start": round_state["start"], "end": round_state["end"],
                "delay": round_state["delay"], "max_requests": round_state["max_requests"]}
            if task["kind"] == "tweet":
                job["keyword"] = task["keyword"]
            else:
                db.commit()  # Begin a new read snapshot after child processes commit.
                job["targets"] = comment_targets(read_records(db), round_state["keywords"],
                    round_state["start"] + " 00:00:00", round_state["end"] + " 00:00:00")
                write_json(folder / "comment-targets.json", job["targets"])
                if not job["targets"]:
                    task.update(status="complete", result={"target_count": 0})
                    save(home, state)
                    continue
            write_json(job_path, job)
            task.update(status="running", started_at=now())
            save(home, state)
            if task["kind"] == "tweet" and round_state.get("tweets_backend", "api") == "browser":
                command = [runtime("browser", args.browser_python), str(PROJECT / "weibospider/run_browser_tweets.py"),
                    "--job", str(job_path), "--result", str(result_path),
                    "--output", str(folder / f"browser-tweets-{number}"),
                    "--cache", str(home / "browser-post-date-cache.json"),
                    "--profile", str(Path(round_state.get("browser_profile") or home / "browser-profile"))]
            elif task["kind"] == "comments" and round_state["backend"] == "browser":
                command = [runtime("browser", args.browser_python), str(PROJECT / "weibospider/run_browser_comments.py"),
                    "--targets", str(folder / "comment-targets.json"), "--output", str(folder / "browser-comments"),
                    "--profile", str(Path(round_state.get("browser_profile") or home / "browser-profile")),
                    "--interval", str(round_state["delay"]), "--mysql"]
            else:
                command = [runtime("scrapy", args.scrapy_python), str(PROJECT / "weibospider/crawl_workflow_worker.py"),
                           "--job", str(job_path), "--result", str(result_path)]
            attempt = task.setdefault("attempts", [])
            attempt_result = {"started_at": now(), "job_file": str(job_path)}
            attempt.append(attempt_result)
            save(home, state)
            print(json.dumps({"round_id": round_state["round_id"], "stage": task["kind"],
                              "keyword": task.get("keyword"), "at": now()}, ensure_ascii=False), flush=True)
            with (folder / f"console-{number}-{len(attempt)}.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=str(PROJECT / "weibospider"), stdout=log,
                    stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                try:
                    while True:
                        try:
                            code = process.wait(timeout=30)
                            break
                        except subprocess.TimeoutExpired:
                            print(f"Stage {task['kind']} running; saved rows retained ({now()}).", flush=True)
                except KeyboardInterrupt:
                    process.terminate()
                    process.wait(timeout=15)
                    raise
            attempt_result.update(exit_code=code, ended_at=now())
            if task["kind"] == "comments" and round_state["backend"] == "browser":
                export = folder / "browser-comments/comments.json"
                if export.exists():
                    task["synchronization"] = import_browser_comments(db, [export])
                result = {"complete": code == 0, "coverage": "browser_visible_only"}
                write_json(result_path, result)
            else:
                result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"complete": False}
            task.update(status="complete" if code == 0 and result.get("complete") else "needs_attention",
                        result=result)
            if task["kind"] == "tweet":
                # A resumed search may add targets after partial comments were already collected.
                for comment_task in round_state["tasks"]:
                    if comment_task["kind"] == "comments" and comment_task["status"] == "complete":
                        comment_task["status"] = "ready"
            if task["status"] == "complete" and task["kind"] == "tweet":
                state["completed_searches"][task_key(task["keyword"], round_state["start"], round_state["end"])] = round_state["round_id"]
            if task["status"] != "complete":
                round_state["status"] = "needs_attention"
                save(home, state)
                # Keep acquired bodies, import partial browser records and prepare review even on failure.
                break
            save(home, state)
        else:
            round_state["status"] = ("complete" if all(t["status"] == "complete" for t in round_state["tasks"])
                                     else "needs_attention")
            if round_state["status"] == "complete":
                round_state["ended_at"] = now()
        db.commit()
        refresh(home, state, read_records(db))
    except BaseException:
        round_state["status"] = "needs_attention"
        save(home, state)
        raise
    finally:
        db.close()
    return 0 if round_state["status"] == "complete" else 5


def print_status(state):
    print(json.dumps({"totals": state.get("totals"), "keyword_counts":
        {s: sum(e["status"] == s for e in state["keywords"].values()) for s in ("active", "excluded", "pending")},
        "approved_nonseed_keywords": sum(e["status"] == "active" and not e.get("historical_seed")
                                           for e in state["keywords"].values()),
        "last_round": {k: state["rounds"][-1][k] for k in ("round_id", "status", "start", "end")}
            if state["rounds"] else None}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "prepare", "approve", "run", "resume", "collect-comments", "status", "import-comments"])
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--file", type=Path, help="Review/seed decision file, or one browser comment JSON for import")
    parser.add_argument("--start-date", help="Saved range, otherwise 2026-10-08 (Asia/Shanghai)")
    parser.add_argument("--end-date", help="Exclusive end date; saved range, otherwise 2026-10-09")
    parser.add_argument("--comments-backend", choices=["browser", "api"], default="browser")
    parser.add_argument("--tweets-backend", choices=["browser", "api"],
                        help="Saved/default backend unless explicitly selected; browser uses normal pages")
    parser.add_argument("--max-requests", type=int, default=2000, help="Per Scrapy stage")
    parser.add_argument("--max-keywords", type=int, help="Limit the number of keywords in this round")
    parser.add_argument("--include-existing", action="store_true", help="Also query the historical seed search keywords")
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--scrapy-python", help="Python with Scrapy and PyMySQL")
    parser.add_argument("--browser-python", help="Python with Playwright")
    parser.add_argument("--browser-profile", type=Path, help="Existing dedicated collector Chrome profile")
    parser.add_argument("--rebuild-review", action="store_true", help="Archive edits before generating a fresh pending review")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry an interrupted/failed stage after investigating its log")
    args = parser.parse_args()
    if args.max_requests < 1 or args.delay < 1 or (args.max_keywords is not None and args.max_keywords < 1):
        parser.error("Positive request/keyword budgets and delay >= 1 second required")
    args.home = args.home.resolve()
    try:
        if args.command == "status":
            path = args.home / "state.json"
            if not path.exists():
                raise ValueError("Run init first")
            state = json.loads(path.read_text(encoding="utf-8"))
            db = connect()
            try:
                state["totals"] = {"as_of": now(), **totals(read_records(db))}
            finally:
                db.close()
            print_status(state)
            return 0
        with WorkflowLock(args.home):
            state_path = args.home / "state.json"
            if args.command == "init":
                if state_path.exists():
                    raise ValueError("Workflow already initialized; use prepare/status/approve, not init")
                state = {"schema_version": 1, "created_at": now(), "keywords": {}, "rounds": [],
                         "completed_searches": {}, "events": [], "review": None,
                         "collection_window": {"start": DEFAULT_START_DATE, "end": DEFAULT_END_DATE,
                                               "backend": "browser", "tweets_backend": "browser", "max_requests": 2000, "delay": 5}}
                db = connect()
                try:
                    synchronization = import_browser_comments(db, default_comment_files())
                    records = read_records(db)
                    for row in records:
                        if row["keyword"]:
                            state["keywords"].setdefault(normalize(row["keyword"]),
                                {"status": "active", "first_seen": now(), "historical_seed": True,
                                 "decision_source": "existing_crawl_keyword"})
                    seed = args.file or PROJECT / "output/topic-keywords/new_keywords.txt"
                    if seed.exists():
                        apply_decisions(state, parse_review(seed.read_text(encoding="utf-8-sig")), str(seed.resolve()))
                    state["events"].append({"at": now(), "event": "import_saved_comments", **synchronization})
                    refresh(args.home, state, records)
                finally:
                    db.close()
            else:
                if not state_path.exists():
                    raise ValueError("Run init first")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                for entry in state["keywords"].values():
                    entry.setdefault("historical_seed", entry.get("decision_source") == "existing_crawl_keyword")
                if args.command in ("run", "resume", "collect-comments"):
                    if args.command == "run":
                        start, end = date_range(state, args.start_date, args.end_date)
                        round_state = new_round(state, start, end, args.comments_backend,
                                                args.max_requests, args.delay, args.max_keywords,
                                                include_existing=args.include_existing,
                                                tweets_backend=args.tweets_backend or state.get("collection_window", {}).get("tweets_backend", "browser"))
                        save(args.home, state)
                    else:
                        unfinished = [r for r in state["rounds"] if r["status"] != "complete"]
                        if not unfinished:
                            raise ValueError("No unfinished round")
                        if args.command == "resume" and not args.retry_failed:
                            raise ValueError("Inspect logs and restore normal access, then use resume --retry-failed")
                        round_state = unfinished[-1]
                        if args.command == "collect-comments" and not args.retry_failed and any(
                                t["kind"] == "comments" and t["status"] in ("needs_attention", "running")
                                for t in round_state["tasks"]):
                            raise ValueError("Inspect comment logs and restore normal access before collect-comments --retry-failed")
                        state["events"].append({"at": now(), "event": "collect_saved_tweet_comments" if args.command == "collect-comments"
                                                else "operator_retry", "round_id": round_state["round_id"]})
                    if args.browser_profile:
                        round_state["browser_profile"] = str(args.browser_profile.resolve())
                    if args.tweets_backend:
                        round_state["tweets_backend"] = args.tweets_backend
                        state.setdefault("collection_window", {})["tweets_backend"] = args.tweets_backend
                    code = execute_round(args.home, state, round_state, args, comments_only=args.command == "collect-comments")
                    print_status(state)
                    return code
                if args.command == "approve":
                    approve(args.home, state, args.file or args.home / "review_keywords.txt")
                db = connect()
                try:
                    if args.command == "import-comments":
                        paths = [args.file] if args.file else default_comment_files()
                        state["events"].append({"at": now(), "event": "import_saved_comments",
                                                **import_browser_comments(db, paths)})
                    refresh(args.home, state, read_records(db), args.rebuild_review)
                finally:
                    db.close()
            print_status(state)
            print("Review: " + str(args.home / "review_status.md"))
            return 0
    except KeyboardInterrupt:
        print("Interrupted; saved data retained. Inspect stage logs before resume --retry-failed.")
        return 130
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError)):
            print(str(exc))
        else:
            print(f"Workflow stopped: {type(exc).__name__}; credentials are not logged.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
