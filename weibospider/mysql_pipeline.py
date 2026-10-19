"""Store every parsed item in MySQL, keeping nested fields as JSON."""
import json
import os
import time


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS crawl_items (
    spider VARCHAR(64) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    keyword VARCHAR(255) NOT NULL DEFAULT '',
    crawl_time BIGINT NOT NULL,
    payload JSON NOT NULL,
    PRIMARY KEY (spider, item_id, keyword),
    KEY idx_crawl_time (crawl_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""

UPSERT = """
INSERT INTO crawl_items (spider, item_id, keyword, crawl_time, payload)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE crawl_time=VALUES(crawl_time), payload=VALUES(payload)
"""


def item_values(item, spider_name):
    data = dict(item)
    if data.get('_id') is None:
        raise ValueError('Cannot store an item without _id')
    data['crawl_time'] = int(time.time())
    return (spider_name, str(data['_id']), str(data.get('keyword') or ''),
            data['crawl_time'], json.dumps(data, ensure_ascii=False))


class MySQLPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def __init__(self, crawler):
        self.crawler = crawler
        self.pool = None

    def open_spider(self, spider):
        import pymysql
        from twisted.enterprise import adbapi

        if not os.environ.get('WEIBO_DB_USER'):
            raise RuntimeError('Set WEIBO_DB_USER and WEIBO_DB_PASSWORD locally before crawling')
        options = {
            'host': os.environ.get('WEIBO_DB_HOST', '127.0.0.1'),
            'port': int(os.environ.get('WEIBO_DB_PORT', '3306')),
            'user': os.environ['WEIBO_DB_USER'],
            'password': os.environ.get('WEIBO_DB_PASSWORD', ''),
            'database': os.environ.get('WEIBO_DB_NAME', 'weibo_data'),
            'charset': 'utf8mb4',
            'connect_timeout': 10,
            'read_timeout': 30,
            'write_timeout': 30,
        }
        # Fail before any requests if the database is unavailable.
        connection = pymysql.connect(**options)
        try:
            with connection.cursor() as cursor:
                cursor.execute(CREATE_TABLE)
            connection.commit()
        finally:
            connection.close()
        self.pool = adbapi.ConnectionPool('pymysql', cp_min=1, cp_max=2, **options)

    def process_item(self, item, spider):
        values = item_values(item, spider.name)
        result = self.pool.runInteraction(self._write, values)
        result.addCallback(self._write_succeeded, item)
        result.addErrback(self._write_failed, spider)
        return result

    def _write_succeeded(self, result, item):
        self.crawler.stats.inc_value('database/items_committed')
        return item

    @staticmethod
    def _write(transaction, values):
        transaction.execute(UPSERT, values)

    def _write_failed(self, failure, spider):
        self.crawler.stats.inc_value('database/write_failures')
        # Do not log the connection options or item payload (may contain private data).
        spider.logger.error('MySQL write failed; stopping the crawl. Error type: %s',
                            failure.type.__name__)
        self.crawler.engine.close_spider(spider, reason='database_write_failed')
        return failure

    def close_spider(self, spider):
        if self.pool is not None:
            self.pool.close()
