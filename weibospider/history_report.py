"""Generate a source-backed history report from MySQL, checkpoints, and baseline."""
from local_config import read_config
import collections
import json
from pathlib import Path

import pymysql
from history_collect import PROJECT, STATE_PATH, KEYWORDS, DATES, START, END, write_json


def main():
    state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    config = read_config()
    db = pymysql.connect(host=config.get('WEIBO_DB_HOST') or '127.0.0.1',
        port=int(config.get('WEIBO_DB_PORT') or 3306), user=config['WEIBO_DB_USER'],
        password=config['WEIBO_DB_PASSWORD'], database='weibo_data', charset='utf8mb4', connect_timeout=10)
    try:
        with db.cursor() as cur:
            cur.execute('SELECT spider,item_id,keyword,crawl_time,payload FROM crawl_items ORDER BY spider,item_id,keyword')
            rows = cur.fetchall()
    finally:
        db.close()
    baseline = json.loads((PROJECT / 'logs/history-baseline.json').read_text(encoding='utf-8'))
    current = {(r[0], r[1], r[2]): (r[3], json.loads(r[4])) for r in rows}
    preserved = all(current.get((r[0], r[1], r[2])) == (r[3], json.loads(r[4])) for r in baseline)
    tweets = [json.loads(r[4]) for r in rows if r[0] == 'tweet_spider_by_keyword'
              and r[2] in KEYWORDS and START <= json.loads(r[4]).get('created_at', '') < END]
    mids = {str(d['_id']) for d in tweets}
    comments = [json.loads(r[4]) for r in rows if r[0] == 'comment' and json.loads(r[4]).get('tweet_id') in mids]
    search_hits = [json.loads(r[4]) for r in rows if r[0] == 'history_search_hit']
    by_mid = collections.Counter(d['tweet_id'] for d in comments)
    batches = []
    for date in DATES:
        for keyword in KEYWORDS:
            key = f'{keyword}|{date}'
            found = [d for d in tweets if d['keyword'] == keyword and d['created_at'][:10] == date]
            ids = {str(d['_id']) for d in found}
            linked = [d for d in comments if d['tweet_id'] in ids]
            batches.append({'date': date, 'keyword': keyword,
                'raw_visible_search_hits': state['raw_search_hits'].get(key, 0),
                'search_pages_read': state['search_pages'].get(key, 0),
                'stored_tweet_keyword_rows': len(found), 'unique_tweets_in_batch': len(ids),
                'level_1_comments': sum(d['comment_level'] == 1 for d in linked),
                'level_2_replies': sum(d['comment_level'] == 2 for d in linked),
                'stored_search_index_rows': sum(d['keyword'] == keyword and d['requested_date'] == date for d in search_hits),
                'search_status': state['batch_status'][key]})
    fields = ['_id', 'mblogid', 'url', 'created_at', 'crawl_time', 'content', 'keyword',
              'reposts_count', 'comments_count', 'attitudes_count', 'is_retweet', 'raw_api_json']
    nonempty = {k: (sum(k in d and d[k] is not None and d[k] != '' for d in tweets) / len(tweets)
                   if tweets else None) for k in fields}
    summary = {'scope': {'keywords': KEYWORDS, 'start': START, 'end_exclusive': END, 'timezone': 'Asia/Shanghai'},
        'batches': batches, 'unique_tweets_across_keywords': len(mids), 'tweet_keyword_rows': len(tweets),
        'comments': len(comments), 'level_1': sum(d['comment_level'] == 1 for d in comments),
        'level_2': sum(d['comment_level'] == 2 for d in comments),
        'comments_by_tweet': dict(by_mid), 'existing_baseline_rows': len(baseline),
        'existing_rows_preserved': preserved, 'database_total_rows': len(rows),
        'http_requests': state['requests'], 'http_status': state['http_status'],
        'network_failures': sum(e['event'] == 'network_failure' for e in state['events']),
        'http_200_rate': state['http_status'].get('200', 0) / state['requests'] if state['requests'] else None,
        'pending_tasks': len(state['queue']), 'deferred_tasks': len(state['deferred_tasks']),
        'stop_reason': state.get('stop_reason'), 'blocked_routes': state['blocked_routes'],
        'elapsed_collection_seconds': state['spent_seconds'], 'field_nonempty_rates': nonempty,
        'comment_association_verified': None if not comments else all(d['tweet_id'] in mids for d in comments),
        'date_scope_verified': None if not tweets else all(START <= d['created_at'] < END for d in tweets),
        'pilot_passed': state['pilot_passed'], 'raw_json_rows': sum('raw_api_json' in d for d in tweets)}
    summary['stored_search_index_rows'] = len(search_hits)
    summary['distinct_observed_search_ids'] = len({d['tweet_id'] for d in search_hits})
    write_json(PROJECT / 'logs/history-verification.json', summary)
    lines = ['# 微博采集与评论修复报告（2026-10-11）', '',
        '## 最终结论与当前阻塞', '',
        f"历史范围：北京时间 {START} 至 {END}，结束时间不包含。三个关键词分别保存检索命中。",
        f"MySQL 回读：本轮历史微博 {len(mids)} 个不同 ID，关键词记录 {len(tweets)} 条；一级评论 {summary['level_1']} 条，二级回复 {summary['level_2']} 条。",
        f"另将已观察到的 {len(search_hits)} 条搜索命中索引存入 spider=history_search_hit，包含 {summary['distinct_observed_search_ids']} 个不同微博 ID。它们只有页面链接、作者 ID 和搜索上下文，没有正文；发布日期尚未核实，不能计入历史正文采集量。",
        f"原有 {len(baseline)} 条记录逐键及内容比对：{'全部保持不变' if preserved else '检测到变化，需要检查'}。没有修改其他数据库、账户、服务或全局配置。", '',
        '本轮未达到历史正文及评论的真实采集目标。评论路径此前明确返回 403，未继续试发。',
        'Chrome 扩展两次无法连接（nodeRepl.fetch request failed）；Windows 电脑操作无法可靠识别浏览器 URL，工具强制停止；未发现现成的 Chrome 远程调试连接。没有开启新的调试端口、导出浏览器 Cookie 或绕过验证。',
        '历史正文小样本：搜索页面可读取，但首条详情接口返回 HTTP 403，因此暂停详情，不进入正式正文采集。后续仅检查其他未受阻搜索首页的可见命中，不替换接口强行获取受限正文。', '',
        '## 评论 403 诊断与修复证据', '',
        '浏览器与代码路径相同：GET /ajax/statuses/buildComments。微博数字 ID 正确，uid 从现有微博作者字段读取。已对齐 is_show_bulletin=3、count=10、fetch_level=0、locale=zh-CN，首个一级请求不附加 max_id。',
        '参数对齐后的既有单请求仍是 403，响应可解析为 JSON，没有提取到数字型业务错误码。不能断定是 Cookie 时效、请求头、客户端行为或访问权限中的哪项原因。浏览器能访问与独立客户端被拒绝可以同时成立。',
        '评论解析保存 api_rootid、tweet_id、root_comment_id、parent_comment_id、comment_level，并区分直接回复字段和线程根回退；二级游标和内嵌回复去重有离线覆盖。尚无真实样本证明一级、二级关联或入库成功。', '',
        '## 按日期与关键词的结果', '',
        '| 日期 | 关键词 | 已读搜索页 | 页面微博链接数 | 命中索引入库 | 入库正文 | 一级评论 | 二级回复 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for b in batches:
        lines.append(f"| {b['date']} | {b['keyword']} | {b['search_pages_read']} | {b['raw_visible_search_hits']} | {b['stored_search_index_rows']} | {b['stored_tweet_keyword_rows']} | {b['level_1_comments']} | {b['level_2_replies']} |")
    lines += ['', '页面微博链接数是已访问页面观察到的链接计数，不是平台全量命中数；未读取的页或批次不记为无搜索结果。跨关键词唯一量按微博 ID 去重，不能把三个关键词数量相加作为唯一微博总数。', '',
        '## 请求、质量和完整性', '',
        f"本轮历史 HTTP 请求：{state['requests']}；状态码分布：`{json.dumps(state['http_status'])}`；网络异常 {summary['network_failures']} 次；HTTP 200 比例 {summary['http_200_rate'] if summary['http_200_rate'] is not None else '未定义'}。",
        f"历史采集累计运行约 {state['spent_seconds']:.1f} 秒，预算上限 7200 秒 / 2000 次请求。",
        f"待处理任务 {len(state['queue'])} 个，受阻或失败保留任务 {len(state['deferred_tasks'])} 个；停止原因 `{state.get('stop_reason')}`。",
        '没有真实历史入库样本时，字段非空率、日期正确性、原始 JSON 保存、每条历史微博评论量和评论关联都标为未验证，不能用空样本声称检查通过。',
        '微博正文、媒体 URL、作者及互动字段的解析已具备；成功获取后完整详情 JSON 保存到 payload.raw_api_json，长文补充 JSON 保存到 payload.raw_longtext_json。当前实际原始 JSON 入库数量见回读统计。',
        '“第五人格”为宽泛关键词。仅搜索首页不足以证明覆盖率；后续页、未完成日期和关键词均在状态文件中保留。不声称已获取全部微博或评论。', '',
        '## 代码改动', '',
        '- `spiders/comment.py`：作者 ID、请求参数、根评论/父评论关联、分页、403 停止。',
        '- `run_comment_test.py`：可选单微博、单请求测试，读取作者 ID，核查现有记录保持不变。',
        '- `comment_pipeline.py`：评论使用 INSERT IGNORE，保留原有记录。',
        '- `history_collect.py`：单进程低频历史任务、日/关键词分批、请求和页数预算、JSON 断点、缓存复用、未决请求保护、访问拒绝停止、只插入不更新。',
        '- `test_comment_offline.py`、`test_history_offline.py`：合成数据测试，不向微博或 MySQL 写测试数据。17 项测试通过。',
        '- `history_report.py`：从 MySQL、原始备份和状态文件重新生成统计和本报告。', '',
        '- `history_store_search_hits.py`：只读取已有断点，将真实观察到的搜索链接标为索引保存，不请求网络、不伪装成完整微博。', '',
        '## 未完成任务与恢复方法', '',
        '评论需要恢复已授权 Chrome 的工具连接，或用户提供平台允许的授权读取方式；若出现登录或人工验证，由用户操作。接口允许访问前不重复请求。二级请求参数仍需从正常展开回复的浏览器请求确认。',
        '历史正文需要正常的详情接口访问恢复并通过一次真实入库试验；恢复前不清除 blocked_routes 或重新请求已拒绝的任务。',
        '`logs/history-state.json` 保存每个待完成任务的日期、关键词、页码、微博 ID、游标/URL、拒绝路径和请求预算。`logs/history-baseline.json` 是原表只读备份，`logs/history-verification.json` 是回读审计。', '',
        '在 Anaconda Prompt 中：', '', '```bat', 'conda activate identify_v',
        'cd /d D:\\Projects\\CourseProjects\\identify_V\\WeiboSpider\\weibospider',
        'python -m unittest -v test_history_offline test_comment_offline',
        'python history_report.py', '```', '',
        '未受访问拒绝阻塞、仅因预算或普通中断停止时，可运行 `python history_collect.py`，它默认加载同一断点及累计请求预算，不重复下载已缓存响应。`inflight` 非空时需先检查该次请求结果，不会自动重发。',
        '当前访问被拒绝，因此不能通过直接重跑绕过阻塞。访问确实恢复后，先小规模验证，再由用户明确授权恢复受阻任务；保留原计数、缓存和旧状态，不重置历史证据。',
        '在正常授权访问确实恢复、用户明确确认后，可运行 `python history_collect.py --access-restored detail --pilot-only` 做一次恢复试验。该参数不更改身份或绕过验证，只恢复所选路径的受阻任务并保留过去失败证据及累计预算；本轮未执行它。再次出现 403 会再次停止。',
        '修改未来独立任务的关键词和日期：在 history_collect.py 中调整 KEYWORDS、DATES、START、END，使用新的任务状态路径；不要覆盖本轮断点。当前任务配置固定为目标文件要求的三个关键词和三天。', '',
        'MySQL 查询示例：', '', '```sql',
        "SELECT keyword, LEFT(payload->>'$.created_at',10) AS publish_date, COUNT(*) AS rows_per_keyword",
        'FROM weibo_data.crawl_items', "WHERE spider='tweet_spider_by_keyword'",
        "  AND keyword IN ('第五人格','第五人格亚运会','第五人格名古屋亚运会')",
        "  AND payload->>'$.created_at'>='2026-09-30 00:00:00'",
        "  AND payload->>'$.created_at'<'2026-10-03 00:00:00'",
        'GROUP BY keyword, publish_date;', '```', '',
        '本报告不包含 Cookie、数据库密码或其他认证信息。']
    report = PROJECT / '微博采集与评论修复报告-2026-10-11.md'
    report.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['unique_tweets_across_keywords', 'comments',
        'existing_rows_preserved', 'http_requests', 'http_status', 'pending_tasks', 'stop_reason']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
