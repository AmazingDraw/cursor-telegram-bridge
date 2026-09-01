# cursor-telegram-bridge

用 Telegram 在手机上控制 Mac 上的 [Cursor](https://cursor.com) **本地 Agent**：选文件夹开会话，发指令，在一条实时更新的消息里看工具调用与回复。

不接 Cursor 桌面 GUI；每个会话都是 SDK 本地 Agent，跑在指定工作目录上。

## 架构

![Cursor Telegram Bridge 架构图](./architecture.svg)

- 仅响应 `ALLOWED_TELEGRAM_USER_ID`（可按 bot 配置群白名单）。
- 每文件夹一个 Cursor bridge；会话记在 `state/bots/<name>/`，重启可 resume。
- 用 `/use` 或会话列表选定当前会话后再发消息。

## 快速开始

```bash
brew install python@3.12   # 已有 3.11+ 可跳过
git clone https://github.com/AmazingDraw/cursor-telegram-bridge.git
cd cursor-telegram-bridge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 填 TELEGRAM_BOT_TOKEN_1、CURSOR_API_KEY；ALLOWED_TELEGRAM_USER_ID 首次可留空
python -m cursor_bridge
```

给 bot 发一条消息拿到数字用户 ID，写入 `.env` 后重启，再 `/new` 选项目。也可双击 `start.command` 前台启动。

**后台（可选）**

```bash
sed "s|__PROJECT_DIR__|$PWD|g" launchd/com.cursor-telegram-bridge.bot.plist \
  > ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
launchctl load ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
```

日志：`state/cursor_bridge.err.log`。Stash/Clash **TUN + fake-ip** 下不要设 `HTTP(S)_PROXY`（会破坏长轮询）。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `/new` `/browse` `/cd` | 开会话 / 选目录 |
| `/sessions` `/use` `/status` `/end` | 列会话 / 切换 / 状态 / 关闭 |
| `/rename` | 当前会话自定义名称 |
| `/model` `/effort` `/mode` | 模型、思考等级、agent/plan |
| `/busy` | 忙碌时：`queue`（默认，可排队）或 `interrupt` |
| `/cancel` `/compact` `/context` | 取消任务 / 压缩上下文 / 恢复上下文 |
| `/files` `/usage` | 浏览发文件 / Cursor 用量 |
| `/restart` | 软重启：重载配置，**不**重载代码 |
| `/reload` | launchd 完整重启，**加载代码变更** |

纯文本 = prompt；图片/文件会写入会话目录并交给 Agent。发 `/help` 可看内置摘要。

Live 回复在同一条消息里原地更新（工具活动 + 最终 HTML）。本地 Web 面板：`http://127.0.0.1:9477`（可设 `CONSOLE_TOKEN`）。

## 安全

- 默认单用户；群聊需显式 `allowed_chat_ids`。
- `full` 权限等同在该文件夹远程执行工具，请只绑自己的账号。
- `/files` 不会发出 `.env`、`.git`、依赖目录等。Agent 也可以用 `launchctl` 重启本服务；Telegram `/reload` / `/restart` 仍可用。

## 排障

1. 运行中勿连发 prompt → 等结束或 `/cancel`
2. 改代码后卡死 → `/reload`
3. 模型无输出 → `/model` 换稳定模型；plan 异常 → `/mode agent`
4. 只改配置 → `/restart`；上下文异常 → `/compact`
5. 会话变红 → `/end` 再 `/new`；无响应看 `state/cursor_bridge.err.log`

## 环境

macOS + 已登录 Cursor 订阅 · Python 3.11+ · Telegram 账号
