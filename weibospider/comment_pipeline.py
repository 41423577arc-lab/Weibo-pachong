"""Insert comments without changing any previously stored record."""
from mysql_pipeline import MySQLPipeline


class CommentMySQLPipeline(MySQLPipeline):
    @staticmethod
    def _write(transaction, values):
        transaction.execute(
            'INSERT IGNORE INTO crawl_items (spider, item_id, keyword, crawl_time, payload) '
            'VALUES (%s, %s, %s, %s, %s)', values)
