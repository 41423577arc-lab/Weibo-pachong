# 本机浏览器评论采集

入口：`weibospider/run_browser_comments.py`。使用 Playwright 启动已安装的 Chrome，读取正常渲染的微博评论和回复，不直接请求评论接口，不读取或导出 Cookie、浏览器存储或令牌。Chrome 在独立配置目录中自行维护登录会话。

## 运行

在 PowerShell 中进入 `D:\Projects\CourseProjects\identify_V\WeiboSpider`：

```powershell
python -m venv .browser-venv
.\.browser-venv\Scripts\python.exe -m pip install -r requirements-browser.lock.txt
```

`requirements-browser.lock.txt` 固定了本次已安装并检查的依赖版本；`requirements-browser.txt` 是版本范围配置。程序使用 `channel='chrome'`，因此无需下载 Playwright Chromium。依赖安装完成后，预览近期目标（只读已有 MySQL，不启动浏览器）：

```powershell
.\.browser-venv\Scripts\python.exe weibospider/run_browser_comments.py --from-db --since 2026-10-08 --preview
```

也可使用已有 `logs/comment-targets.json`，省略 `--from-db`。自定义目标文件是 JSON 数组，每项至少有数字 `tweet_id` 和 `https://weibo.com/作者ID/短微博ID` 的 `url`；只接受正常微博详情 URL。

如需登录，运行下面命令，在新开的 Chrome 窗口中手动完成登录，再回到终端按 Enter：

```powershell
.\.browser-venv\Scripts\python.exe weibospider/run_browser_comments.py --login
```

先运行一条验证，再运行全部目标：

```powershell
.\.browser-venv\Scripts\python.exe weibospider/run_browser_comments.py --from-db --since 2026-10-08 --limit 1
.\.browser-venv\Scripts\python.exe weibospider/run_browser_comments.py --from-db --since 2026-10-08
```

## 保存、恢复与结果解释

默认输出目录为 `output/browser-comments`：

- `comments.sqlite3`：每轮读取后事务提交评论和进度，是恢复依据。
- `comments.json`：自动导出的可读结果，通过临时文件替换，保留每条微博的状态和原因。
- `browser-profile/`：专用 Chrome 配置目录，后续复用同一登录会话。
- `collector.lock`：防止同一输出目录被两个进程同时采集；异常退出后操作系统释放锁。

重复运行同一命令即可恢复。已完成目标会跳过；未完成微博从页面起点重放，以本地键去重，已完成回复线程跳过。重放不是从任意滚动位置精确接续。使用 `--refresh` 重新访问已完成目标，同时保留旧记录；如果只需要新的采集快照，改用新的 `--output` 路径。更换输出目录时，可用 `--profile` 指向同一专用配置目录，但不能同时运行使用该配置的浏览器。

默认每次滚动间隔 2 秒，每个一级/回复流最多 500 轮，每条微博最多 1000 个回复线程。可通过 `--interval`、`--max-passes`、`--max-reply-threads` 调整；预算达到上限会标为不完整。无法加载、登录要求、验证或访问拒绝会保存进度并停止批次，后续目标仍为待处理。不要反复重试未解除的验证；用 `--login` 人工检查后再恢复。

状态 `visible_exhausted` 仅表示正常页面已显示可见结束标记，且已发现的回复线程处理完成；不代表历史评论总数完整。`incomplete` 表示滚动停滞、预算不足、正文截断或回复未完成；`needs_attention` 表示需要人工处理。退出码 0 表示所选目标的可见流结束（可能是空结果），5 表示仍不完整，2 表示启动/配置失败，130 表示人工中断。

正文、表情替代文字、用户 ID/昵称、页面时间、显示点赞数和回复数来自 DOM。页面不提供真实评论 ID 时，`comment_id` 保持 null；`record_key` 是由微博、层级、用户、时间、正文和回复线程组成的本地指纹。`root_record_key` 只表示本地线程关联，不冒充平台父评论 ID。同一用户、同一显示时间、相同正文的评论可能合并；正文变化也可能产生新记录。不能将这种去重视为真实 ID 的精确去重。

外部“查看全部”链接不会自动打开，截断正文标为 `content_truncated`。浏览器评论数据独立保存，不修改既有 MySQL 记录。选择器基于目前观察到的页面结构，微博改版后可能需要调整；加载停滞不会被包装成“全部采集成功”。

## 验证

```powershell
.\.browser-venv\Scripts\python.exe -m unittest discover -s weibospider -p test_browser_comments_offline.py -v
```

合成场景覆盖虚拟列表、回复分页、断点恢复、重复采集、预算、验证停止、异常保存和 JSON 导出。

2026-10-12 已通过终端启动 Playwright 和本机 Chrome，在用户手动登录的专用配置中完成真实验证：一条微博保存 1 条一级评论及 3 条回复，另两条分别保存 16 条、14 条一级评论。一级评论实际经过 3 轮和 2 轮滚动后识别结束；回复样本只有 3 条，未验证多页回复。一次限 1 轮的运行保存 6 条并标记预算不足，续跑补齐到 16 条；重复运行未产生重复记录，已完成目标会跳过。SQLite 与 JSON 回读一致，原 MySQL 262 条记录逐内容摘要保持不变。证据位于 `output/browser-validation-20261012/verification.json`。

一条机器人回复在网页中被截断，程序保持 `incomplete/truncated_content`，没有把它当成完整正文。真实运行出现过间歇性的 `ERR_CONNECTION_CLOSED`；程序保留网络错误代码并停止，而不是误报登录跳转。已修复该错误分类；`--login` 导航失败时也会保留 Chrome 窗口供人工检查。上述测试证明小样本链路可工作，不保证历史评论全量或持续稳定访问。

API 依据：[Playwright 持久浏览器上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Locator API](https://playwright.dev/python/docs/api/class-locator)。
