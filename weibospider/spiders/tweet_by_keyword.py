#!/usr/bin/env python
# encoding: utf-8
"""
Author: rightyonghu
Created Time: 2022/10/22
"""
import datetime
import json
import re
from urllib.parse import urlencode, urlparse
from scrapy import Spider, Request
from spiders.common import parse_tweet_info, parse_long_tweet


class TweetSpiderByKeyword(Spider):
    """
    关键词搜索采集
    """
    name = "tweet_spider_by_keyword"
    base_url = "https://s.weibo.com/"

    def __init__(self, keyword='第五人格亚运会', start_date='2026-10-01', end_date='2026-10-03', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keyword = keyword
        self.start_time = datetime.datetime.fromisoformat(start_date)
        self.end_time = datetime.datetime.fromisoformat(end_date)
        self.failed_request_urls = set()
        if self.start_time >= self.end_time:
            raise ValueError('end_date must be after start_date (exclusive)')

    async def start(self):
        for request in self.start_requests():
            yield request

    def start_requests(self):
        """
        爬虫入口
        """
        # 这里keywords可替换成实际待采集的数据
        keywords = [self.keyword]
        # 这里的时间可替换成实际需要的时间段
        start_time = self.start_time
        end_time = self.end_time
        # 是否按照小时进行切分，数据量更大; 对于非热门关键词**不需要**按照小时切分
        is_split_by_hour = True
        for keyword in keywords:
            if not is_split_by_hour:
                _start_time = start_time.strftime("%Y-%m-%d-%H")
                _end_time = end_time.strftime("%Y-%m-%d-%H")
                url = f"https://s.weibo.com/weibo?q={keyword}&timescope=custom%3A{_start_time}%3A{_end_time}&page=1"
                yield Request(url, callback=self.parse, errback=self.request_failed, meta={'keyword': keyword})
            else:
                time_cur = start_time
                while time_cur < end_time:
                    _start_time = time_cur.strftime("%Y-%m-%d-%H")
                    _end_time = (time_cur + datetime.timedelta(hours=1)).strftime("%Y-%m-%d-%H")
                    url = 'https://s.weibo.com/weibo?' + urlencode({
                        'q': keyword, 'timescope': f'custom:{_start_time}:{_end_time}', 'page': 1})
                    yield Request(url, callback=self.parse, errback=self.request_failed, meta={'keyword': keyword})
                    time_cur = time_cur + datetime.timedelta(hours=1)

    def request_failed(self, failure):
        self.failed_request_urls.add(failure.request.url)
        self.crawler.stats.set_value('requests/failed_terminal', len(self.failed_request_urls))
        self.logger.error('Request failed after retries: %s', failure.type.__name__)

    def parse(self, response, **kwargs):
        """
        网页解析
        """
        html = response.text
        if 'passport.weibo' in response.url or '新浪通行证' in html or '访问频次过高' in html:
            self.crawler.stats.inc_value('search/login_or_rate_limit')
            from scrapy.exceptions import CloseSpider
            raise CloseSpider('login_or_rate_limit')
        if '抱歉，未找到相关结果' in html:
            self.crawler.stats.inc_value('search/no_results')
            self.logger.info(f'no search result. url: {response.url}')
            return
        tweet_ids = set()
        for href in response.css('.from a::attr(href)').getall():
            match = re.search(r'weibo\.com/\d+/([A-Za-z0-9]+)', href)
            if match:
                tweet_ids.add(match.group(1))
        if not tweet_ids:
            self.crawler.stats.inc_value('search/unrecognized_page')
            from scrapy.exceptions import CloseSpider
            raise CloseSpider('unrecognized_search_page')
        self.crawler.stats.inc_value('search/recognized_pages')
        for tweet_id in sorted(tweet_ids):
            url = f"https://weibo.com/ajax/statuses/show?id={tweet_id}"
            yield Request(url, callback=self.parse_tweet, errback=self.request_failed,
                          meta={'keyword': response.meta['keyword']}, priority=10)
        for next_page in response.css('a.next::attr(href)').getall():
            url = response.urljoin(next_page)
            parsed_url = urlparse(url)
            if parsed_url.scheme not in ('http', 'https') or parsed_url.hostname != 's.weibo.com':
                self.crawler.stats.inc_value('search/disabled_next_link')
                continue
            yield Request(url, callback=self.parse, errback=self.request_failed, meta=response.meta)
            break

    def parse_tweet(self, response):
        """
        解析推文
        """
        data = json.loads(response.text)
        item = parse_tweet_info(data)
        created_at = datetime.datetime.fromisoformat(item['created_at'])
        if not self.start_time <= created_at < self.end_time:
            self.crawler.stats.inc_value('search/outside_date_range')
            return
        item['keyword'] = response.meta['keyword']
        if item['isLongText']:
            url = "https://weibo.com/ajax/statuses/longtext?id=" + item['mblogid']
            yield Request(url, callback=parse_long_tweet, errback=self.request_failed, meta={'item': item}, priority=20)
        else:
            yield item
