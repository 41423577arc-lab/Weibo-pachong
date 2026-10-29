# 第 0 阶段：微博爬虫本机审计与重构准备

审计日期：2026-10-29（Asia/Shanghai）。初轮检查中断后，按用户“继续执行，不要考虑时间”的指示补齐报告。本文只记录检查结果和下一阶段建议；未实施正式重构。

## 1. 结论

- 应以 **`trae-modified` 的当前提交 + 本机全部未提交修改及未跟踪文件**作为后续工作基础，不能用远端覆盖工作区。
- 已创建并切换到本地分支 **`audit/phase0-20261029`**，HEAD 保持 `3adc84dee789935d482536926d51ea670e40f695`。分支没有包含未提交文件的快照；这些文件仍保留在共享工作区，未被 commit、stash 或复制到其他位置。
- 本机已提交代码与 fetch 后的 `origin/trae-modified` 相同，但整个工作区不相同：有 2 个已跟踪文件修改和 4 个未跟踪文件。存在本机新增功能，尚未进入提交或远端。
- 浏览器方式适合作为后续主后端的准备方向；HTTP/API 的解析、调度、游标和异常处理能力值得保留为可选适配器。两种后端的实时可用性均未验证，本轮未测试微博 403。
- 核心缺口是统一状态、跨任务/跨后端身份去重、正文完整性与任务完成语义。已有代码有较多可复用能力，不需要从零实现。
- 离线主环境共运行 79 项测试：78 项通过、1 项失败；浏览器环境另运行 21 项测试，全部通过（这些用例与主环境部分重复，不能加算为独立覆盖）。51 个 Python 源文件语法检查通过。

## 2. Git 与远端一致性

### 2.1 仓库定位与远端

用户工作目录 `D:/Projects/CourseProjects/identify_V` 本身不是 Git 仓库。通过 `git rev-parse --show-toplevel` 确认真实根目录为：

`D:/Projects/CourseProjects/identify_V/WeiboSpider`

origin 已核对为 `41423577arc-lab/Weibo-pachong`，检查时没有输出原始远端 URL 或认证信息。初轮及恢复后均执行了安全的 `git fetch origin`，退出码均为 0；只更新远程跟踪引用，没有 pull 或合并。

| 项目 | 审计结果 |
| --- | --- |
| 初始本地分支 | `trae-modified` |
| 最终本地分支 | `audit/phase0-20261029` |
| 本机 HEAD | `3adc84dee789935d482536926d51ea670e40f695` |
| `origin/trae-modified` | `3adc84dee789935d482536926d51ea670e40f695` |
| `origin/main` | `95b5f2186545142539af05a66ccc36ae825beb82` |
| HEAD 相对 `origin/trae-modified` 超前 / 落后 | `0 / 0` |
| 已提交但尚未推送的提交 | 相对 `origin/trae-modified` 没有 |
| 远端尚未包含于 HEAD 的提交 | 相对 `origin/trae-modified` 没有 |
| 已暂存但未提交的修改 | 没有 |
| 远端是否偏离用户提供的已知 SHA | 没有 |

创建分支前只有一个本地分支 `trae-modified`；准备分支从当前 HEAD 创建，未切换到其他提交，全部已有本机文件继续保留。

### 2.2 具体工作区差异

| 类型 | 文件 | 本机能力或变化 |
| --- | --- | --- |
| 未暂存修改 | `weibospider/history_collect.py` | 数据库名改为读取 `WEIBO_DB_NAME`，保留默认值 |
| 未暂存修改 | `weibospider/run_browser_tweets.py` | 长文展开超时记录、保留不完整正文后继续、已有缺失清单初始化、完成条件及续跑跳过条件变化 |
| 未跟踪 | `weibospider/collect_daily.py` | 按日断点调度，日期和数据库参数，browser/http 两个执行后端 |
| 未跟踪 | `weibospider/daily_request_worker.py` | 将日任务适配到现有 HTTP 历史采集器及检查点 |
| 未跟踪 | `weibospider/watch_daily.py` | 逐日状态与数据库计数监控，目前含固定项目时间范围和库名 |
| 未跟踪 | `逐日采集-20260924-20261007.md` | 本机采集说明文档；仅核对存在性和文件哈希，未据此宣称采集成功 |

两个已跟踪文件的差异合计 19 行新增、9 行删除。Git 提示正文脚本存在 LF/CRLF 转换设置；本轮未触发源码格式修改。

恢复工作后，将以上 6 个文件与初轮检查的 SHA-256 对照，全部一致。最终新增审计报告另计，不属于原有差异。

### 2.3 `main` 与 `trae-modified`

两个远端快照的文件差异只有以下 5 项：

- `.gitignore`：修改。
- `.vscode/settings.json`：新增。
- `weibospider/crawl_workflow_worker.py`：修改。
- `weibospider/diagnose_http_access.py`：修改。
- `weibospider/settings.py`：修改。

本地仓库是 shallow repository；当前可见历史中 `git merge-base HEAD origin/main` 未找到共同祖先。这不足以证明完整远端历史无共同祖先，本轮未 deepen/unshallow。快照检查显示 `main` 已含浏览器、工作流等文件，因此不能将两个分支简单理解为“只有 API”和“只有浏览器”的对应关系。

## 3. 本机专属资产与检查边界

| 路径 | 识别结果 | 本轮处理 |
| --- | --- | --- |
| 上级目录 `local-secrets.json` | 本机配置文件；配置模块支持向上一级兼容查找 | 未读取真实配置内容 |
| `weibospider/cookie.txt` | Git 忽略的 Cookie 文件 | 仅查看路径/存在性，未读取内容 |
| `.browser-venv/` | 已有浏览器 Python 环境，初轮清点 1,937 个文件、137,878,974 字节 | 使用已有解释器，不安装软件包 |
| `output/` | 初轮清点 2,571 个文件、436,924,184 字节，包含检查点、输出及浏览器资料所在目录 | 仅目录元数据检查，未改动或导入 |
| `logs/` | 初轮清点 18 个文件、274,819 字节 | 未执行会写入日志的采集命令 |
| 上级目录 `raw_data/`、`publish/`、`reports/` | 仓库外的本机资产目录 | 未搬移、覆盖、删除 |
| `database.sql` | 已跟踪 SQL 文件 | 未执行，不代表实时数据库结构 |

上述文件数量和字节数是初轮元数据快照，不代表数据质量、采集总量或当前实时状态。`.gitignore` 已覆盖常见凭据、数据库、日志、输出和浏览器资料路径。忽略规则不能替代备份，本轮也未做完整历史凭据扫描。

没有连接真实 MySQL 或打开既有 SQLite，因此数据库实际版本、表结构、数据行数、完整率和孤立记录数均未核验。没有打开 Chrome、读取 Cookie、修改浏览器用户资料或测试登录状态。

## 4. 运行环境与依赖

| 环境 | Python | 已安装的关键依赖 |
| --- | --- | --- |
| `C:/Users/86139/anaconda3/envs/identify_v/python.exe` | 3.10.22 | Scrapy 2.13.4、Twisted 25.5.0、cryptography 50.0.2、pyOpenSSL 26.4.0、PyMySQL 1.2.3、requests 2.34.2、python-dateutil 2.9.0.post0、parsel 1.12.1；没有 Playwright |
| `.browser-venv/Scripts/python.exe` | 3.11.5 | Playwright 1.63.0、PyMySQL 1.2.3；没有 Scrapy、Twisted、requests、python-dateutil、parsel |

本机 Chrome 可执行文件存在于 `C:/Program Files/Google/Chrome/Application/chrome.exe`，但没有启动验证。浏览器环境能够导入并运行浏览器辅助逻辑的离线测试，不等于浏览器启动或微博访问已通过。

`requirements.txt` 仍固定 Scrapy 2.5.1、Twisted 22.10.0、cryptography 36.0.2、pyOpenSSL 22.0.0，与主环境明显不一致；`requirements-identify-v.lock.txt` 中这些关键版本与主环境匹配。`requirements-browser.txt` 声明 Playwright `>=1.49,<2`，浏览器 lock 文件记录 1.63.0。后续应先明确依赖文件用途和支持环境，避免依照旧 requirements 重装而改变当前有效环境。本轮未安装、升级、卸载或完整解析依赖冲突。

## 5. 两套采集方案的可复用能力

| 目标环节 | 已有实现 | 评价与缺口 |
| --- | --- | --- |
| 搜索任务生成 | `crawl_workflow.py:new_round`、`history_collect.py:fresh_state`、`collect_daily.py` | 有关键词、日期、轮次/日任务；多个入口拥有独立状态 |
| 搜索页遍历 | `spiders/tweet_by_keyword.py`、`history_collect.py:parse_search`、`run_browser_tweets.py` | 有分页、日期范围和页面异常识别；浏览器依赖页面结构，未验证当前页面兼容性 |
| 帖子 ID 索引 | 搜索结果解析、短 ID/数字 ID 转换、`history_store_search_hits.py` | 有复用基础；浏览器搜索直接排入详情任务，尚无统一的独立持久化索引层 |
| 全局去重 | MySQL 组合主键、浏览器 `saved_sids` / `known_bodies`、SQLite 指纹 | 有局部去重及跨关键词正文复用；未建立统一实体身份 |
| 正文详情 | Scrapy/API、HTTP 历史采集、浏览器页面自身响应监听 | 保留原始 JSON、时间标准化及长文标记；有长文失败处理差异 |
| 评论及楼中楼 | `spiders/comment.py`、`browser_comment_collector.py`、`browser_comments_dom.mjs` | 有游标/滚动、回复展开、层级/父帖信息和预算限制；浏览器指纹不等于平台评论 ID |
| 断点与异常 | JSON 原子写、工作流锁、SQLite target/stream 状态、HTTP blocked/deferred/inflight | 可复用，但后端检查点与顶层状态有多套事实来源 |
| 入库与评估 | `mysql_pipeline.py`、`workflow_pipeline.py`、`workflow_store.py`、`history_report.py` | 有幂等写入、部分正文升级及统计；缺统一质量门槛与完整性口径 |

浏览器正文主要读取页面自身发出的 `/ajax/statuses/show` 和长文响应，并不是已实现了完整的纯 DOM 正文备用解析。页面正常显示但响应未按预期出现时，仍可能以 `post_body_not_loaded` 停止。API 适配器可以保留离线解析能力；没有实际访问证据时，不能宣布其线上可用或应直接删除。

## 6. 重构前必须明确的问题

### 6.1 任务完成与正文完整性混用

`run_browser_tweets.py:288` 的本机完成条件只检查队列和停止原因，不再要求 `incomplete_longtexts` 为空；同时 `:108` 将有正文内容的已有记录加入跳过集合，包括 `long_text_complete=False` 的记录。结果是“任务完成”可以同时存在未补全长文，普通续跑可能不再访问它们。应保留容错保存能力，并为后续补全任务明确状态，不能把完成标志解释为全文完整。

### 6.2 已有 API 测试与代码约定冲突

`crawl_workflow_worker.py:39` 明确豁免长文拒绝导致的整批停止，保存部分正文；`test_workflow_worker_offline.py:36` 仍要求长文 403 时调用 `close_spider`。这两个文件与 `origin/trae-modified` 相同，所以失败属于现有基线问题，不能归因于本轮审计或那两个本机已修改文件。后续先确定访问受限策略，再决定修代码还是更新测试，不应直接删除失败断言。

### 6.3 去重口径尚未统一

MySQL 主键为 `(spider, item_id, keyword)`，同一帖的不同关键词命中可以保存为多行；这适合保留命中关系，但并不构成全局实体唯一约束。`workflow_store.py:37` 对微博统计按 ID 去重，对评论按来源及 ID 区分，并明确标注 API 与 DOM 同一性未核实。SQLite DOM 指纹使用父帖、层级、根评论、用户、显示时间和正文，相同显示特征可能合并不同评论，显示时间或文本变化也可能产生新指纹。

下一阶段应区分帖子实体、关键词命中关系、采集观察记录和评论来源身份；无法证明相同的评论不能强行合并，也不能将跨来源相加当作平台唯一评论数。

### 6.4 状态与存储有多套事实来源

现有状态包括工作流 `state.json`、逐日 `daily-state.json`、浏览器正文检查点、HTTP history/http 检查点、SQLite targets/streams，以及 MySQL 内容记录。`MirroredBrowserCommentStore` 先保存 SQLite，再镜像 MySQL；不是跨库原子事务。已有测试验证 MySQL 失败时 SQLite 数据保留，后续仍需明确重放、同步确认和对账机制。

`workflow_store.read_records` 全量读取已有记录；逐日采集在多次任务中反复读库，数据增长后可能成为性能瓶颈。这里只作静态风险记录，未做性能测量。

### 6.5 日期、范围和初始化行为需统一

逐日入口的 `--end` 是包含结束日，底层任务通常使用排他结束时间；历史采集仍有模块级关键词/日期常量，监控脚本也有固定库名与日期范围。后续应在入口转换后使用统一的 `[start, end)` 时间契约。

`collect_daily.py:65` 会创建数据库及表，`MySQLPipeline.open_spider` 也包含建表行为。这些入口本轮没有执行；今后数据库初始化应与普通采集/离线验证边界明确区分。

### 6.6 完整性评估必须表达证据范围

`visible_exhausted` 只表示当前可见评论流达到页面结束标记，不代表历史全部评论。现有导出使用 `historical_total_complete=None` 是合理保留。建议下一阶段分别记录搜索任务覆盖、索引到正文转化、全文完整率、评论流完成状态、未展开回复、截断、异常和未完成任务；没有可靠分母时报告数量与未知项，不编造百分比。

## 7. 离线验证记录

主环境运行的 7 个测试模块：

- `test_access_diagnostics_offline`。
- `test_browser_comments_offline`。
- `test_comment_offline`。
- `test_history_offline`。
- `test_topic_keywords_offline`。
- `test_workflow_offline`。
- `test_workflow_worker_offline`。

结果为 **74 项、73 通过、1 失败、0 errors**。唯一失败为 `WorkerTests.test_longtext_failure_saves_partial_and_stops_on_rejection`，原因见 6.2。

另选取 `test_local_config_offline.ConfigurationTests` 中 5 项配置/脱敏测试运行，全部通过。排除了 `test_export_has_no_old_history_or_private_files_and_source_remains`：它会在临时仓库中执行 Git commit，故按本轮禁止 commit 的边界未执行。

浏览器专用环境运行 `test_access_diagnostics_offline` 和 `test_browser_comments_offline`，**21 项全部通过**。语法检查通过 AST 内存解析进行，未生成 pyc。

测试以 `-B` 禁止字节码写入；通过 Python audit hook 阻止外部连接、DNS 和真实子进程，通过替换 `pymysql.connect` 阻止真实 MySQL 连接。只允许 Windows asyncio 创建内部 socketpair 必需的回环连接；SQL 测试使用 Mock，SQLite 使用临时测试目录。初次保护器配置曾阻止 asyncio 初始化，调整为上述规则后才得到完整的 74 项结果；这些中断不计入通过数。没有启动采集入口、浏览器或发布脚本。

## 8. 下一阶段实施准备建议（尚未执行）

1. 继续在当前准备分支评审本机新增功能；未取得额外指示前，不自动 commit 或覆盖。后续获准提交时，需同时考虑未跟踪代码及本机文档，避免遗漏。
2. 先约定统一任务身份、状态迁移、结束时间和质量口径，将任务完成与数据完整分开；为部分正文保留补全任务。
3. 以现有工作流作为候选调度基础，把浏览器和 API/HTTP 封装为采集适配器；状态写入与存储写入由共同接口负责，避免让适配器各自维护最终业务状态。
4. 确定唯一任务状态来源及内容存储职责；如保留 SQLite 缓冲和 MySQL，明确哪一层是权威、如何幂等重放和确认同步。本轮不预先决定迁移方案，更不修改数据库。
5. 先建立离线契约用例：跨关键词重复 ID、同帖多来源、长文失败后补全、日期边界、分页循环、崩溃后重放、镜像失败、楼中楼未完成，以及完整正文不被部分正文覆盖。
6. 在后续明确授权允许联网验证后，才使用小规模正常浏览器任务检查页面兼容性；当前不因历史 403 结果推断访问已恢复，也不以自动切换后端重试取代访问异常记录。

## 9. 最终变更范围

本轮持久化动作只有：安全 fetch、创建并切换本地准备分支，以及新增本文。现有源码、采集数据、配置和浏览器资料未编辑、删除、覆盖或移动。没有 commit、stash、pull、merge、rebase、push、reset、clean；没有安装软件包、真实采集或数据库结构/数据写入。

最终报告处于未跟踪状态，尚未提交。准备工作已完成；正式重构、线上可用性验证、生产数据完整性检查和数据库迁移均未开始。
