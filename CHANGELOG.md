# 更新日志

网页控制面板会定期读取这个文件（只读取，不上传任何数据）：这里出现比你正在运行的版本更新的版本号时，面板顶部会提示，
用 `git clone` + `./install.sh` 装的可以在面板里一键更新。格式：`## 版本号 — 日期`，下面每行一条 `- 改动`。

## 1.5.8 — 2026-11-26
- 修：在面板删除账号后，用同一个名字再添加时提示「账号已经存在」，但列表里看不到这个账号。原因是视频模式在每个账号目录里放了一个只能写不能读的文件夹（media/w），删除时删不掉，留下一个空目录。现在删除前先解锁再删，删不干净会直接报错，不再显示「已删除」；以前删除留下的空目录，添加同名账号时自动清掉（目录里还有文件的不会动）。
- 新增离线测试项（panel_controls_test 23 项）。

## 1.5.7 — 2026-11-26
- 新：可以删除 AI Studio 会话。每个 AI Studio 一栏（包括「仅 AI Studio」、没有 Antigravity 登录的卡片）都有「删除」按钮，输入邮箱确认后删除服务器上保存的这个账号的 AI Studio 登录状态；同一邮箱的 Antigravity 登录不受影响。
- 改：「添加新账号」和「重新登录」默认打开 Refresh Token 页（不用打开 Google 登录页、不用输账号密码，粘贴 refresh_token 即可）；要用浏览器登录时点「OAuth 授权」。
- 新增离线测试项（panel_controls_test 19 项）。

## 1.5.6 — 2026-11-21
- 修：Antigravity 通道在长上下文多轮工具调用（如 `gemini-4-neon` 搭配 Agent 客户端）下响应极慢甚至超时（动辄 85–210 秒）。原因是 ACP 协议没有 `stop_sequences`，模型在写出完整的 `</tool_calls>` 后仍继续凭空续写多轮假 `<tool_result>` 与后续对话，而网关此前一直等到整段无效续写自然结束才返回。现在启用工具调用的请求一旦收到完整且合法的 `</tool_calls>`（或 `</function_calls>`），立即截断其后内容并向 `agy` 发送 `session/cancel` 结束本轮（支持 Chat Completions、Messages、Responses 三种接口的流式与非流式）；同时逐个校验闭合标签，避免参数字符串内恰好包含 `</tool_calls>` 时误截断，既将工具调用耗时从分钟级压回秒级，也避免浪费 Antigravity 额度。
- 修：上游返回临时容量不足（`MODEL_CAPACITY_EXHAUSTED`、`selected model is at capacity`、`no available model instances`）时优先判为 `RATE_LIMIT`（90 秒短冷却），不再被误判为个人额度耗尽（`QUOTA`，30 分钟冷却）；真正的个人额度耗尽仍按 `QUOTA` 处理。
- 修：`AcpProcess.request()` 取消时始终清理 `pending` 表，`_run_session()` 在流提前终止时清理未完成的 `Queue.get()` / `session/prompt` 任务，消除 `Task was destroyed but it is pending` 泄漏。
- 新增离线测试 `scripts/tool_stop_test.py`。

## 1.5.5 — 2026-11-21
- 新：两路额度通用。`gemini-3.8-flash`、`gemini-4-hydrogen`、`gemini-4-neon` 在 Antigravity 和 AI Studio 里是同一个模型，现在两边的额度可以互相顶上：用 `aistudio/gemini-4-neon` 时 AI Studio 到了限额、冷却中、没登录或连不上，同一个请求自动改用 Antigravity 的 `gemini-4-neon`；用 `gemini-4-neon` 时 Antigravity 上有这个模型的账号都在限额 / 冷却，或者最后一个账号在这次请求中用完了额度，就改用 AI Studio 的同一模型（思考档位保留）。三种接口、流式和非流式都支持；流式在开始输出答案之前才会切换，不会把两边的回答拼在一起。用 `X-ACP-Account` 指定账号时，只在这个邮箱自己的两路之间切换。每次切换都记在面板「最近事件」里。用 `AGY_SHARED_MODELS` 可以改这份模型名单（逗号分隔，留空 = 关闭）。
- 新：默认思考档位。请求没有指定档位时，`gemini-4-neon` 和 `gemini-4-hydrogen` 用 `xhigh`，`gemini-3.8-flash` 用 `high`（Antigravity 和 AI Studio 两边都一样）；模型名带档位（如 `gemini-3.8-flash-medium`）或客户端传了 `reasoning_effort` 时照客户端的。账号的模型没有这一档时用它有的最接近的低一档（xhigh → high → medium）。用 `AGY_DEFAULT_LEVELS` 改（如 `gemini-4-neon=xhigh,gemini-3.8-flash=high`，留空 = 用模型自己的默认）。
- 新增离线测试 `scripts/shared_quota_test.py`。

## 1.5.4 — 2026-11-20
- 修：没有启用 AI Studio 第二路的网关，点「＋ 接入 AI Studio」会打开登录窗口并一直显示「正在启动服务器浏览器」，原因只在底部一行小字里。现在直接说明需要先在服务器上运行 `aistudio.sh setup`（按这台网关的实际安装目录给出命令）；登录窗口启动失败时也会把原因显示在窗口中间。
- 修：`aistudio.sh setup` / `remove` 最后重启网关时写死了服务名 `acp-ant`，服务名不同的安装（手动部署时起了别的名字）会在这一步报错。现在依次用 `ACPANT_SERVICE`、`acp-ant`、或启动命令在安装目录里的那个服务。

## 1.5.3 — 2026-11-20
- 新：面板账号搜索。账号列表上方的搜索框可按名称、邮箱、代理、出口 IP 或状态（如「已关闭」）筛选，多个词同时满足；按 `/` 快速搜索，`Esc` 清空。每 10 秒的自动刷新不会清掉搜索。
- 新：每个账号显示它支持的 Antigravity 模型和思考档位（点一下复制模型名）。还没读到的账号点「读取模型列表」即可：开一个空会话读取，不发消息、不消耗额度，关闭的账号也能读。
- 改：每个账号的 Antigravity 和 AI Studio 各有一个开关，分开控制。原来卡片右上角那个开关只管 Antigravity，现在移到 Antigravity 一栏里；AI Studio 一栏新增自己的开关（关掉后 `aistudio/` 模型不再用这个账号，代理和登录保持不变）。用 `X-ACP-Account` 指定到 AI Studio 已关闭的账号时直接报错，不会换成别的账号。
- 改：展开的模型列表在自动刷新后保持展开。
- 新增离线测试 `scripts/panel_controls_test.py`。

## 1.5.2 — 2026-11-17
- 修：AI Studio 账号登录后，所有模型一直报「HTTP 403、协议错误码 7：The caller does not have permission」，同一账号的 Antigravity 却正常。实测原因是登录时启动的那个 AI Studio 页面被 Google 拒绝，换一个新页面立刻恢复。现在网关每 60 秒检查一次，请求失败时也立即检查：遇到被这样拒绝、当前空闲的账号，自动重启它的 AI Studio 页面（代理、语言、时区不变）并清掉它的冷却，失败的那次请求再试一次。同一账号 10 分钟内最多重启一次；连续 3 次没好就暂停 1 小时，面板账号行提示去 aistudio.google.com 用这个账号核对。每次重启都记在面板「最近事件」里。
- 新增离线测试 `scripts/studio_heal_test.py`。

## 1.5.1 — 2026-11-16
- 修（#3）：模型在工具参数里直接写了换行 / 制表符（多行命令、代码）时，工具调用解析失败，整段 `<tool_calls>` 被当成文字发给客户端。现在能正常解析。
- 修（#4）：对话历史很长时，官方 CLI 会把超长的输入从末尾截掉（约 18.8 万字符），最新那句提问因此丢失、模型答非所问。现在整段输入超过 `AGY_MAX_PROMPT_CHARS`（默认 150000 字符，0 = 不限制）时，先缩短很长的旧工具输出，再从最早的历史开始省略，并告诉模型省略了几条；最新输入永远完整保留。
- 新（PR #8，感谢 @wangbingnan136）：按账号路由模型。每个账号记录自己能用的模型，Gemini 4 Hydrogen、Claude Opus 这类只有付费账号才有的模型只派给有它的账号，不再被悄悄换成默认模型回答；`/v1/models` 列出所有开启账号的模型合集，思考档位按模型实际支持的列出；支持 Hydrogen 的 `xhigh` 档位。
- 修（#7）：面板「最近事件」里长邮箱和内容文字重叠；账号列过长时省略显示，悬停看全名。
- 新增离线测试 `scripts/prompt_fit_test.py`、`scripts/model_route_test.py`。

## 1.5.0 — 2026-11-16
- 修：所有账号突然都报「RESOURCE_EXHAUSTED (code 429)」，但额度其实没用完。原因是 Google 现在拒绝 agent 说明里的一句话（"You have no built-in tools: never try to read files, browse, or run commands yourself."），已改写为意思相同的另一句。升级后网关重启即恢复。
- 新增：AI Studio 第二路（可选）。运行 `aistudio.sh setup` 启动专用的 AIStudio2API，之后同一个地址和密钥下 `aistudio/<模型名>` 走 AI Studio，其余照旧走 Antigravity。
- 新增：面板「＋ 接入 AI Studio」与卡片上的「重新登录」：在面板里操作服务器上的浏览器登录 Google（全屏、可输入、可粘贴），登录的不是指定邮箱会被拒绝；有同邮箱 Antigravity 账号时自动用同一代理出口。也保留粘贴 Cookie 的方式。
- 新增：请求头 `X-ACP-Account: <账号名或邮箱>` 可指定这次用哪个 Google 账号（两路都认），指定的账号不可用就报错，不换别的邮箱。
- 面板：每个账号卡片分开显示 Antigravity 和 AI Studio 两路的状态、模型和额度；AI Studio 剩余额度显示「未知」，另显示今日调用和被限额的模型；只有 AI Studio 的账号单独成卡，可一键添加它的 Antigravity 登录。「接入信息」「在线测试」包含 AI Studio 模型。
- 依赖新增 `httpx`、`websockets`（只在启用 AI Studio 时使用）；新增离线测试 `scripts/studio_test.py`。

## 1.4.5 — 2026-11-12
- 修：每次请求最多 10 张图、多出的最早几张被悄悄丢掉（调用方不知道）。实测 Gemini 3.8 Flash 一次 200 张按顺序全看到、250 张上游报错，默认上限改为 200（`AGY_MAX_IMAGES`）。最新一条消息里的图从不丢弃，超过上限直接返回 400 并写明张数；只有多轮对话里更早轮次的图从最早的开始换成一句说明。
- 修：没有 Antigravity 使用许可的账号，上游把「⚠ You do not have a valid license of this product…(#3501)」当回答发回，网关以前原样交给调用方。现在认出后把这个号标成「无使用许可（#3501）」不再派活，请求自动换下一个号；面板「检测」也不会再把它显示成「正常」。
- 兜底：任何以「⚠」开头的回复都是 Antigravity 自己的状态信息、不是模型回答，一律当错误处理并换号，不再交给调用方。
- 新增离线测试 `scripts/image_limit_test.py`、`scripts/soft_error_test.py`。

## 1.4.4 — 2026-11-11
- 添加账号后不再自动读取额度：确认登录正常即可开启，额度通过账号卡片上的「刷新额度」手动读取；已打开的旧版面板页面也走相同逻辑，不发测试消息。
- 账号列表刷新不再为额度为空的账号后台发起查询，避免与添加向导重复并发读取；未读取时显示操作提示。
- 新账号登录确认后，定时巡检等待正常间隔再执行；手动刷新单个 / 全部额度和主动检测保留。
- 手动部署且没有源码目录的实例保留本地页面修复，不再被后台静默文件同步覆盖。
- 新增离线回归测试 `scripts/quota_flow_test.py`，覆盖添加流程不查额度、手动刷新、旧页面兼容、巡检间隔及未登录请求拒绝。

## 1.4.3 — 2026-11-12
- 加账号时只读额度改由服务器保证：更新前就打开着的面板页面（旧脚本）走到第 3 步也不会再真发测试消息。

## 1.4.2 — 2026-11-12
- 面板「添加账号」第 3 步不再真发一句话测试：改为读取一次额度（`agy -p /usage`，不发消息、不耗额度）确认登录正常，通过后自动开启。要真发一句话试，仍可在账号卡片上点「检测」。

## 1.4.1 — 2026-11-10
- 账号名字可以直接用 Google 邮箱：允许字母、数字和 `- _ . @ +`，最长 64 个字符（以前只能字母、数字、`-`、`_`，最长 32 个）。粘贴含 `email` 的 JSON 时，名字框会自动填成这个邮箱。面板和 `account.sh` 都一样；仍然拒绝 `..`、斜杠、空格等。
- 测试 `scripts/token_login_test.py` 增加邮箱名字和非法名字检查（51 项）。

## 1.4.0 — 2026-11-10
- 改版「添加账号」：点「添加账号」后是 OAuth 授权 / Refresh Token 两个标签页。Refresh Token 页直接粘贴（单个 `1//...`、含 `refresh_token` 的 JSON 如 `{email, refresh_token}`，或任意含 token 的文本），点「确认添加」一步完成建号和登录；token 不能用时自动删掉刚建的空账号。「重新登录」也是同样两个标签页。
- 代理改为可选：面板里留空、`account.sh` 里直接回车都行，那个账号用服务器本机网络。默认不再强制代理（`AGY_REQUIRE_PROXY` 默认值由 1 改为 0）；想强制每个账号都有代理，设 `AGY_REQUIRE_PROXY=1`。
- 测试 `scripts/token_login_test.py` 增加无代理建号等检查（39 项）。

## 1.3.0 — 2026-11-10
- 新增「用 refresh_token 登录」：已经有某个 Google 账号的 refresh_token 时，不用再开浏览器登录。面板里在登录这一步展开「已经有 refresh_token？」粘贴即可（单独的 token 或含 `refresh_token` 的整段 JSON 都行）；命令行是 `account.sh token <名字>`（不回显，账号没有就先建、先设代理）。
- 只能用 Antigravity 登录得到的 refresh_token（Antigravity 编辑器、Antigravity 账号管理工具导出的），Gemini CLI 等其它程序的不能用。网关把它写进官方 `agy` 自己的登录文件（权限 600，和浏览器登录后同一格式），由 `agy` 通过这个账号的代理去换令牌；网关里没有 Google 客户端密钥。
- 写完立即检测，用不了就恢复原来的登录；refresh_token 不显示、不记日志、不返回，成功后只显示账号邮箱，同一个 Google 账号被别的账号用过会提醒。
- 新增离线测试 `scripts/token_login_test.py`。

## 1.2.0 — 2026-11-10
- 新增「视频模式」（试验功能，默认关闭，`AGY_VIDEO_MODE=1` 打开）：打开后，Gemini 模型可以看视频、听音频、读 PDF。只收请求里直接带的文件（base64），不下载链接；单个文件默认最大 40 MB，一次最多 5 个。非 Gemini 模型照旧返回 400。
- 视频模式的做法：Antigravity 的 ACP 接口不收视频，只能让模型用 `read_file` 工具去读服务器上的文件。所以网关把附件写到一个本次请求专用、名字随机的目录里，请求结束立刻删除（连同 Antigravity 自己留的副本），并且只在带视频 / 音频 / PDF 的请求里使用一个单独的 `agy` 进程，这个进程只有 `read_file` 一个工具。
- 视频模式的三层防护：① 这个进程的 HOME、临时目录、工作目录都是专用的空目录，里面没有代理配置和日志，其它路径 Antigravity 会要求授权，网关一律拒绝；② 网关盯着模型的每一次读文件，只要路径不是本次请求的附件，立刻取消并返回 400，后面的内容不会发给调用方；③ agent 说明里写明只能打开附件。
- 不打开视频模式时，行为和 1.1.1 完全一样。
- 新增离线测试 `scripts/video_mode_test.py`。

## 1.1.1 — 2026-11-09
- 修复：请求里带视频、音频、PDF 等文件时（OpenAI `file` / `input_audio` / `video_url`、Responses `input_file`、Anthropic PDF `document` 等），以前网关会悄悄丢掉，模型只看到文字，回答里的内容是编的。现在直接返回 400，写明是第几条消息的第几项，调用方可以换通道。实测 Antigravity 的 ACP 接口本身就不把视频、音频、PDF 交给模型（Gemini 4 Argon、Gemini 3.8 Flash 都一样），所以没法转发过去。
- 修复：图片链接下载失败、链接其实不是图片、关了图片下载（`AGY_FETCH_IMAGES=0`）时，以前只在提示词里写一句「图片下载不了」照样回答，现在返回 400。
- 修复：系统提示词里放图片时以前会被丢掉，现在返回 400（OpenAI 本身也不允许）。
- 新增：纯文本文件（txt、md、json、csv 等）会转成文字发给模型；OpenAI `file` 里装的图片按图片处理；Anthropic 的 `content` 类文档和 `search_result` 也能用了。
- 新增离线测试 `scripts/content_test.py`（不需要账号和网络）。

## 1.1.0 — 2026-11-08
- 新增 Docker 版：`docker compose up -d --build` 一条命令跑起来，账号、密钥、面板密码都存在数据卷里。
- 新增不用 sudo 的安装方式：`./install.sh --user`，装到 `~/acp-ant`，作为当前用户的服务运行（systemctl --user）。
- 修复：服务器缺少 python3-venv 时，安装脚本会留下一个没有 pip 的虚拟环境，之后每次安装都失败。现在会检查 pip 是否可用，坏了就重建；有 apt 和 sudo 时自动补装，否则提示要装的包名。
- 新增版本更新提醒：面板定期检查 GitHub 上的新版本，在顶部提示，并显示更新内容；git 安装的可以在面板里一键更新，更新完自动重启。
- 所有账号都满时请求排队，哪个账号先空出来就交给哪个；超过请求时限才返回「忙」（503 / Anthropic 格式 529）。
- 流式回复静默 15 秒发一次心跳，防止 Cloudflare 等反向代理在 100 秒没有数据时断开连接（长时间思考、很长的工具调用不再断线）。
- 面板「设置」里可以直接修改每个账号同时处理几个请求（1–10），首页显示正在处理 / 同时上限和排队数。

## 1.0.0 — 2026-11-07
- 首个公开版本：通过 ACP 协议驱动官方 Antigravity CLI，提供 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 三种接口，支持工具调用。
- 多账号、每个账号独立代理；按最闲的账号分配；额度用完、限流、需要验证时自动换号。
- 网页控制面板：添加账号、网页登录 Google、换代理、看额度、开关账号、自动巡检、代理记录、在线测试。
- 每个账号一个常驻进程并预热，短回复首字 1–2 秒。
