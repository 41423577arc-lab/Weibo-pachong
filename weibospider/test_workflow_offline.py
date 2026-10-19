"""Workflow semantics with synthetic records; no platform or live DB calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

from crawl_workflow import (parse_review, apply_decisions, refresh, approve, new_round,
                            task_key, execute_round, date_range)
from workflow_store import (totals, comment_targets, saved_browser_comments, import_browser_comments,
                            MirroredBrowserCommentStore)


def state():
    return {"keywords": {}, "events": [], "rounds": [], "review": None, "completed_searches": {}}


def tweet(mid="100", text="#保留 #不要#", keyword="种子"):
    return {"spider": "tweet_spider_by_keyword", "item_id": mid, "keyword": keyword,
            "payload": {"_id": mid, "content": text, "created_at": "2026-10-08 10:00:00",
                        "user": {"_id": "200"}, "url": f"https://weibo.com/200/{mid}", "comments_count": 0}}


class WorkflowTests(unittest.TestCase):
    def test_default_date_range_matches_previous_successful_collection(self):
        self.assertEqual(date_range(state()), ("2026-10-08", "2026-10-09"))
        s = state()
        s["collection_window"] = {"start": "2026-10-01", "end": "2026-10-03"}
        self.assertEqual(date_range(s), ("2026-10-01", "2026-10-03"))
        self.assertEqual(date_range(s, "2026-10-10", "2026-10-11"), ("2026-10-10", "2026-10-11"))

    def test_manual_marks_are_decisions_not_hashtags(self):
        self.assertEqual(parse_review("保留\n#不要\n＃也不要\n\n"),
                         {"保留": "active", "不要": "excluded", "也不要": "excluded"})
        with self.assertRaises(ValueError):
            parse_review("保留\n#保留")
        with self.assertRaises(ValueError):
            parse_review("#正文话题#")

    def test_rejected_keywords_do_not_filter_tweets_or_comments(self):
        rows = [tweet(), {"spider": "comment", "item_id": "300", "keyword": "",
                         "payload": {"tweet_id": "100", "content": "#不要#", "comment_level": 1}}]
        s = state()
        apply_decisions(s, {"不要": "excluded"}, "test")
        with tempfile.TemporaryDirectory() as directory:
            refresh(Path(directory), s, rows)
            self.assertEqual(s["totals"]["tweet_plus_comment_total"], 2)
            self.assertEqual(s["keywords"]["不要"]["status"], "excluded")
            self.assertIn("不要\n", (Path(directory) / "excluded_keywords.txt").read_text(encoding="utf-8"))

    def test_prepare_preserves_review_edits_and_unreviewed_deleted_lines(self):
        s = state()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            refresh(home, s, [tweet()])
            review = home / "review_keywords.txt"
            review.write_text("#不要\n", encoding="utf-8")
            refresh(home, s, [tweet(), tweet("101", "#新来的")])
            self.assertEqual(review.read_text(encoding="utf-8"), "#不要\n")
            self.assertIn("新来的", s["review"]["new_candidates_waiting"])
            approve(home, s, review)
            refresh(home, s, [tweet(), tweet("101", "#新来的")])
            self.assertEqual(s["keywords"]["保留"]["status"], "pending")
            self.assertEqual(s["keywords"]["不要"]["status"], "excluded")

    def test_empty_review_accepts_new_candidates_next_round(self):
        s = state()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            refresh(home, s, [])
            refresh(home, s, [tweet("100", "#新词")])
            self.assertIn("新词", (home / "review_keywords.txt").read_text(encoding="utf-8"))

    def test_round_only_uses_approved_new_search_windows(self):
        s = state()
        apply_decisions(s, {"保留": "active", "不要": "excluded"}, "test")
        s["completed_searches"][task_key("保留", "2026-10-08", "2026-10-09")] = "past"
        with self.assertRaises(ValueError):
            new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)
        r = new_round(s, "2026-10-09", "2026-10-10", "api", 10, 5)
        self.assertEqual(r["keywords"], ["保留"])
        with self.assertRaises(ValueError):
            new_round(s, "2026-10-10", "2026-10-11", "api", 10, 5)

    def test_pending_review_blocks_network_round(self):
        s = state()
        s["keywords"]["未审核"] = {"status": "pending"}
        with self.assertRaises(ValueError):
            new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)

    def test_historical_seeds_do_not_replace_new_keyword_frontier(self):
        s = state()
        apply_decisions(s, {"旧检索": "active", "新检索": "active"}, "test")
        s["keywords"]["旧检索"]["historical_seed"] = True
        r = new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)
        self.assertEqual(r["keywords"], ["新检索"])
        r["status"] = "complete"
        s["completed_searches"][task_key("新检索", "2026-10-08", "2026-10-09")] = r["round_id"]
        r = new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5, include_existing=True)
        self.assertEqual(r["keywords"], ["旧检索"])

    def test_all_stored_tweets_targeted_even_zero_count_and_rejected_body(self):
        rows = [tweet(), tweet(keyword="种子"), tweet("101", keyword="其他检索词")]
        targets = comment_targets(rows, ["种子"], "2026-10-08", "2026-10-09")
        self.assertEqual([t["tweet_id"] for t in targets], ["100"])

    def test_totals_deduplicate_tweets_ignore_indices_count_replies(self):
        rows = [tweet(), tweet(keyword="第二个检索词"),
                {"spider": "history_search_hit", "item_id": "999", "payload": {}},
                {"spider": "comment", "item_id": "300", "payload": {"tweet_id": "100", "comment_level": 1}},
                {"spider": "browser_comment", "item_id": "hash", "payload": {"tweet_id": "100", "comment_level": 2}}]
        result = totals(rows)
        self.assertEqual(result["unique_tweets"], 1)
        self.assertEqual(result["tweet_keyword_rows"], 2)
        self.assertEqual(result["tweet_plus_comment_total"], 3)
        self.assertEqual(result["comment_parent_ids_without_stored_body"], [])

    def test_browser_import_has_stable_identity_and_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comments.json"
            path.write_text(json.dumps({"targets": [{"tweet_id": "100", "comments": [
                {"comment_level": 1, "content": "#不要#", "user_id": "200", "created_at_display": "今天"}]}]}), encoding="utf-8")
            first = list(saved_browser_comments(path))[0]
            second = list(saved_browser_comments(path))[0]
            self.assertEqual(first["_id"], second["_id"])
            self.assertEqual(first["tweet_id"], "100")
            db = MagicMock()
            cursor = db.cursor.return_value.__enter__.return_value
            cursor.rowcount = 1
            self.assertEqual(import_browser_comments(db, [path])["new_comment_rows"], 1)
            query, values = cursor.execute.call_args.args
            self.assertIn("INSERT IGNORE", query)
            self.assertEqual(json.loads(values[-1])["tweet_id"], "100")
            db.commit.assert_called_once()

    def test_conflicting_browser_parent_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comments.json"
            path.write_text(json.dumps({"targets": [{"tweet_id": "100", "comments": [
                {"tweet_id": "999", "content": "正文"}]}]}), encoding="utf-8")
            db = MagicMock()
            with self.assertRaises(ValueError):
                import_browser_comments(db, [path])
            db.rollback.assert_called_once()
            db.commit.assert_not_called()

    def test_browser_capture_mirrors_immediately_and_survives_mysql_error(self):
        with tempfile.TemporaryDirectory() as directory:
            db = MagicMock()
            with patch("workflow_store.connect", return_value=db):
                store = MirroredBrowserCommentStore(Path(directory) / "comments.sqlite3")
            try:
                store.save("100", [{"comment_level": 1, "content": "不要也保存", "user_id": "200"}])
                self.assertEqual(json.loads(db.cursor.return_value.__enter__.return_value.execute.call_args.args[1][-1])["tweet_id"], "100")
                db.commit.assert_called_once()
                db.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("DB unavailable")
                with self.assertRaises(RuntimeError):
                    store.save("100", [{"comment_level": 2, "content": "二级回复", "user_id": "300"}], "root")
                self.assertEqual(len(store.records("100")), 2)
                db.rollback.assert_called_once()
            finally:
                store.close()

    def test_failed_stage_keeps_round_and_does_not_start_next_stage(self):
        s = state()
        apply_decisions(s, {"种子": "active"}, "test")
        r = new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)
        args = Mock(scrapy_python=None, browser_python=None)
        with tempfile.TemporaryDirectory() as directory:
            with patch("crawl_workflow.connect"), patch("crawl_workflow.read_records", return_value=[tweet()]), \
                 patch("crawl_workflow.subprocess.Popen") as child:
                child.return_value.wait.return_value = 5
                self.assertEqual(execute_round(Path(directory), s, r, args), 5)
                child.assert_called_once()
                self.assertEqual(r["status"], "needs_attention")
                self.assertEqual(s["totals"]["unique_tweets"], 1)
                self.assertEqual(r["tasks"][1]["status"], "ready")

    def test_resume_skips_completed_search_stage(self):
        s = state()
        apply_decisions(s, {"种子": "active"}, "test")
        r = new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)
        r["tasks"][0]["status"] = "complete"
        args = Mock(scrapy_python=None, browser_python=None)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / "rounds" / r["round_id"]
            folder.mkdir(parents=True)
            (folder / "result-1.json").write_text('{"complete":true}', encoding="utf-8")
            with patch("crawl_workflow.connect"), patch("crawl_workflow.read_records", return_value=[tweet()]), \
                 patch("crawl_workflow.subprocess.Popen") as child:
                child.return_value.wait.return_value = 0
                self.assertEqual(execute_round(home, s, r, args), 0)
                child.assert_called_once()
                self.assertEqual(r["status"], "complete")

    def test_partial_comment_collection_keeps_failed_search_pending(self):
        s = state()
        apply_decisions(s, {"种子": "active"}, "test")
        r = new_round(s, "2026-10-08", "2026-10-09", "api", 10, 5)
        r["tasks"][0]["status"] = "needs_attention"
        args = Mock(scrapy_python=None, browser_python=None)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / "rounds" / r["round_id"]
            folder.mkdir(parents=True)
            (folder / "result-1.json").write_text('{"complete":true}', encoding="utf-8")
            with patch("crawl_workflow.connect"), patch("crawl_workflow.read_records", return_value=[tweet()]), \
                 patch("crawl_workflow.subprocess.Popen") as child:
                child.return_value.wait.return_value = 0
                self.assertEqual(execute_round(home, s, r, args, comments_only=True), 5)
                child.assert_called_once()
                self.assertEqual(r["tasks"][0]["status"], "needs_attention")
                self.assertEqual(r["tasks"][1]["status"], "complete")
                self.assertEqual(r["status"], "needs_attention")


if __name__ == "__main__":
    unittest.main()
