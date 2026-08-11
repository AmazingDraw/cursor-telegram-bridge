# cursor-telegram-bridge

在 Mac 上用 Telegram 控制无头 [Cursor](https://cursor.com) Agent 会话：选文件夹开 session，用手机发指令，在一条实时更新的消息里看工具调用、代码片段和完整回复——不必碰 Mac。

不涉及 Cursor 桌面 GUI：每个会话都是 Cursor SDK 的**本地 Agent**，在指定文件夹上运行。

英文说明：[README.md](README.md) · 更新记录：[CHANGELOG.md](CHANGELOG.md)

## 快速开始

```bash
brew install python@3.12   # 已有 Python 3.11+ 可跳过
git clone https://github.com/AmazingDraw/cursor-telegram-bridge.git
cd cursor-telegram-bridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m cursor_bridge
```

首次运行可把 `ALLOWED_TELEGRAM_USER_ID` 留空。给新 bot 发一条消息，它会回复你的 Telegram 数字 ID；写入 `.env` 后重启，用 `/new` 选项目目录。

Mac 上可双击 `start.command` 前台启动；软重启 `/restart` 时窗口不会关。

## 工作原理

```
Telegram（手机）  <->  cursor-telegram-bridge（Mac）  <->  Cursor SDK bridge  ->  各文件夹本地 Agent
```

- 一个长期运行的 Python 进程监听 Telegram。
- **会话注册表**记录每个 Agent（文件夹、id、状态），并写入 `state/bots/<bot>/sessions.json`，重启后可 `Agent.resume` 重新挂载。
- **每个文件夹一个 Cursor bridge**；子进程以该文件夹为 cwd 启动（bot 进程本身不 `chdir`）。同一文件夹共用一个 bridge；该文件夹最后一个 session 结束时关闭 bridge。
- 仅响应 **你的** Telegram 用户 ID，其他人消息会被忽略。
- 发消息前用 `/use <id>` 或会话列表按钮选定**当前会话**；不会自动切到别的 session。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/new [path]` | 新建会话；无路径时弹出文件夹选择器 |
| `/browse [path]` | 用按钮浏览目录，再选「使用此文件夹」 |
| `/cd <path>` | 在指定绝对路径开 session |
| `/sessions` | 会话列表；可切换 / 取消 / 关闭 |
| `/use <id>` | 设定本聊天的当前会话 |
| `/status` | 当前会话详情、运行状态、上下文用量 |
| `/rename <name>` | 重命名当前会话（`/rename reset` 恢复默认） |
| `/compact` | 压缩当前会话上下文（Agent `/compact`） |
| `/context [list\|refresh\|<agent-id>]` | Agent 重置后恢复先前对话上下文 |
| `/model` | 选择当前会话模型 |
| `/effort` | 设置思考等级（视模型支持） |
| `/mode [agent\|plan]` | 查看或切换 agent / plan 模式 |
| `/busy [interrupt\|queue]` | 忙碌时：**queue**（默认）对新消息显示 **📋 排队 / ⚡ 发送 / ❎ 取消**；**取消**只丢掉新命令。**interrupt** 则总是中止当前任务并执行新消息 |
| `/cancel` | 取消当前会话正在跑的任务 |
| `/end <id>` | 关闭会话（若为该文件夹最后一个，会关掉对应 bridge） |
| `/files` | 浏览当前会话文件夹；点选发送到 Telegram |
| `/files find <name>` | 按文件名搜索 |
| `/usage` | Cursor 订阅用量、赠金、当前会话上下文 |
| `/restart` | 软重启：重载 `.env` / `config.toml` 并重新挂载会话（**不**重载 Python 代码） |
| `/reload` | 通过 launchd 完整重启，**可加载代码变更** |
| _(纯文本)_ | 作为 prompt 发给当前会话；回复在一条 live 消息里流式更新 |
| _(图片/文件)_ | 保存到会话目录并交给 Agent（说明文字可作 prompt） |

发 `/start` 或 `/help` 可看 bot 内摘要。`/start` 菜单含 **文件**、**状态**、**重启**（软）、**重载**（完整）。

状态：绿 = 运行中，黄 = 空闲，红 = 错误。

### 运行中的 live 展示

发 prompt 或 `/compact` 时，**同一条 Telegram 消息**全程原地更新。

**页眉**（引用块，始终可见）：

```
[s1] MyProject · composer-2.5
```

**运行中**可能显示：

| 层 | 内容 |
| --- | --- |
| 正文预览 | 最新助手文本（过长会截断） |
| 活动行 | 🟡 运行中 / ✅ 完成 / ❌ 失败 |
| 工具片段 | 编辑、grep、shell 等的红/绿单色预览 |
| 计时 | 页眉 `⏳ 12s` / `⌛ 12s` |

活动与工具事件会**立即刷新**（绕过编辑节流）。

**结束时**同一消息替换为最终回复：Markdown 转 Telegram HTML；✅ 完成、✋ 已取消、🔴 错误。超长会拆成多条（4096 字限制）。

实现见 `cursor_bridge/formatting.py`。

### 出站文件（Agent → Telegram）

| 来源 | 时机 | 方式 |
| --- | --- | --- |
| **GenerateImage** | 工具完成时 | 自动发图/动图 |
| **其它** | 按需 | `/files` 或 `/files find` |

`.git`、`node_modules`、`.env` 等路径不会列出或发送。

### 入站文件（Telegram → Agent）

支持照片、文档、动图、视频、音频；说明文字可作 prompt。

- 保存在会话目录下 `.cursor_bridge/inbound/`
- **图片**以视觉形式传给 Agent（`SDKImage`）
- **其它文件**在 prompt 里用路径引用
- 单文件最大 **20 MB**（Telegram 下载限制）
- 连续多文件约 **1.2s** 内合并为一条 prompt

### `/usage`

从本机 Cursor 状态库读取登录信息，调用 Cursor 用量 API：套餐、账期、用量比例、包含额度、**赠金**；有当前会话时附带上下文信息。

### Web 控制台

bot 运行时在本机打开 [http://127.0.0.1:9477](http://127.0.0.1:9477)：会话列表（支持多 bot）、每会话事件、日志尾部。

- 可在 `.env` 设 `CONSOLE_TOKEN`，访问 `http://127.0.0.1:9477?token=...`
- `config.toml` 可改 `console_port`
- `console_enabled = false` 可关闭
- 终端面板：双击 `console.command`

事件 JSONL：`state/bots/<bot>/events/{sid}.jsonl`（由 `event_log_max` 限制条数）。每个 bot 的会话在 `state/bots/<name>/` 下平级存放；控制台对非 default bot 显示为 `BotName:s1`。进程日志 / pid 仍在 `state/`。

## 环境要求

- macOS，已安装 [Cursor](https://cursor.com) 并有订阅
- **Python 3.11+**（`brew install python@3.12`）
- Telegram 账号与手机客户端

## 安装配置

### 1. 创建 Telegram Bot

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)
2. `/newbot`，取名与 username
3. 复制 **bot token**

### 2. Cursor API Key

1. 打开 [cursor.com/dashboard](https://cursor.com/dashboard) → **Integrations**
2. 创建 **User API Key** 并复制

### 3. 安装

```bash
git clone https://github.com/AmazingDraw/cursor-telegram-bridge.git
cd cursor-telegram-bridge

brew install python@3.12
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

编辑 `.env`：

```
TELEGRAM_BOT_TOKEN_1=
TELEGRAM_BOT_TOKEN_2=
CURSOR_API_KEY=
ALLOWED_TELEGRAM_USER_ID=     # 首次可留空
```

可选编辑 `config.toml`：`projects_root`、`model`、`[[bookmarks]]`、`[[bots]]`（多 bot 同进程）。

### 4. 首次运行 — 绑定用户

```bash
python -m cursor_bridge
```

给 bot 发消息，把返回的数字 ID 写入 `.env`，重启后 `/new`。

### 5. 后台服务（可选）

```bash
sed "s|__PROJECT_DIR__|$PWD|g" launchd/com.cursor-telegram-bridge.bot.plist \
  > ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
launchctl load ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
```

日志：`state/cursor_bridge.out.log`、`state/cursor_bridge.err.log`。

使用 Stash/Clash **TUN + fake-ip** 时，**不要**在 plist 或 `.env` 里设 `HTTP(S)_PROXY`（会破坏长轮询）。纯 HTTP 代理模式才按需设 `HTTPS_PROXY`（见 `.env.example`）。

```bash
launchctl list | grep cursor-telegram-bridge   # 中间列 0 = 正常
launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot"
```

## 远程重载（手机）

1. 打开 Telegram bot
2. 发 **`/reload`** 或点 **⚛️ 重新加载**
3. 等待 bot 通知已恢复

| 操作 | Telegram | 说明 |
| --- | --- | --- |
| 软重启 | `/restart` 或 **♻️ 重启** | 重载 `.env` / `config.toml`，不重载 Python |
| 完整重启 | `/reload` 或 **⚛️ 重新加载** | launchd 完整重启，**加载代码变更** |

`/reload` 前建议等当前任务结束或 `/cancel`。

SSH 远程：

```bash
ssh you@your-mac 'launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot"'
```

## 配置说明

### `.env`（密钥，勿提交 Git）

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN_1` | 是 | 主 bot token（BotFather） |
| `TELEGRAM_BOT_TOKEN_2` | 否 | 第二个 bot（`[[bots]]` + `token_env`） |
| `CURSOR_API_KEY` | 是 | Cursor Integrations 用户 API Key |
| `ALLOWED_TELEGRAM_USER_ID` | 是 | 你的 Telegram 数字用户 ID |
| `CONSOLE_TOKEN` | 否 | 设置后 Web 控制台需 `?token=…` |

### `config.toml`（非密钥）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `projects_root` | `~/Projects` | `/new` 扫描根目录 |
| `model` | `composer-2.5` | 新会话默认模型 |
| `models` | — | `/model` 可选列表 |
| `effort` | — | 新会话默认思考等级 |
| `busy_policy` | `queue` | 忙碌时 `queue` 或 `interrupt` |
| `setting_sources` | `["user","project"]` | 加载磁盘 `.cursor/` 配置；**不含** Customize 里的 User Rules |
| `rules_file` | — | 每条 prompt 注入的规则文件（人设等） |
| `rules` | — | 内联规则（可与 `rules_file` 合并） |
| `browser_page_size` | `20` | `/browse`、`/files` 每页条数 |
| `event_log_max` | `500` | 每会话事件 JSONL 上限 |
| `console_enabled` | `true` | 本地 Web 面板 |
| `console_host` | `127.0.0.1` | Web 绑定地址 |
| `console_port` | `9477` | Web 端口 |
| `[[bookmarks]]` | — | `/new` 顶部固定目录 |
| `[[bots]]` | — | 多 bot；推荐 `token_env` 从 `.env` 读 token |
| `permission`（per-bot） | `full` | 工具能力闸：`full` 或 `readonly` |
| `allowed_chat_ids`（per-bot） | `[]` | 允许说话的群/超级群 `chat.id` 白名单 |

### 多 Bot

`config.toml` 示例：

```toml
[[bots]]
name = "default"
token_env = "TELEGRAM_BOT_TOKEN_1"
permission = "full"

[[bots]]
name = "group-reader"
token_env = "TELEGRAM_BOT_TOKEN_2"
permission = "readonly"
allowed_chat_ids = [-1001234567890]
```

- 各 bot **会话独立**，平级目录：`state/bots/default/`、`state/bots/secondary/` 等（各有 `sessions.json` + `events/`）
- 共用同一 `CURSOR_API_KEY`、`rules.md`、全局默认配置
- **`permission = "readonly"`**：硬拦 Shell / 写改删 / 生图 / MCP；只允许 Read / Grep / Glob 等；路径必须落在会话 `cwd` 内（含 symlink 出界）
- **`allowed_chat_ids`**：白名单群内**任何人**可触发；私聊仍只许 `allowed_user_id`；会话按 `chat.id` 共享（一群一会话）
- `/reload`、`/restart`（含菜单按钮）始终仅主人可执行

## 安全说明

- 默认单用户：仅 `ALLOWED_TELEGRAM_USER_ID`（或 per-bot `allowed_user_id`）可操作。
- 可选：`allowed_chat_ids` 放开指定群；工具层可用 `permission = "readonly"` 收紧能力。
- `full` 时 Agent 在所选文件夹内拥有完整工具权限，视同远程 shell。
- 密钥只在 `.env`（已 gitignore）；`/files` 不会发送 `.env`、`.git`、依赖目录等。
- Agent 内试图停服务的 shell 会被拦截；请用 Telegram `/reload` 或 `/restart`。
- Web 控制台默认只绑 `127.0.0.1`；若代理或隧道暴露，请设 `CONSOLE_TOKEN`。

## 排障

1. 等跑完或 **`/cancel`** — 勿在 🟡 运行时又发 prompt
2. **`/reload`** — 改代码后或卡死时完整重启
3. **`/model` → `composer-2.5`** — 某模型总无输出时
4. **`/mode agent`** — plan 模式异常时
5. **`/restart`** — 只重载配置，不重载代码
6. **`/compact`** — 上下文过大或行为怪异
7. **`/end <id>` + `/new`** — 重启后 session 变红（路径变了等）
8. **`/use <id>`** — 消息没反应时显式选会话
9. **`/end` 闲置会话** — 每个文件夹的 bridge 约占 50MB+
10. **Telegram 无响应** — 看 `state/cursor_bridge.err.log`；TUN 下取消 `HTTPS_PROXY`；确认 launchd plist 已替换 `__PROJECT_DIR__`

多数 bridge / Agent 卡住会在日志里自动恢复；仍卡死时 `/reload` 通常能清掉。

## 说明与限制

- **内存**：每文件夹一个 bridge（Node，约 50MB+），小内存 Mac 及时 `/end`。
- 超长回复会拆多条 Telegram 消息。
- 重启后 session 变红：多半无法 resume，`/end` 后 `/new`。
- TLS 代理/VPN：`export NODE_EXTRA_CA_CERTS=/path/to/root-ca.pem` 后再启动。

## 项目结构

```
cursor_bridge/
  __main__.py     # 入口 python -m cursor_bridge
  bot.py          # Telegram 处理、LiveMessage、prompt 执行
  sessions.py     # SessionManager、bridge、run_prompt 流
  formatting.py   # live/final HTML、Markdown 转换
  attachments.py  # 出站文件、/files、GenerateImage
  inbound.py      # Telegram 媒体下载
  folders.py      # /new、/browse 目录选择
  config.py       # .env + config.toml
  context.py      # 上下文用量、/context 恢复
  usage.py        # /usage API
  events.py       # 会话事件 JSONL
  webconsole.py   # 本地 Web 面板
  console.py      # 终端状态视图
config.toml       # 非密钥配置
.env              # 密钥（gitignore）
rules.md          # 全局规则注入（gitignore，见 rules.md.example）
launchd/          # launchd plist 模板
state/            # 进程日志/pid；各 bot 会话在 bots/<name>/（gitignore）
start.command     # 前台启动
console.command   # 终端面板
```
