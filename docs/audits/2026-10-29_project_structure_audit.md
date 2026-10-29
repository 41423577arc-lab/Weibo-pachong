# 项目结构专项审计与重构规范

日期：2026-10-29。依据：[第 0 阶段审计](2026-10-29_phase0_audit.md)。本文为实施前规划，所有目标模块均尚未创建，采集逻辑未修改。

## 1. 安全保存与归档结果

对 history_collect.py、run_browser_tweets.py、collect_daily.py、daily_request_worker.py、watch_daily.py 和《逐日采集-20260924-20261007.md》检查了实际内容及修改范围。采用项目已有敏感信息检测器、本机配置中已知密码/Cookie 的内存匹配，以及 Token、Bearer、认证 URL、私钥等补充模式扫描，未发现敏感信息。没有输出真实凭据。5 个文件为代码，1 个为相关使用和运行说明，未混入原始采集记录、数据库或无关文件。模式检查不等于数学意义的安全保证。

已按用户本轮明确授权，将且仅将这 6 个文件提交为：

`cdd012cd7e27f164e13f615d9fd2fcac6f6a748a` — 保存逐日采集及长文处理的本机修改

提交父节点为 `3adc84d`。随后从该本地提交创建并切换至 `refactor/staged-crawl-20261029`，未从旧远端重新起分支。没有推送。之前报告的“尚未提交”是当时状态，历史报告保留原文，以本补充记录说明状态变化。

审计目录确定为仓库内 `docs/audits/`。仓库外的 `../reports/` 不随本仓库版本管理，且包含 JSON 运行摘要；因此不使用它作为本项目长期人工审计的归档位置。`git check-ignore` 确认目标报告路径未被忽略，无需修改忽略规则。

已将根目录 `PHASE0_AUDIT_20261029.md` 安全移动为 `docs/audits/2026-10-29_phase0_audit.md`。移动前验证目标不存在、解析后的路径位于当前仓库，移动后 SHA-256 一致。当前仓库文本及上级 reports 中未查到旧文件名的直接引用，故没有需要修正的旧相对链接。聊天中的旧链接无法作为文件改写，新路径见上方链接。本专项报告也归入同一目录，两个报告均未提交。

## 2. 实际目录与问题

以下为扫描得到的结构摘要；省略缓存、第三方环境文件及数据目录内部文件名，不省略其存在性。

```text
identify_V/                       # 仓库上级，不是本仓库根
├─ reports/                       # 仓库外
│  ├─ collection_20261020.md
│  └─ collection_20261020_summary.json
├─ raw_data/、publish/、data_preprocessing/
├─ local-secrets.json、.gitignore
└─ WeiboSpider/                   # 真实 Git 根目录
   ├─ .git/、.githooks/pre-push、.github/、.vscode/
   ├─ .gitignore、README.md、LICENSE
   ├─ requirements*.txt、environment.yml、database.sql
   ├─ 多份中文报告/说明及 BROWSER_COMMENTS.md
   ├─ PHASE0_AUDIT_20261029.md     # 整理前；现已迁入 docs/audits
   ├─ .browser-venv/              # 忽略
   ├─ logs/                      # 仓库内、忽略
   ├─ output/                    # 仓库内、忽略
   │  └─ 多个诊断/验证/逐日/工作流输出目录及浏览器资料
   └─ weibospider/
      ├─ 入口、调度、存储、解析、测试平铺
      ├─ browser_comments_dom.mjs
      ├─ spiders/                # 关键词、用户、帖子、评论等 Scrapy spider
      ├─ cookie.txt              # 忽略，未展示内容
      └─ __pycache__/            # 忽略
```

整理前仓库内没有 docs/ 和 reports/；本轮仅新增 docs/audits。根目录报告、指南、SQL 和依赖文件混排；测试与可执行脚本混排；多个入口拥有独立状态；根目录定位依赖 `__file__.parents[1]`，JSONL 输出还依赖当前工作目录。机械移动文件会改变数据、凭据和资料定位。

实际依赖包括：collect_daily → crawl_workflow 的锁/JSON 写入；daily_request_worker → history_collect 并修改模块常量；watch_daily → workflow_store；run_browser_tweets → crawl_workflow、run_browser_comments、workflow_pipeline、workflow_store；workflow_store → extract_topic_keywords 的 PROJECT/常量。底层模块从功能脚本获取路径常量造成职责倒置。fan/follower 还从 comment 模块获取 parse_user_info，应在后续共用解析函数迁移时检查。尚未据此判定存在真实 import 环，不能将潜在循环风险说成已证实的循环。

## 3. 文档分类、引用及历史迁移建议

| 现有文件/位置 | 分类与建议 | 已发现的路径关系/限制 |
| --- | --- | --- |
| 403诊断与修复-2026-10-14.md | 长期诊断，建议将来归 audits | 本轮不移动；迁移时核对文内相对命令和链接 |
| 评论验证报告-2026-10-11.md、评论单请求验证-2026-10-11.md、评论接口诊断-浏览器参数对照.md | 长期验证证据，建议 audits | 保留原文及日期，先脱敏和确认引用 |
| 采集结果-2026-10-08.md | 采集结果总结，建议 audits | 原始数据继续留在原位置 |
| 微博采集与评论修复报告-2026-10-11.md | 可作为审计快照，但由程序生成 | history_report.py:123 写入此根目录文件；不能只移动成品 |
| ../reports/collection_20261020.md | 仓库外总结，建议人工审查后选择性归档 | 来源与外部调用者本轮未穷尽；不直接跨项目搬迁 |
| 本次采集说明.md、逐日采集-20260924-20261007.md | 运行说明兼历史记录，建议 docs/guides 或拆出经审阅的审计快照 | 逐日文档引用 output 检查点和旧脚本命令；本轮保持刚提交版本 |
| README.md | 根入口保留 | 已有安全发布说明等相对链接，后续搬指南须更新 |
| 安全发布说明.md、使用说明-本机数据库.md、循环采集工作流.md、话题关键词提取说明.md、BROWSER_COMMENTS.md | 长期使用指南，建议 docs/guides | 不把所有 Markdown 都当成审计；保留旧命令兼容期 |
| logs/*.log、*.json；output 下 JSON/SQLite/浏览器资料 | 程序运行产物，原地保留 | verify_project 读 logs/last-result.json；workflow_store 读历史评论 JSON；不能迁入 audits |
| output/**/review_status.md、topic_keywords.md | 自动生成的运行视图 | crawl_workflow.py:157、extract_topic_keywords.py:210 持续生成；tests 使用临时路径验证，保持其运行属性 |
| ../reports/collection_20261020_summary.json | 原始运行摘要 | 不归入人工审计目录 |

history_report 还读取 logs/history-baseline.json，history_store_search_hits 写 logs/history-search-index-verification.json；diagnose_http_access/diagnose_browser_access 写 output/access-diagnostic。文件扩展名不能决定归档性质。长期人工审计应使用 `YYYY-MM-DD_主题_audit.md` 等明确名称，带来源、时间和验证范围；机器生成结果经审阅可形成独立脱敏摘要，原产物仍不移动。

引用搜索覆盖仓库代码、测试、Markdown、隐藏配置（排除 Git、环境、运行数据）及上级 reports；不声称外部任务计划、终端历史或其他项目没有调用者。历史文档全部只列建议，不批量迁移。

## 4. 建议目标结构

```text
WeiboSpider/
├─ README.md、LICENSE、依赖与环境声明
├─ docs/
│  ├─ audits/                     # 人工审计与经审阅的验证报告
│  └─ guides/                     # 使用说明、重构约定
├─ logs/、output/                 # 既有路径保留，不随源码搬迁
└─ weibospider/
   ├─ __init__.py
   ├─ main.py                    # CLI: collect/resume/status/validate
   ├─ core/
   │  ├─ config.py               # 稳定项目根与数据路径、配置
   │  ├─ models.py               # 共享任务/记录/结果契约，无数据库依赖
   │  ├─ scheduler.py            # 唯一业务调度、时间窗口与后端选择
   │  ├─ state.py                # 状态迁移规则和锁契约
   │  └─ identity.py             # 平台 ID 规范化、去重身份
   ├─ stages/
   │  ├─ search.py               # 任务生成、索引及关键词命中关系
   │  ├─ detail.py               # 正文及补全策略
   │  ├─ comments.py             # 一级/楼中楼阶段
   │  └─ quality.py              # 覆盖、完整性和异常汇总
   ├─ backends/
   │  ├─ api/                    # Scrapy spiders、HTTP 请求/解析、worker
   │  └─ browser/                # 会话、页面响应、DOM 与评论交互
   ├─ storage/
   │  ├─ repository.py           # 幂等持久化契约
   │  ├─ mysql.py                # MySQL 实现及序列化映射
   │  └─ sqlite.py               # 任务状态/缓冲实现，具体权威方案待定
   ├─ tests/                     # 离线、回归、合成 fixtures
   └─ 原入口兼容脚本             # 过渡期保留，全部委托统一调度
```

无需为每个小函数单建文件。共享模型放 core，数据库映射放 storage，避免 models 双向依赖。main 负责组装并注入后端/存储；scheduler 调用 stages；stages 使用契约；后端只采集和解析，返回记录、游标和终止原因。core/models 不导入 stages、后端或数据库实现；后端不反向导入 main/scheduler，storage 不依赖采集后端。锁/原子写等小工具在现有职责模块内复用，暂不新增泛化框架。

统一任务状态并不要求两个技术环境合并：Scrapy reactor 和 Playwright 可继续在不同进程/解释器运行，但只能消费同一任务协议并返回统一结果，不能再各自拥有一套业务工作流。

## 5. 原文件到目标模块映射

| 现有模块 | 目标职责/模块 | 保留与融合策略 |
| --- | --- | --- |
| crawl_workflow.py | main + core/scheduler、state + stages | 保留关键词审核、轮次、恢复、锁；拆出底层工具，旧 CLI 兼容 |
| collect_daily.py | main collect 参数 + scheduler 时间窗口 | 按日只是任务生成策略，不保留第二套完成状态；建库行为与采集分离 |
| daily_request_worker.py | backends/api/worker | 保留进程隔离和 HTTP 适配；替换修改模块全局常量的传参方式 |
| watch_daily.py | main status（含 watch 选项） | 查询统一状态，去除固定日期/库名；不另存状态 |
| history_collect.py | backends/api/http + stages/search/detail + core/state | 保留解析、请求预算、拒绝/游标证据；历史检查点通过兼容读取器接入 |
| run_browser_tweets.py | backends/browser/tweets + stages/detail | 保留正常页面响应、长文标记、日期缓存；去重、写库和任务完成规则移到共享层 |
| run_browser_comments.py、browser_comment_collector.py、browser_comments_dom.mjs | backends/browser/comments、driver、DOM 资源 + stages/comments | 页面滚动和展开留后端；统一任务状态和父子关系契约 |
| browser_comment_store.py | storage/sqlite + core/identity | 保留指纹来源及局限、SQLite 数据；不把指纹伪装成平台 ID |
| workflow_store.py | storage/mysql、repository + stages/quality | 去除从关键词脚本导入项目根；保留镜像重放和统计证据 |
| mysql_pipeline.py、workflow_pipeline.py、comment_pipeline.py | storage/mysql + API pipeline 薄适配层 | 统一幂等及完整正文升级规则，保留 Scrapy 接口 |
| crawl_workflow_worker.py、spiders/tweet_by_keyword.py、spiders/comment.py | backends/api | 保留 Scrapy 生命周期、分页/游标、部分正文；冲突策略先定义再修改 |
| spiders/common.py | core/identity + backends/api/parsers | 通用 ID 转换共享；具体接口字段解析留 API |
| local_config.py | core/config | 保留环境变量、指定文件和父目录兼容优先级；迁移模块不改变项目根 |
| comment_run.py、run_comments.py、run_project.py、run_spider.py | stages/comments、main + 旧入口适配 | 兼容既有调用命令，逐步委托统一入口 |
| history_store_search_hits.py | stages/search + storage | 索引与正文实体分开，保留关键词/时间窗口命中关系 |
| history_report.py、verify_project.py | stages/quality + main validate | 保留读回评估；输出目录参数化，旧文件路径兼容 |
| access_diagnostics.py、diagnose_http_access.py、diagnose_browser_access.py | quality 与对应后端诊断 | 保留脱敏异常分类，联网诊断必须显式运行 |
| extract_topic_keywords.py | stages/search 中关键词扩展工具 | 保留人工审核与正文话题提取，避免存储依赖此工具 |
| test_*_offline.py | tests/ 对应模块 | 保留 fixtures 和基线失败，改成包导入后运行验证 |
| settings.py、middlewares.py、pipelines.py | backends/api 对应配置/适配 | 检查动态字符串类路径与相对输出路径后再迁移 |
| 其他 user/fan/follower/repost/tweet spiders | backends/api/spiders 或原位保留 | 本次主流程以外能力不删除，确认 run_spider 及外部用户调用 |
| publish_safety.py、check_publish.py、prepare_publish.py | 暂保留原位 | .githooks/pre-push 直接调用旧路径；可后续迁至工具目录并同步 hook/tests |

main 与 trae-modified 的能力按文件比较，不能按名字取舍：两者已有浏览器和工作流；现有五项快照差异及 shallow 历史限制见初轮报告。保留 main 中共有的解析/采集能力，保留 trae 的诊断与配置改进；长文拒绝策略存在测试冲突，需单独决策。本机六文件的能力已经提交为基线，不能回退到远端而丢失。

## 6. 保留、融合与删除门槛

- 保留：全部历史数据、JSON/SQLite 检查点、失败事件、Cookie/用户资料位置、历史报告、许可与来源说明、现有测试及非主流程 spider。
- 融合：每日与轮次调度、正文/评论完成判定、去重身份、MySQL 写入和 SQLite 同步职责，按上表拆分而不是复制两套流程。
- 暂缓删除：所有旧入口、history_collect、工作流 worker、发布工具、旧配置模块；当前已有直接 import、子进程命令、文档或 hook 调用证据。
- 当前可立即删除清单：空。
- 将来待核验的删除候选：完成委托后的 collect_daily/daily_request_worker/watch_daily 等旧包装，以及被证明无调用者的重复实现。必须同时检查静态 import、动态配置字符串、subprocess 命令、文档、测试及外部调度；兼容期与回归通过后才列入可删除清单。

数据保护要求：固定数据根路径，源码位置不决定旧数据搬迁；检查点设版本，旧格式只读识别后转换到新文件，保留原文件及失败事件；数据库迁移先备份和独立验证再另行执行；对幂等重放、部分记录升级、SQLite/MySQL 同步失败做回归。不得将旧 JSON 的 complete 直接等同全文完整。

## 7. 正式重构中文注释规范

1. 核心 Python 模块首部用简短中文 docstring 写职责与边界；主要函数用中文 docstring 说明作用。复杂函数说明关键输入、返回结果、状态变化、异常或持久化副作用。
2. 必须解释搜索翻页与时间窗口边界、帖子 ID 提取/规范化、跨关键词命中保留与去重、API/浏览器选择、JSON/DOM/XPath/CSS 解析假设。
3. 必须解释一级评论与楼中楼父子关系、游标终止与重复评论、403/长文不完整/其他异常的判定和后续状态、断点重放、状态转换、数据库幂等和完整性校验。
4. JavaScript 使用 `//` 中文注释说明 DOM 选择器依据、可见性、评论归属和缺失字段处理。技术名词、标识符和第三方接口保留英文。
5. 统一 UTF-8；说明功能、原因和关键条件，不逐行翻译语法。注释描述实际实现及证据边界，不能把 visible_exhausted 写成历史全量，也不能把 DOM 指纹写成平台 ID。
6. 每次行为修改同步审查相关注释/docstring；过时注释必须更新。测试预期与注释不一致时先确定契约，不能靠改注释掩盖失败。

示例（规划示意，未加入源码）：

```python
def deduplicate_tweets(records):
    """按平台微博 ID 合并帖子，同时保留所有命中的关键词。"""
    # 使用规范化后的平台 ID 判断帖子身份，关键词只作为命中关系保存。
    ...
```

## 8. 渐进式实施顺序与现有失败

1. 以 cdd012c 为基线，先确认现有 1 项长文拒绝测试的契约冲突，记录而非悄悄删除；冻结旧入口及路径行为。
2. 提取稳定路径配置、共享模型、ID 和状态契约，建立包导入；保留旧入口代理，测试不同 cwd 与两个运行环境。
3. 先接入共享存储契约和旧检查点兼容读取，验证幂等重放/失败恢复，不同时迁移生产数据。
4. 将日任务生成并入唯一调度；status 只读统一状态；保留关键词审核流程。
5. 按搜索索引→正文→评论顺序接入浏览器适配器，每次保留旧实现用于离线对照。
6. 将 HTTP/Scrapy 接入同一任务协议，保留技术环境隔离，不复制完整业务工作流。
7. 统一质量报告，区分任务执行结束、正文完整、评论可见流采尽及未知历史覆盖；整理指南和生成器输出约定。
8. 回归与调用者检查通过后，再提出旧文件删除候选。联网验证、数据迁移及删除需在后续对应任务中执行。

初轮主环境 79 项测试中 1 项失败：test_longtext_failure_saves_partial_and_stops_on_rejection，代码豁免长文拒绝的整批停止而测试要求停止；浏览器环境 21 项通过。此次文档整理不修改逻辑，也未重复运行这些测试。依赖冲突风险还包括旧 requirements 与当前已安装版本不一致、裸 import 和动态 Scrapy 类路径迁移、两环境依赖不同；本轮只记录。

本轮到此停止：唯一代码相关操作是用户明确授权的六文件基线提交，目录变更仅为审计文档归档和本报告新增；没有实际重构、旧模块删除、忽略规则修改或远端推送。
