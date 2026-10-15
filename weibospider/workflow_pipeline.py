"""Append-only collection with explicit round and parent identity."""
from mysql_pipeline import MySQLPipeline


class WorkflowPipeline(MySQLPipeline):
    def process_item(self, item, spider):
        item = dict(item)
        item["workflow_round_id"] = spider.workflow_round_id
        if spider.name == "comment" and not str(item.get("tweet_id") or "").isdigit():
            raise ValueError("Every comment requires its numeric parent tweet_id")
        return super().process_item(item, spider)

    @staticmethod
    def _write(transaction, values):
        transaction.execute("INSERT IGNORE INTO crawl_items (spider,item_id,keyword,crawl_time,payload) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE payload=IF("
            "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.long_text_complete'))='false' AND "
            "JSON_UNQUOTE(JSON_EXTRACT(VALUES(payload),'$.long_text_complete'))='true',"
            "VALUES(payload),payload)", values)
