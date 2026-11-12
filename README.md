# ACP-Ant

把 Google 官方 **Antigravity CLI（`agy`）** 通过 **ACP 协议（Agent Client Protocol）** 变成标准的大模型接口：
**OpenAI Chat Completions、OpenAI Responses、Anthropic Messages** 三种格式都支持，带**工具调用（function calling）**，Agent 和应用可以直接接入。
支持**多个 Google 账号轮流使用，每个账号走自己的代理节点**。

- 用的是 Google 官方命令行程序和官方协议，不抓包、不反代网页登录凭据。
- Codex CLI、OpenAI SDK、LangChain、Dify、Cline、OpenCode……只要支持 OpenAI 或 Anthropic 接口，填上地址、密钥和模型名就能用。
- 账号多了自动分流；某个账号额度用完、被限速、要求验证时自动换下一个账号。
- **网页控制面板**：添加账号、登录 Google、换代理、看每个账号的额度、开关账号、自动巡检，全部在网页里点，不用敲命令。

[English](#english)

## 🎬 推荐：COOL AI — 电商 AI 视频与图片一站式创作

上传一张商品图，就能做出口播带货视频、剧情带货短剧和整套电商详情页。

- **口播带货视频**：自动写脚本、分镜和台词，一键生成真人口播带货视频
- **剧情带货视频**：反转短剧、对话短剧、剧情植入，自动编剧、分镜、出片
- **电商详情图**：一张白底图，一键生成首图、场景图、卖点图、细节图、对比图整套
- **AI 图片与工具**：商品精修、GIF 动图、数字人、字幕擦除、画质增强

👉 立即体验：[www.coolai.mov](https://www.coolai.mov/?utm_source=github&utm_medium=readme&utm_campaign=acp-ant)


---

## 它做了什么

```
Agent / 应用 ──OpenAI 或 Anthropic 接口──▶ ACP-Ant（127.0.0.1:8095）
  （工具由 Agent 自己执行）                       │  挑一个空闲账号
                                                 ├─▶ agy acp（账号 g1，HOME=accounts/g1，走代理 A）──▶ Google Antigravity
                                                 ├─▶ agy acp（账号 g2，HOME=accounts/g2，走代理 B）──▶ Google Antigravity
                                                 └─▶ agy acp（账号 g3，HOME=accounts/g3，走代理 C）──▶ Google Antigravity
```

- **三种接口**：`/v1/chat/completions`（OpenAI）、`/v1/responses`（OpenAI 新接口，Codex 用这个）、`/v1/messages`（Anthropic），都支持流式输出。
- **工具调用**：Agent 声明的工具（OpenAI `tools`、Anthropic `tools`、Responses 的 function / custom 工具）会写进提示词交给模型。模型要用工具时，网关把它的回复转成标准的 `tool_calls` / `tool_use` / `function_call` 返回给 Agent。Agent 自己执行工具，再把结果发回来。支持一次调用多个工具，也支持 `tool_choice`。
- **图片**：data URL、http(s) 图片链接（网关代为下载）、工具结果里的图片都可以。纯文本文件（txt / md / json / csv 等）会转成文字。
- **视频、音频、PDF 等文件默认不支持，直接返回 400**（想让 Gemini 看视频，见下面的[视频模式](#视频模式试验功能默认关闭)）：Antigravity 的 ACP 接口只把文字和图片交给模型，视频、音频、PDF 发过去会被它悄悄丢掉，模型照样回答，内容是编的（实测 agy 1.5.3，Gemini 4 Argon / Gemini 3.8 Flash 都一样）。所以网关遇到这类内容（OpenAI `file` / `input_audio` / `video_url`、Responses `input_file`、Anthropic PDF `document` 等）直接返回 400 并写明是哪一项，调用方可以换通道，或者把视频抽成图片帧再发。
- **每个账号一个独立目录**：`agy` 的 HOME 指向这个目录，登录凭据只存在这里，账号之间互不串。
- **每个账号一个独立代理**（可选）：`http://`、`https://`、`socks5://`，可带用户名密码。添加时会测出口 IP 和国家，两个账号用了同一个 IP 会提醒。
- **Antigravity 自己不碰你的机器**：网关给 `agy` 注册的 agent 没有任何内置工具，权限请求一律拒绝。工具只在 Agent 那边执行。
- **自动换号**：还没输出正文前出错就换下一个账号重试；额度用完歇 30 分钟、限速歇 90 秒、要求验证歇 10 分钟，登录失效的账号停用。
- **认得 Antigravity 的「软错误」**：审核拦截、账号需要验证、上游报错（如 `RESOURCE_EXHAUSTED`）时，Antigravity 不会报错，而是把一句固定的话当成回答发回来，有时还会在后台重试好几分钟。网关几秒内就能认出这些情况，返回标准错误码，Agent 能正常重试或换通道。
- **模型名兜底**：Agent 请求了 Antigravity 没有的模型名（例如写死的 `gpt-4o`），自动改用默认模型（可关闭）。
- **隐私**：`agy` 每次对话都会在本地存一份记录，网关每小时删掉超过 6 小时的（可配置）。
- **新加账号不用重启**：网关会自动发现新账号。

## 要求

- Linux（x86_64 或 arm64），Python 3.10+，`curl`、`git`；或者只要 Docker。
- 一个或多个能用 Antigravity 的 Google 账号（例如 Google AI Pro）。
- 可选：每个账号一个代理节点。

## 安装

三种方式任选一种，装好以后都在网页控制面板里操作。

### 方式一：系统服务（推荐，需要 sudo）

```bash
git clone https://github.com/czg86389-hub/ACP-Ant.git
cd ACP-Ant
./install.sh
```

安装脚本会：
1. 从官方地址（`https://antigravity.google/cli/install.sh`）安装 `agy` 到 `/opt/acp-ant/bin`；
2. 建 Python 虚拟环境并装依赖（FastAPI、Uvicorn）。缺 `python3-venv` 时，有 apt 就自动补装，否则提示该装哪个包；
3. 生成随机网关密钥，写进 `/opt/acp-ant/gateway.env`（权限 600）；
4. 装成 systemd 服务 `acp-ant`，只监听 `127.0.0.1:8095`。

可以用环境变量改安装位置和端口：`AGY_ROOT=/srv/acp-ant AGY_PORT=9000 ./install.sh`；不想装服务就加 `--no-systemd`。

### 方式二：用户服务（不用 sudo）

```bash
git clone https://github.com/czg86389-hub/ACP-Ant.git
cd ACP-Ant
./install.sh --user
```

装到 `~/acp-ant`，作为当前用户的服务运行（`systemctl --user status acp-ant`，日志 `journalctl --user -u acp-ant -f`）。
脚本会顺便开启「退出 SSH 后继续运行」（`loginctl enable-linger`）；有的服务器不允许普通用户开，脚本会提示请管理员运行一次 `sudo loginctl enable-linger 你的用户名`。
请用 SSH 直接登录这个用户再安装（不要 `su` 过去），否则连不上用户的 systemd。

### 方式三：Docker

```bash
git clone https://github.com/czg86389-hub/ACP-Ant.git
cd ACP-Ant
docker compose up -d --build
cat data/panel-initial-password.txt     # 面板初始密码（可能要 sudo）
```

- 第一次启动会生成密钥、把官方 `agy` 装进数据目录；账号、密钥、面板密码、统计都在 `./data` 里，删掉重建容器也不会丢。
- 默认只监听本机 `127.0.0.1:8095`，要绑定域名请在前面加 Caddy / Nginx（见下文）；端口和其它设置在 `docker-compose.yml` 里改。
- 命令行管账号：`docker compose exec acp-ant account.sh list`（一般用面板就够了）。

### 更新

- 网页控制面板每 6 小时读一次 GitHub 上的 [CHANGELOG.md](CHANGELOG.md)（只读取这一个公开文件，不上传任何数据），有新版本时在顶部提示并列出更新内容。可以在「设置」里关闭，或在 `gateway.env` 里写 `ACPANT_UPDATE_CHECK_HOURS=0` 彻底关掉。
- 方式一、方式二（`git clone` 安装）：在面板里点 **「立即更新」** 就行。面板会拉取最新代码、装好依赖，等正在处理的请求做完后自动重启，一般 1 分钟内完成；账号、密钥、面板密码和统计都保留。
- 手动更新：在源码目录运行 `git pull && ./install.sh`（用户服务加 `--user`）。Docker：`git pull && docker compose up -d --build`。
- 用自己的 fork：`gateway.env` 里写 `ACPANT_UPDATE_REPO=你的用户名/ACP-Ant`。

## 网页控制面板

网关的首页就是控制面板：本机打开 `http://127.0.0.1:8095/`，绑定域名后打开 `https://你的域名/`。

- **登录**：管理员密码。第一次的初始密码在服务器的 `/opt/acp-ant/panel-initial-password.txt`（`cat` 一下就能看到），
  在面板「设置」里改成自己的密码后，这个文件自动删除。也可以在 `gateway.env` 里写 `AGY_PANEL_PASSWORD=` 指定。
  面板密码和客户端用的 API 密钥是两回事。
- **账号卡片**：状态（正常 / 已关闭 / 需要 Google 验证 / 冷却中 / 需要重新登录）、代理和出口 IP、
  **额度**（Gemini 模型、Claude / GPT 模型、Gemini 4 Argon 三组，每组的每周和 5 小时剩余百分比和重置时间）、今天和近 7 天的成功 / 失败次数和 Token 用量、最近错误。
- **开关**：每个账号右上角一个开关，关掉后这个账号不会再被调用（文件 `accounts/<名字>/disabled`），打开立即恢复。
- **按钮**：检测（真发一句话；Google 要求验证时直接给出验证链接）、刷新额度、重新登录、换代理、清除冷却、删除。
- **添加账号向导**：填名字和代理 → 自动测出口 IP（同一 IP 被别的账号用过会提醒）→ 点按钮打开 Google 登录页，
  把授权码或落地页网址粘贴回面板 → 自动读取一次额度确认登录正常（不发测试消息、不耗额度），通过后自动开启；要真发一句话试，在账号卡片上点「检测」。登录全程在你自己的浏览器里完成，面板只转交授权码，不保存。
- **用 refresh_token 登录**：已经有这个 Google 账号的 refresh_token（例如从 Antigravity 账号管理工具里导出的），
  在「添加账号」里切到 Refresh Token 标签页直接粘贴（单独的 token，或者含 `refresh_token` 的整段 JSON 都行），不用再开浏览器。
  见下文「用 refresh_token 添加账号」。
- **接入信息**：接口地址、一键复制密钥、全部模型名、Codex / PI-Desktop / curl 配置示例。**在线测试**：选模型发一句话。**最近事件**：换号、出错、开关等记录。
- **版本更新**：顶部提示新版本和更新内容；`git clone` 安装的点「立即更新」自动拉取、安装、重启（见上文「更新」）。
- **同时请求数**：首页显示「正在处理 / 同时上限」和排队数；在「设置」里改每个账号同时处理几个（1–10，默认 3），保存后立即生效。
- **自动巡检（保活）**：默认每 6 小时给每个开启的账号读一次额度——**不消耗额度**，顺便刷新 Google 登录令牌、确认代理和登录都正常；
  发现登录失效会标成「需要重新登录」。可选每天再真发一句话，提前发现「需要 Google 验证」（每天消耗一次极小的额度）。

> 关于保活：Google 的登录令牌由官方 `agy` 在每次使用时自动续期，长期不用一般也不会失效，所以严格意义的保活不是必须的。
> 巡检的意义在于**提前发现**代理挂了、登录失效这类问题，不用等到真有请求时才知道。

面板接口都在 `/panel/api/` 下：会话 Cookie（HttpOnly、SameSite=Strict），修改类请求要带 `X-ACP-Panel: 1` 请求头，登录失败按 IP 限次。

## 添加账号（命令行方式）

面板里就能添加账号；喜欢命令行的话也可以用脚本。每个账号起一个名字（字母、数字和 `- _ . @ +`，可以直接用 Google 邮箱），例如 `g1`：

```bash
/opt/acp-ant/account.sh add g1
```

1. **输入代理地址**（输入时屏幕上不显示，粘贴后直接回车），例如 `socks5://用户名:密码@1.2.3.4:1080`。脚本会测这个代理的出口 IP 和国家。
2. **登录 Google**：屏幕上会出现一个授权网址。复制到**你自己电脑**的浏览器里打开，选这个账号并同意。浏览器最后会跳到一个打不开的 `localhost` 网址，或者显示一串授权码。把整串网址或授权码复制回终端，粘贴后回车。
3. 显示「已登录，可用模型：…」就好了。

中途关掉也没关系，再运行一次 `add g1` 会接着做。其它命令：

| 命令 | 作用 |
|---|---|
| `account.sh list` | 列出所有账号、代理（只显示主机和端口）、出口 IP、登录状态和可用模型 |
| `account.sh check g1` | 真发一句话测试；如果 Google 要求验证这个账号，会打印验证网址 |
| `account.sh login g1` | 重新登录（代理不变） |
| `account.sh token g1` | 用 refresh_token 登录（输入时不显示；账号还没有就先建、先设代理） |
| `account.sh proxy g1` | 换代理节点 |
| `account.sh remove g1` | 删除账号（含登录凭据，会先确认） |

代理是可选的（1.4.0 起）：面板里留空、`account.sh add` 时直接回车都行，那个账号就用服务器本机网络。想强制每个账号都有代理，在 `gateway.env` 里写 `AGY_REQUIRE_PROXY=1`（`account.sh` 的环境变量也要设成一样）。

### 用 refresh_token 添加账号

已经有某个 Google 账号的 refresh_token 时，可以不走浏览器登录：

- 面板：添加账号 → 切到「Refresh Token」标签页 → 填名字（代理可选）→ 粘贴 → 「确认添加」；已有账号点「重新登录」也是同样两个标签页。
- 命令行：`/opt/acp-ant/account.sh token g1`，粘贴后回车（屏幕上不显示）。

要求和做法：

- **只能用 Antigravity 登录得到的 refresh_token**（Antigravity 编辑器、各种 Antigravity 账号管理工具导出的都是），一般以 `1//` 开头。
  Google 的 refresh_token 和签发它的程序绑定，Gemini CLI 等其它程序的 refresh_token 在这里用不了。
- 网关把它写进这个账号目录里官方 `agy` 自己的登录文件（`accounts/<名字>/.gemini/antigravity-cli/antigravity-oauth-token`，权限 600），
  格式和浏览器登录后的一样，然后让 `agy` 自己去换新的访问令牌、读出账号邮箱。网关里没有 Google 的客户端密钥，也不直接请求 Google。
- 写完马上检测：用不了（失效、被撤销、不是 Antigravity 的、代理连不上）就恢复原来的登录文件，账号不受影响。
- refresh_token 不显示、不记日志、不返回给面板；登录成功只显示账号邮箱。同一个 Google 账号已经被别的账号用了会提醒。
- 用 refresh_token 登录时一样走这个账号自己的代理。一个 refresh_token 等于这个 Google 账号的长期登录，请当密码保管。

## 接入 Agent 和应用

不管用哪个 Agent，都填这三样：

- **地址**：`http://127.0.0.1:8095/v1`（Anthropic 格式的客户端填 `http://127.0.0.1:8095`）
- **密钥**：`grep AGY_GATEWAY_KEY /opt/acp-ant/gateway.env` 看到的那串
- **模型名**：推荐 `gemini-3.8-flash-medium`（其它见下面的「模型名」）

**Agent 在你自己电脑上、网关在服务器上**时，最简单安全的办法是 SSH 隧道。在你电脑上运行下面这条命令并保持窗口打开，然后照样填 `http://127.0.0.1:8095/v1`：

```bash
ssh -N -L 8095:127.0.0.1:8095 用户名@服务器
```

不要把 8095 端口直接暴露到公网；确实要公网访问，请用 Nginx / Caddy 加 HTTPS 反向代理。

### 测试过的

| Agent | 接口 | 结果 |
|---|---|---|
| **Codex CLI** 0.134 | OpenAI Responses | ✅ 真跑通：用 shell 工具建文件、再读回来，15 秒 |
| OpenAI 格式的工具调用 | Chat Completions | ✅ `scripts/agent_test.py`：工具调用、带工具结果的第二轮、流式工具调用、JSON 模式 |
| Anthropic 格式的工具调用 | Messages | ✅ 同上：`tool_use`、`tool_result` 第二轮、流式、`count_tokens` |
| Responses 格式的工具调用 | Responses | ✅ 同上：`function_call`、`function_call_output` 第二轮、流式自定义工具（custom tool） |
| **Claude Code** | Anthropic Messages | ❌ **不能用**，见下面说明 |

**Codex CLI**（`~/.codex/config.toml`）：

```toml
model_provider = "acpant"
model = "gemini-3.8-flash-medium"

[model_providers.acpant]
name = "ACP-Ant"
base_url = "http://127.0.0.1:8095/v1"
env_key = "ACPANT_KEY"
wire_api = "responses"
```

然后 `export ACPANT_KEY=你的网关密钥`，再运行 `codex`。新版 Codex 已经不支持 `wire_api = "chat"`，必须用 `responses`。

**关于 Claude Code**：Google 的 Antigravity 服务端会拒绝带有 Claude Code 特征内容的请求。Claude Code 每次请求都会带一行 `x-anthropic-billing-header` 和一句固定的身份介绍，服务端遇到这两样就返回 `RESOURCE_EXHAUSTED (429)`，和账号额度无关。这是 Google 那边有意的限制，ACP-Ant 不做绕过（绕过有封号风险）。网关会在 2–3 秒内把它当成 429 返回，不会卡住。

### 按 OpenAI 兼容接口配置即可（接口已测，软件本身未逐一实测）

| 软件 | 怎么配 |
|---|---|
| OpenAI SDK、LangChain（`ChatOpenAI`）、LlamaIndex、AutoGen、CrewAI 等 | `base_url` 填地址，`api_key` 填密钥，`model` 填模型名 |
| Cline / Roo Code / Kilo Code（VS Code 插件） | API Provider 选「OpenAI Compatible」，填 Base URL、API Key、Model ID |
| Continue、Cherry Studio、Open WebUI、LobeChat、Dify、n8n | 添加「OpenAI 兼容」模型供应商，填地址、密钥、模型名 |
| Aider | `aider --openai-api-base http://127.0.0.1:8095/v1 --openai-api-key 你的密钥 --model openai/gemini-3.8-flash-medium` |
| OpenCode | 在 `opencode.json` 里加一个 `@ai-sdk/openai-compatible` 供应商，`baseURL` 填地址 |
| Cursor | 需要一个公网 HTTPS 地址（Cursor 是从它自己的服务器发请求的），本机地址和 SSH 隧道都不行 |

Python 例子（带工具）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8095/v1", api_key="你的网关密钥")
r = client.chat.completions.create(
    model="gemini-3.8-flash-medium",
    messages=[{"role": "user", "content": "巴黎现在天气怎么样？"}],
    tools=[{"type": "function", "function": {"name": "get_weather", "description": "查询城市天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
)
print(r.choices[0].message.tool_calls)   # [ChatCompletionMessageToolCall(function=Function(name='get_weather', arguments='{"city": "Paris"}'), ...)]
```

自己验证：`python3 /opt/acp-ant/agent_test.py`（三种接口的工具调用全测一遍，约 1–2 分钟）。

### 工具调用是怎么实现的

ACP 协议本身只能让 Antigravity 调用它自己那边的工具，不能把工具调用交还给你的 Agent。所以网关把 Agent 的工具清单（名字、说明、参数格式）写进系统提示词，并规定模型用 `<tool_calls>[...]</tool_calls>` 这种格式提出调用。网关把这段内容解析成标准的工具调用返回给 Agent，Agent 发回的工具结果再写回对话里。

- Gemini 3.8 Flash 用这种方式的成功率很高（上面的测试全部通过）。
- 模型偶尔会不按格式写；这时网关会把原文当作普通回答返回，Agent 一般会重试或继续。
- 流式输出时，普通文字照常实时输出，工具调用那一段等写完后一次性发出。

## 模型名

| 模型名 | 实际用的模型 | 思考档位 |
|---|---|---|
| `gemini-3.8-flash-high` / `-medium` / `-low` | gemini-3.8-flash | 高 / 中 / 低 |
| `gemini-3.8-flash` | gemini-3.8-flash | 账号默认 |
| `gemini-4-argon-high` / `gemini-4-argon` | gemini-4-argon | 高 / 账号默认 |

- 不在表里的名字原样传给 Antigravity，结尾的 `-high` / `-medium` / `-low` 会自动当成思考档位。Antigravity 还提供 `claude-sonnet-5-5`、`claude-opus-5-5`、`gpt-oss-120b-medium` 等模型（以 `account.sh list` 显示的为准），也可以直接用。
- `GET /v1/models` 会列出别名和账号实际提供的模型（网关处理过一次请求后才知道账号有哪些模型）。
- Agent 请求了不存在的模型名，默认改用 `AGY_DEFAULT_MODEL`（`gemini-3.8-flash-medium`）；`AGY_MODEL_FALLBACK=0` 改为报错。
- 自定义别名：`AGY_MODEL_ALIASES={"fast": ["gemini-3.8-flash", "low"]}`。
- OpenAI 的 `reasoning_effort` / Responses 的 `reasoning.effort`（low / medium / high）会当作思考档位（模型名里已经写了档位的以模型名为准）。

**速度**（服务器本机实测，gemini-3.8-flash-medium）：短回复首字约 1.3–2 秒；约 300 字的回答首字 6–9 秒；长文首字 8–10 秒，之后边生成边输出（每秒约 100 字）。
首字主要花在模型先思考再输出上。网关做了两件事来压低延迟：
- 每个账号一个常驻 `agy` 进程，所有会话共用，新建会话约 0.02 秒（每个会话单独起进程要多约 2 秒），服务启动时还会先预热好；
- 回答开头一确定不是 Antigravity 的「假回答」（审核拦截、要求验证、上游报错），就立刻往外输出，不再攒字。

高思考档非常慢，复杂长文一次可能要几分钟，一般用 `medium` 就够了。

**同时请求数和排队**：每个账号同时处理 `AGY_PER_ACCOUNT` 个请求（默认 3，面板「设置」里可以直接改），N 个账号就是同时 N×3 个。
超出的请求排队，**不报错**：哪个账号先空出来就交给哪个，先来先得；排队时间算在单次请求的 290 秒里，超过才返回「忙」（OpenAI 格式 503、Anthropic 格式 529）。
Codex 这类写代码的 Agent 是「发请求 → 本机执行工具 → 再发请求」一步一步来的，一个任务同时只占 1 个位子。
Google 没有公布同一账号同时几个请求的上限（官方 IDE 本身就支持一个账号同时跑多个 Agent），真正的限制是每个账号的 5 小时 / 每周额度；想同时服务更多人，最稳的办法是加账号。

**流式心跳**：模型思考、写很长的工具调用、排队等位子时，流式回复可能很久没有数据。Cloudflare 等反向代理 100 秒收不到数据就会断开连接，
所以流式回复静默超过 `AGY_HEARTBEAT` 秒（默认 15）时，网关发一个心跳：OpenAI 两种格式发 SSE 注释行 `: keep-alive`（客户端按标准会忽略），Anthropic 格式发官方的 `ping` 事件。非流式请求没有心跳，走 Cloudflare 时单次超过 100 秒会被断开，长任务请用流式。

ACP 协议没有 `temperature`、`max_tokens` 参数，这两个字段会被忽略。回复被截断时，网关会在同一个会话里让模型接着写，最多 3 次。

## 管理接口（都要带密钥）

| 接口 | 作用 |
|---|---|
| `GET /admin/status` | 每个账号的状态：能不能用、正在跑几个、成功 / 失败次数、冷却剩余秒数、最近一次错误；以及每账号上限 `per_account` 和排队数 `waiting` |
| `GET /admin/status?quota=1` | 再加上每个账号的剩余额度（每周和 5 小时两个窗口；查询本身不消耗额度，缓存 10 分钟） |
| `POST /admin/reset/{名字}` | 重新登录或换代理后，清掉这个账号的冷却 / 停用标记，并重启它的进程 |
| `GET /healthz` | 存活检查（不需要密钥，只返回账号数） |

密钥可以放在 `Authorization: Bearer <密钥>`、`x-api-key` 或 `api-key` 请求头里。OpenAI 格式的非流式回复里有 `x_account` 字段，写着这次用的是哪个账号。

## 出错时怎么处理

| 情况 | 网关的做法 | 返回给调用方 |
|---|---|---|
| 额度用完 | 这个账号歇 30 分钟（或到额度重置时间），换下一个 | 全部不可用时 429 |
| 限速 / 过载 | 歇 90 秒，换下一个 | 全部不可用时 429 |
| 上游 `RESOURCE_EXHAUSTED` 软错误 | 几秒内认出来；再换 1 个账号试；后台查真实额度，真用完了才让账号歇 | 429 |
| Google 要求验证账号 | 歇 10 分钟，换下一个；用 `account.sh check` 拿验证网址 | 全部不可用时 503 |
| 登录失效 | 停用这个账号，`account.sh login` 后再 `/admin/reset` | 全部不可用时 503 |
| 被 Antigravity 审核拦截 | 不换号（换了也一样） | 400 |
| 模型名不存在（关了兜底时） | 不换号 | 400 |
| 请求里有视频、音频、PDF 等模型收不到的内容，或图片链接下载不了 | 不发给模型 | 400，写明是第几条消息的第几项 |
| 视频模式下模型想读附件以外的文件 | 立刻取消这次请求，不换号 | 400 |
| 所有账号都满了 | 排队，哪个账号先空出来就交给哪个 | 290 秒内没排上：503（Anthropic 格式 529） |
| 超时 | 默认 290 秒 | 504 |

Anthropic 格式下，「没有可用账号」返回 529（overloaded），Claude 系客户端会自动重试。冷却时间等所有选项见 [gateway.env.example](gateway.env.example)。

## 视频模式（试验功能，默认关闭）

在 `gateway.env` 里加一行 `AGY_VIDEO_MODE=1` 并重启后，**Gemini 模型可以看视频、听音频、读 PDF**。其它模型照旧返回 400。

- **怎么发**：和发图片一样，把文件用 base64 直接放在请求里。OpenAI 的 `file`（`file_data` 写 `data:video/mp4;base64,...`）、`video_url` / `audio_url`（data URL）、`input_audio`，Responses 的 `input_file` / `input_audio`，Anthropic 的 `document`（PDF）都可以。**不下载链接**，发 http 链接会返回 400。
- **限制**：单个文件默认最大 40 MB（`AGY_VIDEO_MAX_MB`），一次请求最多 5 个（`AGY_VIDEO_MAX_FILES`）。可用的模型由 `AGY_VIDEO_MODELS` 决定，默认所有 `gemini-` 开头的。
- **速度**：实测 25 秒、23 MB 的竖屏口播视频，Gemini 4 Argon 约 25–30 秒给出按时间顺序的画面、字幕和口播内容；9 秒的小视频约 12 秒。声音的时间点不一定准。

**为什么默认关闭。** Antigravity 的 ACP 接口不收视频，唯一的办法是把文件放到服务器上，让模型用它自带的 `read_file` 工具去读。这等于给模型开了一个读服务器文件的工具，所以网关做了三层防护：

1. **单独的进程，专用的空目录。** 只有带视频 / 音频 / PDF 的请求才会用到每个账号的第二个 `agy` 进程，它只有 `read_file` 一个工具。它的 HOME 是 `accounts/<名字>/media/`（空的，没有 `proxy.env`，没有日志；代理通过环境变量传给它，登录目录用 `--gemini-dir` 参数单独指给它，这个目录受 `agy` 自身保护，`read_file` 读不到），临时目录是 `media/tmp/`，工作目录是空的。实测 `agy` 1.5.3 只允许不经授权读取「当前会话的工作目录」和「临时目录」，其它路径都要申请授权，网关一律拒绝。
2. **网关盯着每一次读文件。** 模型每次用 `read_file`，网关都会核对路径。只要不是本次请求的附件，立刻取消这次请求，返回 400，模型之后写的任何内容都不会发给调用方。
3. **用完即删。** 每次请求的附件写在一个随机命名的专用目录里（`media/w/<32 位随机字符>/`，别的请求猜不到，也列不出来），请求结束立刻删除，`agy` 自己留的对话记录和视频副本也一起删。网关启动时会清掉上次异常退出留下的文件。

实测（agy 1.5.3，Gemini 4 Argon）：在账号目录、日志目录、HOME、临时目录、别的请求的目录、`/tmp`、`/var/tmp`、家目录各放一个带暗号的文件，再加上 `proxy.env` 和 `/proc/self/environ`，用三种接口各种说法让模型去读，暗号一次都没有出现在回复里。把 agent 说明故意换成「让你读什么就读什么」再测一遍，网关拦下了全部 20 次越界读取。

**仍然要知道的：**
- 这是靠「`agy` 的授权机制 + 网关核对路径」来保证的，不是操作系统级的隔离。`agy` 升级后行为可能变，升级后请重新跑一遍测试。想更稳妥，可以把整个网关放进 Docker 或单独的系统用户里。
- 模型的回答里偶尔会带上附件在服务器上的路径（只是路径，没有内容）。
- 视频模式的进程在第一次用到时才启动，第一次请求会多几秒。

## 安全与隐私

- 网关默认只监听 `127.0.0.1`，所有接口（除了 `/healthz`）都要密钥。
- Google 登录凭据由官方 `agy` 自己保存在每个账号的目录里，网关不使用、不打印（面板只从里面读出账号邮箱）。
  唯一的例外是你用 refresh_token 登录时：网关把它写进这个账号的登录文件一次（权限 600），不记日志、不返回。
- 代理地址（含密码）存在 `accounts/<名字>/proxy.env`，权限 600；`list` 只显示主机和端口。
- `accounts/`、`gateway.env` 已写进 `.gitignore`，**不要把它们传到任何地方**。
- 网关启动 `agy` 时加了 `--disable-telemetry`，关闭它的遥测。

## 排查工具

- `python3 /opt/acp-ant/smoke_test.py [图片路径] [并发数]`：冒烟测试（短回复、系统提示词、读图、并发）。
- `python3 /opt/acp-ant/agent_test.py [模型名]`：Agent 相关测试（三种接口的工具调用、流式、JSON 模式）。
- `HOME=/opt/acp-ant/accounts/g1 HTTPS_PROXY=<代理地址> python3 /opt/acp-ant/acp_probe.py --prompt "Reply with OK"`：直接和 `agy acp` 握手，打印协议往来（看账号有哪些模型和思考档位）。
- 在源码目录里 `/opt/acp-ant/venv/bin/python scripts/content_test.py`：离线测试（不用账号、不联网），检查视频 / 音频 / PDF 等会返回 400、文字和图片照常通过。
- `/opt/acp-ant/venv/bin/python scripts/video_mode_test.py`：视频模式的离线测试（同样不用账号、不联网）。
- 日志：`journalctl -u acp-ant -f`；每个账号的 `agy` 日志在 `accounts/<名字>/gw/`。

## 免责声明

- 本项目是个人开源工具，**不是 Google 官方项目**，和 Google 没有任何关系。
- 使用前请阅读并遵守 Google Antigravity 和你所用订阅的服务条款。把个人订阅的额度提供给多人或商业服务使用，可能违反条款，并导致账号被限制或封禁。风险由使用者自己承担。
- 本项目不绕过 Google 的任何限制（比如上面说的 Claude Code）。
- Antigravity CLI 和 ACP 协议都还在变化，新版本可能改变行为。本项目在 `agy` 1.5.2 上测试通过。

## 致谢

用 ACP 协议驱动 `agy acp` 的思路来自 [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle) 里的 `dsh-llm-antigravity-acp`（MIT）。本项目是独立实现，并增加了三种标准接口、工具调用、多账号、每账号代理、自动换号、软错误识别、额度查询和会话清理。

## 许可证

[MIT](LICENSE)

---

<a id="english"></a>
## English

ACP-Ant turns Google's official **Antigravity CLI (`agy`)** into a standard LLM API by talking to it over the **Agent Client Protocol (ACP)**. It exposes **OpenAI Chat Completions, OpenAI Responses and Anthropic Messages**, with **tool calling**, so agents and apps can plug straight in. It runs **several Google accounts side by side, each through its own proxy**.

- **Three APIs:** `/v1/chat/completions`, `/v1/responses` (used by Codex CLI) and `/v1/messages`, all with streaming.
- **Tool calling:** OpenAI `tools`, Anthropic `tools`, and Responses function and custom tools are described to the model. Its requests come back as native `tool_calls` / `tool_use` / `function_call` items. Your agent runs the tools; Antigravity itself has no tools and every permission request is refused.
- **Images:** data URLs, http(s) image links (downloaded by the gateway) and images inside tool results. Plain-text files (txt / md / json / csv …) are passed as text.
- **No video, audio or PDF by default: HTTP 400 instead** (see video mode below). Antigravity's ACP interface hands only text and images to the model. Video, audio and PDF content is silently dropped there, and the model answers anyway with made-up content (measured with agy 1.5.3 on Gemini 4 Argon and Gemini 3.8 Flash). So OpenAI `file` / `input_audio` / `video_url` parts, Responses `input_file`, Anthropic PDF `document` blocks and the like get a 400 that names the offending part, and the caller can fall back to another provider or send video frames as images.
- **Video mode (experimental, off by default, `AGY_VIDEO_MODE=1`):** lets Gemini models watch video, hear audio and read PDFs sent inline as base64 (no link downloads; 40 MB per file and 5 files per request by default). Antigravity's ACP interface accepts no video, so the only way is to put the file on the server and let the model open it with its own `read_file` tool. ACP-Ant guards that in three layers. (1) Such requests run in a second `agy` process per account that has `read_file` as its only tool, with a dedicated HOME (no `proxy.env`, no logs; the proxy comes through the environment), a private temp directory and an empty working directory. agy 1.5.3 reads without permission only inside the session's working directory and the temp directory; everything else raises a permission request, which the gateway refuses. (2) The gateway checks every `read_file` call: a path that is not an attachment of this request cancels the request with a 400, and nothing the model writes afterwards reaches the caller. (3) Attachments live in a per-request directory with a random 128-bit name and are deleted when the request ends, together with agy's own copies. Tested with canary files in the account directory, logs, HOME, temp directory, another request's directory, `/tmp`, `/var/tmp` and the home directory: none leaked, also with an agent deliberately told to open anything (20 of 20 out-of-bounds reads stopped). This relies on agy's permission system plus the gateway's check, not on OS-level isolation; rerun the tests after upgrading agy.
- **Log in with a refresh_token (1.3.0):** in the panel's login step ("已经有 refresh_token？") or with `account.sh token <name>`. Only refresh tokens issued to Antigravity work (Antigravity editor, Antigravity account managers; usually starting with `1//`); Google binds a refresh token to the app that obtained it, so Gemini CLI tokens are refused. The token is written once into `agy`'s own login file in that account's directory (mode 600, same format as after a browser login) and `agy` exchanges it itself through the account's proxy; the gateway holds no Google client secret and never logs or returns the token. If the token does not work, the previous login file is put back.
- **Isolation:** each account has its own HOME directory, so credentials stay separate, and optionally its own HTTP/SOCKS5 proxy.
- **Load spreading:** requests go to the least busy account, round-robin among equals. Each account runs up to 3 requests at once (`AGY_PER_ACCOUNT`, also settable live in the panel). Extra requests queue, first come first served, for whichever account frees a slot first.
- **Stream keep-alive:** after 15 silent seconds (`AGY_HEARTBEAT`), a stream sends an SSE comment or an Anthropic `ping` event, so Cloudflare and other proxies don't cut long thinking or long tool calls. Cloudflare cuts a connection after 100 s with no data.
- **Failover:** quota, rate-limit and "verify your account" errors put that account on cooldown and move to the next one. Expired logins disable the account.
- **Soft errors:** Antigravity reports prompt-filter refusals, "Verification Required" and upstream API errors as plain text, sometimes after minutes of internal retries. ACP-Ant recognises them within seconds and returns proper error codes.
- **Unknown model names** fall back to the default model, which can be turned off.
- **Cleanup:** local conversation files older than 6 hours are deleted every hour.
- **Web panel** at `/`, protected by an admin password (initial one in `panel-initial-password.txt`). From the panel you can:
  - add accounts: test the proxy, then log in with Google through a paste box, or paste the account's refresh_token instead;
  - re-login, change proxy, health-check, clear cooldowns, delete;
  - see per-account quota (weekly and 5-hour windows with reset times) and request / token stats;
  - turn each account on or off;
  - copy connection info and run a test chat;
  - enable an automatic patrol that reads quota every few hours (free) and can optionally send one tiny prompt a day.

**Tested:**
- Codex CLI 0.134 (Responses API) completes a real shell-tool task.
- `scripts/agent_test.py` passes tool calling on all three APIs: first call, round trip with the result, streaming, JSON mode and custom tools.

**Not supported:** Claude Code. Google's Antigravity backend refuses requests that carry Claude Code's `x-anthropic-billing-header` line or its identity sentence, answering `RESOURCE_EXHAUSTED (429)`. ACP-Ant does not work around this; it returns a 429 within seconds instead of hanging.

Install, choose one:
- `./install.sh`: system service, needs sudo.
- `./install.sh --user`: user service via `systemctl --user`, no sudo.
- `docker compose up -d --build`: Docker; everything persists in `./data`.

If `python3-venv` is missing, the installer adds it when apt and sudo are available. Otherwise it names the package to install. It no longer leaves a venv without pip behind.

**Updates:** every 6 hours the panel reads `CHANGELOG.md` from GitHub and shows a banner when a newer version exists. For `git clone` installs, **Update now** in the panel runs `git pull` and `install.sh --update`, then restarts the service. Turn the check off with `ACPANT_UPDATE_CHECK_HOURS=0`.

```bash
git clone https://github.com/czg86389-hub/ACP-Ant.git && cd ACP-Ant && ./install.sh
/opt/acp-ant/account.sh add g1      # enter the proxy (hidden input), then log in with Google
/opt/acp-ant/account.sh token g2    # or: log in with a refresh_token (hidden input)
# Codex: model_provider "acpant", base_url "http://127.0.0.1:8095/v1", wire_api "responses", env_key "ACPANT_KEY"
```

All settings are listed in [gateway.env.example](gateway.env.example).

### 🎬 Recommended: COOL AI — AI video & image studio for e-commerce

Upload one product photo and get talking-head sales videos, story-style ad shorts and a complete product detail-page image set.

- **Talking-head sales videos**: script, shots and lines written for you, generated in one click
- **Story ads**: twist, dialogue and product-placement shorts, scripted and storyboarded automatically
- **Product detail images**: hero, lifestyle, selling-point, detail and comparison images from one white-background photo
- **More tools**: product retouching, GIF animations, digital humans, subtitle removal, upscaling

👉 Try it: [www.coolai.mov](https://www.coolai.mov/?utm_source=github&utm_medium=readme&utm_campaign=acp-ant)


**Disclaimer:** this is an unofficial tool, not affiliated with Google. Follow the Antigravity terms of service. Sharing a personal subscription with many users or with a commercial service may break those terms and get accounts restricted. Use at your own risk. Tested with `agy` 1.5.2.

Inspired by `dsh-llm-antigravity-acp` in [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle) (MIT). Licensed under [MIT](LICENSE).
