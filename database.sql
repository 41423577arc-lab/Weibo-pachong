-- Run using an account allowed to create databases, then use a dedicated
-- crawler account with SELECT, INSERT, UPDATE, CREATE on weibo_data.*.
CREATE DATABASE IF NOT EXISTS weibo_data
    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE weibo_data;

CREATE TABLE IF NOT EXISTS crawl_items (
    spider VARCHAR(64) NOT NULL,
    item_id VARCHAR(128) NOT NULL,
    keyword VARCHAR(255) NOT NULL DEFAULT '',
    crawl_time BIGINT NOT NULL,
    payload JSON NOT NULL,
    PRIMARY KEY (spider, item_id, keyword),
    KEY idx_crawl_time (crawl_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Example after crawling:
-- SELECT item_id, payload->>'$.content' AS content FROM crawl_items LIMIT 10;
