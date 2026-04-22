# NewsFlow Bot

<div align="center">

**自托管的 RSS 推送后端 —— 为 Discord / Telegram 机器人提供订阅、翻译、过滤、AI 日报**

[![Python](https://img.shields.io/badge/Python-3.11%20–%203.13-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ed.svg)](https://www.docker.com/)

[English](README.md) | 简体中文

</div>

> 📖 **本文是快速上手指引**。完整的命令、配置、高级部署、设计决策、扩展开发等细节见 **[GUIDE.md](GUIDE.md)**。

---

## 🎯 它是什么

部署在你自己服务器上的 **RSS 推送后端**。给它一个 Discord 或 Telegram bot token，在频道里 `/feed add <url>`，源有更新时自动推送到频道，可选自动翻译、关键词过滤、定期 AI 日报汇总。

**设计原则**：自托管优先、零配置启动、渐进式复杂度、组件可替换。

---

## ✨ 功能概览

| 功能 | 说明 |
|---|---|
| 📡 **RSS 抓取** | `feedparser` + `aiohttp`，条件请求 / 并发 / SSRF 校验 / 大小上限 |
| 🌐 **双平台推送** | Discord 斜杠命令 + Telegram 前缀命令并发工作 |
| 🌍 **自动翻译** | DeepL / OpenAI / Google，两层缓存（DB + 内存/Redis） |
| 🎯 **关键词过滤** | 单订阅 include/exclude 规则，被过滤条目不消耗翻译 API |
| 📰 **AI 日报 / 周报** | 可选 LLM 摘要，按日/周把频道收到的文章聚合成简报 |
| 📋 **OPML 导入导出** | 从 Feedly / Reeder 搬家；仓库带 23 源预置清单 |
| 🔁 **指数退避** | 源失效自动拉长重试；10 次连续失败自动停订并通知 |
| ⏸ **暂停 / 恢复** | 临时不收推送又不删订阅 |
| 🩺 **健康可视** | `/feed status` 查健康、错误、最近文章；容器 HEALTHCHECK 集成 |
| 🐳 **Docker 就绪** | 一条 compose 启动；alembic 自动迁移 |

---

## 🏗️ 架构

```
               单 asyncio 进程
   ┌──────────────────────────────────────────┐
   │  Discord / Telegram adapter               │
   │  Dispatch loop  ← 抓取→翻译→推送            │
   │  Cleanup loop   ← 清过期条目                │
   │  Digest loop    ← AI 日报/周报              │
   │  Platform monitor  ← heartbeat              │
   └──────────────────────────────────────────┘
                      │
                      ▼
          SQLite 文件 / Postgres (可选)
```

详细分层、各模块职责、设计决策见 [GUIDE.md 第 9 章](GUIDE.md#九架构总览)。

---

## 🚀 快速开始（Docker）

**Debian 12 / Ubuntu 22+**（其他发行版改对应 Docker 安装命令即可）：

```bash
# 1. 安装 Docker（如未装）
sudo apt update && sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 2. 拉代码、填 token
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
cp .env.example .env
nano .env     # 至少填一个 DISCORD_TOKEN 或 TELEGRAM_TOKEN

# 3. 启动
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

看到 `Discord bot logged in as ...` 或 `Telegram bot started successfully` 就跑起来了。

**获取 token**：Discord 看 [Developer Portal](https://discord.com/developers/applications)；Telegram 找 [@BotFather](https://t.me/BotFather)。详细步骤见 [GUIDE.md](GUIDE.md#一完整命令参考)。

---

## 📋 环境要求

| 项 | 要求 |
|---|---|
| OS | Linux（Debian 12 / Ubuntu 22+ / CentOS 等） |
| Python | 3.11 / 3.12 / 3.13（3.14 暂不支持，`lxml` 无 wheel） |
| 内存 | 最低 256 MiB，推荐 512 MiB |
| 网络 | 仅需出站 HTTPS 443 |
| Docker | 20.10+ + Compose v2（或用 [systemd 部署](GUIDE.md#六高级部署与运维)） |

---

## 📱 核心命令速查

**Discord**：

```
/feed add <url>          订阅（几秒内推一条预览）
/feed remove <url>       退订
/feed list               看当前订阅
/feed filter-set ...     关键词过滤
/digest enable ...       开日报/周报
```

**Telegram**：

```
/add <url>               订阅
/list                    看当前订阅
/filter <url> ...        关键词过滤
/digest enable daily 9   开日报
```

**完整命令参考**（30+ 个）：[GUIDE.md 第 1 章](GUIDE.md#一完整命令参考)。

---

## ⚙️ 关键配置

最小 `.env`：

```bash
DISCORD_TOKEN=your_real_token
# 或
TELEGRAM_TOKEN=your_real_token
```

常见额外配置：

```bash
FETCH_INTERVAL_MINUTES=30                # 抓取间隔
TRANSLATION_ENABLED=true                 # 开翻译
TRANSLATION_PROVIDER=openai              # 或 deepl / google
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com # OpenAI 兼容端点（可选）
DIGEST_MODEL=gpt-5.4-mini                # 日报用的模型
```

**完整 30+ 配置项**：[GUIDE.md 第 2 章](GUIDE.md#二完整配置项)。

---

## 🐛 常见问题

<details>
<summary><b>订阅了但没收到消息</b></summary>

正常。新订阅成功后几秒会推一条最新文章预览；之后按 `FETCH_INTERVAL_MINUTES`（默认 60min）周期抓。源没新文章就没推送。用 `/feed status <url>` 看详情。
</details>

<details>
<summary><b>容器无限重启，日志 <code>InvalidToken</code></b></summary>

`.env` 里的 token 还是占位符或者输错了。改后 `docker compose restart newsflow`。
</details>

<details>
<summary><b>想自定义 AI 日报的风格 / 翻译的口吻</b></summary>

`.env` 里设 `TRANSLATION_SYSTEM_PROMPT=` 或 `DIGEST_SYSTEM_PROMPT=` 覆盖默认。详见 [GUIDE.md §3.3](GUIDE.md#33-自定义-ai-提示词)。
</details>

**更多 FAQ**（DNS / 翻译没生效 / 数据重置 / 升级报错 等）：[GUIDE.md 第 14 章](GUIDE.md#十四常见陷阱--faq)。

---

## 🤝 贡献 / 二次开发

架构设计、代码风格、扩展点（加新平台 / 新翻译 provider / 新 API 端点）都在 **[GUIDE.md 第 7-16 章](GUIDE.md#开发--架构)**。

快速开发循环：

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio
make test      # 134 个测试
make lint
```

---

## 📄 许可证

[MIT](LICENSE)

---

<div align="center">

**为自托管社区而生 ❤️**

觉得有用欢迎 ⭐ 和分享

[报告 bug](https://github.com/Lynthar/NewsFlow-Bot/issues) · [功能建议](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Pull Request](https://github.com/Lynthar/NewsFlow-Bot/pulls)

</div>
