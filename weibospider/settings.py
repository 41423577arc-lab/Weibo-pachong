# -*- coding: utf-8 -*-
import os
from local_config import load_environment

load_environment()

BOT_NAME = 'spider'

SPIDER_MODULES = ['spiders']
NEWSPIDER_MODULE = 'spiders'

ROBOTSTXT_OBEY = False

cookie = os.environ.get('WEIBO_COOKIE', '').strip()
DEFAULT_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://weibo.com/',
    'Origin': 'https://weibo.com',
}
if cookie:
    DEFAULT_REQUEST_HEADERS['Cookie'] = cookie

CONCURRENT_REQUESTS = 16

DOWNLOAD_DELAY = 1

DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': None,
    'scrapy.downloadermiddlewares.redirect.RedirectMiddleware': None,
    'middlewares.IPProxyMiddleware': 100,
    'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': 101,
}

ITEM_PIPELINES = {
    'mysql_pipeline.MySQLPipeline': 300,
}
