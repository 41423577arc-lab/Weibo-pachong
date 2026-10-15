"""Synthetic extraction checks; no network or database access."""
import json
from pathlib import Path
import tempfile
import unittest

from extract_topic_keywords import analyze, extract_topics, file_records, write_outputs


def record(mid, content, keyword="旧词", spider="tweet_spider_by_keyword"):
    return {"spider": spider, "item_id": mid, "keyword": keyword,
            "payload": {"_id": mid, "content": content, "url": f"https://weibo.com/1/{mid}"}}


class TopicTests(unittest.TestCase):
    def test_html_entities_fullwidth_and_duplicates(self):
        self.assertEqual(extract_topics('<a href="/?q=%23诱饵%23">#中国队#</a>'
            '＃第五人格＃ &#35;中国队&#35; #ＡＢＣ# #空\u200b格#'),
            ["ABC", "中国队", "空格", "第五人格"])

    def test_empty_topics(self):
        for text in ("##", "# #", "正常正文，没有标签"):
            with self.subTest(text=text):
                self.assertEqual(extract_topics(text), [])

    def test_open_topics_and_boundaries(self):
        self.assertEqual(extract_topics("正文#冠军！#新词 正文 #中国队# #结尾"),
                         ["中国队", "冠军", "新词", "结尾"])
        self.assertEqual(extract_topics("#刘耀文up it# #新话题 #另一话题"),
                         ["刘耀文up it", "另一话题", "新话题"])

    def test_distinct_tweets_and_existing_keywords(self):
        result = analyze([record("1", "#新词##新词##旧词#"),
            record("1", "#新词#", "另一个检索词"), record("2", "#新词#")], [], "test")
        self.assertEqual(result["unique_tweets_with_body"], 2)
        self.assertEqual(result["topics"][0]["tweet_count"], 2)
        self.assertFalse(next(t for t in result["topics"] if t["keyword"] == "旧词")["is_new"])

    def test_exclude_comments_index_and_empty_body(self):
        rows = [record("1", "#正文#"), record("2", "#评论#", spider="comment"),
                record("3", "#索引#", spider="history_search_hit"), record("4", "")]
        result = analyze(rows, [], "test")
        self.assertEqual(result["topic_count"], 1)
        self.assertEqual(result["skipped_rows"], {"not_tweet_body": 2, "missing_body": 1})

    def test_backup_and_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.json"
            path.write_text(json.dumps([["tweet_spider_by_keyword", "1", "检索词", 0,
                json.dumps({"content": "#新话题#"})]]), encoding="utf-8")
            result = analyze(file_records(path), [], "backup")
            self.assertEqual(result["topics"][0]["keyword"], "新话题")
            path = Path(directory) / "items.jsonl"
            path.write_text(json.dumps({"_id": "2", "content": "#正文#"}) + "\n" +
                json.dumps({"_id": "3", "tweet_id": "2", "content": "#评论#"}), encoding="utf-8")
            result = analyze(file_records(path), [], "jsonl")
            self.assertEqual(result["unique_tweets_with_body"], 1)

    def test_output_threshold_and_empty_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = analyze([record("1", "#常见##单次#"), record("2", "#常见#")], [], "test")
            write_outputs(result, output, 2)
            self.assertEqual((output / "new_keywords.txt").read_text(encoding="utf-8"), "常见\n")
            result = analyze([], [], "empty")
            write_outputs(result, output, 1)
            self.assertEqual(result["tweets_with_topics"], 0)
            self.assertEqual((output / "new_keywords.txt").read_text(encoding="utf-8"), "")

    def test_refresh_keeps_user_rejection_marks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "new_keywords.txt").write_text("#不要\n保留\n", encoding="utf-8")
            result = analyze([record("1", "#不要# #保留#")], [], "test")
            write_outputs(result, output, 1)
            self.assertIn("#不要\n", (output / "new_keywords.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
