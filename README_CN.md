# NewsFlow Bot

<div align="center">

**自托管的 RSS 推送后端 —— 为 Discord / Telegram 机器人提供订阅、翻译、健康管理**

[![Python](https://img.shields.io/badge/Python-3.11%20–%203.13-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red.svg)](https://www.sqlalchemy.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ed.svg)](https://www.docker.com/)

[English](README.md) | 简体中文

</div>

---

## 📖 目录

- [项目定位](#-项目定位)
- [功能特点](#-功能特点)
- [架构总览](#-架构总览)
- [快速开始（Docker）](#-快速开始docker)
- [环境要求](#-环境要求)
- [配置项](#-配置项)
- [命令参考](#-命令参考)
  - [Discord 斜杠命令](#discord-斜杠命令)
  - [Telegram 命令](#telegram-命令)
- [OPML 导入导出](#-opml-导入导出)
- [REST API（可选）](#-rest-api可选)
- [高级部署](#-高级部署)
- [健康检查与监控](#-健康检查与监控)
- [常见问题](#-常见问题)
- [贡献与开发](#-贡献与开发)
- [许可证](#-许可证)

---

## 🎯 项目定位

NewsFlow Bot 是一个**部署在你自己服务器上**的 RSS 推送后端。给它一个 Discord 或 Telegram 机器人的 token，在你的频道里发 `/feed add <url>`，源更新时 bot 会自动把新文章推到频道，可选翻译成你想要的语言。

**设计原则**

- 🏠 **自托管优先** —— 你的 VPS、你的 token、你的翻译 API key，数据完全在你手里
- ⚡ **零配置启动** —— 最小只需填 `DISCORD_TOKEN` 或 `TELEGRAM_TOKEN`，一条 `docker compose up` 搞定
- 🧩 **渐进式复杂度** —— 翻译、REST API、Redis、Postgres 全部可选，按需启用
- 🛡️ **生产就绪** —— 指数退避、SSRF 校验、rate-limit、健康检查、alembic 迁移全部内置

**反设计：本仓库不做的**

- ❌ 不内置多租户 / 配额 / 计费（中心化版本会另开仓库）
- ❌ 不自带 Web UI（管理靠 bot 命令或可选 REST API）
- ❌ 不强制翻译（默认关闭；你想用再配 DeepL / OpenAI / Google key）

---

## ✨ 功能特点

| 功能 | 说明 |
|---|---|
| 📡 **RSS 抓取** | 基于 `feedparser` + `aiohttp`，支持 ETag / Last-Modified 条件请求、并发抓取、大小上限、SSRF 校验 |
| 🌐 **多平台推送** | Discord（斜杠命令）+ Telegram（前缀命令）同时工作，平台隔离、频道隔离 |
| 🌍 **自动翻译** | 可选 DeepL / OpenAI-compatible / Google Cloud Translation；两层缓存（DB + 内存/Redis）节省 API 调用 |
| 🔁 **指数退避** | 源临时失效时自动拉长重试间隔，10 次连续失败自动停订并**通知用户** |
| 🎯 **关键词过滤** | 单条订阅可设 include / exclude 关键词，只推感兴趣内容；被过滤的条目不消耗翻译 API |
| 📰 **AI 日报 / 周报** | 可选开启 LLM 摘要，按日或按周把频道推送过的文章归纳成一份简报 |
| 📋 **OPML 导入导出** | 从 Feedly / Reeder / 其他 RSS reader 直接搬家，或备份到文件 |
| ⏸ **暂停 / 恢复** | 临时不收推送又不想删订阅？`/feed pause` 即可 |
| 👀 **订阅预览** | 订阅成功后几秒内就推 1 条最新文章，不用等下一轮 |
| 🩺 **健康状态可视** | `/feed status <url>` 显示失败次数、退避窗口、最近文章等 |
| 🐳 **Docker 就绪** | 一条 `docker compose` 启动；内置 heartbeat 健康检查、alembic 自动迁移 |
| 🔒 **链路安全** | URL 白名单（http/https、拒绝私网/环回/云元数据 IP）、响应体 5 MiB 上限 |
| 🔧 **灵活数据库** | 默认 SQLite 单文件；改一条 URL 即可切 PostgreSQL |

---

## 🏗️ 架构总览

```
                   ┌──────────── NewsFlow Process ────────────┐
                   │                                           │
                   │   单 asyncio 事件循环，并发以下任务：         │
                   │                                           │
Discord Slash ◀───▶│  • Discord Adapter       ┐                │
Commands           │                          │                │
Telegram  ◀───────▶│  • Telegram Adapter      │ register 到     │
Polling            │                          │ Dispatcher       │
                   │  • Dispatch Loop         ┘                │
                   │    (每 N 分钟抓取 → 翻译 → 推送)              │
                   │  • Cleanup Loop                           │
                   │    (每 24h 删过期条目)                         │
                   │  • Platform Monitor                       │
                   │    (每 30s 检查 adapter 连通 → 写 heartbeat) │
                   │                                           │
                   │  共享单例：Settings / Fetcher /              │
                   │  TranslationService / Cache / DB Engine    │
                   │                                           │
                   └───────────────┬───────────────────────────┘
                                   │
                                   ▼
                    SQLite 文件  /  Postgres（可选）
```

**分层**：`adapters/` 和 `api/`（可选 REST）→ `services/` 业务逻辑 → `repositories/` 数据访问 → `models/` SQLAlchemy ORM；`core/` 是无状态原语（抓取、HTML 清洗、URL 安全校验、时间格式化）。

详细架构说明见 [DEVELOPMENT.md](DEVELOPMENT.md)。

---

## 🚀 快速开始（Docker）

**Debian 12 / Ubuntu 22+ 示例**。其他 Linux 发行版把 Docker 安装命令换成对应的即可。

### 1. 装 Docker（如未安装）

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

### 2. 准备 token

<details>
<summary><b>Discord Bot Token</b></summary>

1. 访问 [Discord Developer Portal](https://discord.com/developers/applications) 新建应用
2. `Bot` 页面 → `Reset Token` 拿到 token
3. 打开 **MESSAGE CONTENT INTENT**
4. `OAuth2 → URL Generator` 勾 `bot` + `applications.commands`，权限选 `Send Messages`、`Embed Links`、`Read Message History`
5. 用生成的链接把 bot 邀请到你的服务器
</details>

<details>
<summary><b>Telegram Bot Token</b></summary>

1. 在 Telegram 里找 [@BotFather](https://t.me/BotFather) 发 `/newbot`
2. 按提示设置机器人名字
3. 拿到 token，格式类似 `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
</details>

### 3. 克隆、配置、启动

```bash
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot

cp .env.example .env
nano .env     # 至少填一个 token

docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

看到 `Discord bot logged in as ...` 或 `Telegram bot started successfully` 就跑起来了。

### 4. 在你的频道 / 聊天里试命令

Discord：
```
/feed test https://feeds.bbci.co.uk/news/rss.xml
/feed add  https://feeds.bbci.co.uk/news/rss.xml
```

Telegram：
```
/test https://feeds.bbci.co.uk/news/rss.xml
/add  https://feeds.bbci.co.uk/news/rss.xml
```

订阅成功后几秒内会推一条最新文章作为预览；之后按 `FETCH_INTERVAL_MINUTES`（默认 60 分钟）的周期自动推送新内容。

---

## 📋 环境要求

| 项目 | 要求 | 备注 |
|---|---|---|
| **操作系统** | Linux（Debian 12 / Ubuntu 22+ / CentOS 等） | Docker 路线跨发行版通用 |
| **Python** | 3.11 / 3.12 / 3.13 | **3.14 暂不支持**（`lxml` 尚无 wheel） |
| **内存** | 最低 256 MiB，推荐 512 MiB | 订阅数 100+ 建议 1 GiB |
| **磁盘** | SQLite 模式 ~100 MiB 起 | 按保留天数 × 订阅数量增长 |
| **网络** | 仅需出站 HTTPS 443 | Bot 主动连 Discord / Telegram / RSS 源 |
| **Docker** | 20.10+ + Compose v2 | 或 [systemd 部署](#高级部署) |

Windows 也能开发（`make dev`），但生产部署建议 Linux。

---

## ⚙️ 配置项

所有配置通过 `.env` 文件或环境变量传入。最小配置：

```bash
# 二选一，也可以两个都填
DISCORD_TOKEN=your_real_discord_token
TELEGRAM_TOKEN=your_real_telegram_token
```

### 全部配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| **平台** | | |
| `DISCORD_TOKEN` | 空 | Discord Bot token，空则不启用 Discord |
| `TELEGRAM_TOKEN` | 空 | Telegram Bot token，空则不启用 Telegram |
| **数据库** | | |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | SQLAlchemy 异步连接串；换成 `postgresql+asyncpg://...` 即切 Postgres |
| **翻译** | | |
| `TRANSLATION_ENABLED` | `false` | 总开关 |
| `TRANSLATION_PROVIDER` | `deepl` | `deepl` / `openai` / `google` |
| `DEEPL_API_KEY` | 空 | DeepL API key |
| `OPENAI_API_KEY` | 空 | OpenAI 或兼容 API 的 key |
| `OPENAI_MODEL` | `gpt-5.4-nano` | OpenAI 模型名（用于翻译，便宜快速） |
| `OPENAI_BASE_URL` | 空 | 自定义 OpenAI 兼容端点（比如 DeepSeek、通义） |
| `GOOGLE_CREDENTIALS_PATH` | 空 | Google Cloud 服务账号 JSON 路径 |
| `GOOGLE_PROJECT_ID` | 空 | GCP 项目 ID |
| **调度** | | |
| `FETCH_INTERVAL_MINUTES` | `60` | 抓取循环间隔 |
| `CLEANUP_INTERVAL_HOURS` | `24` | 清理循环间隔 |
| `ENTRY_RETENTION_DAYS` | `7` | 保留多少天的条目 |
| **缓存** | | |
| `CACHE_BACKEND` | `memory` | `memory`（进程内 LRU）或 `redis` |
| `REDIS_URL` | 空 | 如 `redis://redis:6379/0` |
| `TRANSLATION_CACHE_TTL_DAYS` | `7` | 翻译结果缓存 TTL |
| **AI 日报 / 周报** | | |
| `DIGEST_PROVIDER` | `openai` | 目前仅支持 openai-compatible |
| `DIGEST_MODEL` | `gpt-5.4-mini` | 生成 digest 用的模型（主题聚合质量比 nano 好） |
| `DIGEST_MAX_ARTICLES` | `50` | 单次 digest 最多纳入的文章数 |
| `DIGEST_MAX_INPUT_CHARS_PER_ARTICLE` | `300` | 单篇文章喂给 LLM 时的字符上限（标题 + 摘要） |
| `DIGEST_CHECK_INTERVAL_MINUTES` | `5` | digest 调度循环的检查间隔 |
| **REST API** | | |
| `API_ENABLED` | `false` | 启用 FastAPI 管理端点 |
| `API_HOST` | `0.0.0.0` | 监听地址 |
| `API_PORT` | `8000` | 监听端口 |
| **日志** | | |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console`（带颜色）或 `json`（结构化，适合 Loki / ELK） |
| **配额（0 = 无限制）** | | |
| `MAX_FEEDS_PER_CHANNEL` | `0` | 单频道最多订阅数 |
| `MAX_ENTRIES_PER_FEED` | `0` | 单 feed 保留多少条 |

---

## 📱 命令参考

命令按**使用频率**和**功能类别**分组。Discord 用斜杠命令 + 参数（自动补全），Telegram 用前缀命令 + 空格分隔参数。

### Discord 斜杠命令

#### Feed 管理

| 命令 | 说明 |
|---|---|
| `/feed add <url>` | 订阅；成功后几秒会推 1 条预览 |
| `/feed remove <url>` | 退订 |
| `/feed pause <url>` | 暂停（不删，可 resume） |
| `/feed resume <url>` | 恢复已暂停的订阅 |
| `/feed list [page]` | 分页列出本频道订阅（每页 20 条） |
| `/feed test <url>` | 不订阅只测试 URL 是否是合法 feed |
| `/feed status <url>` | 单 feed 详情：健康、最近失败时间、最近 5 篇文章 |

#### 翻译设置

| 命令 | 说明 |
|---|---|
| `/settings language <code>` | 设置本频道**所有订阅**的翻译目标语言 |
| `/settings translate <on\|off>` | 开关本频道**所有订阅**的翻译 |
| `/feed language <url> <code>` | **单条订阅**的语言（覆盖频道默认） |
| `/feed translate <url> <on\|off>` | **单条订阅**的翻译开关（覆盖频道默认） |

#### OPML

| 命令 | 说明 |
|---|---|
| `/feed export` | 导出本频道订阅为 OPML 文件 |
| `/feed import <file>` | 上传 `.opml` / `.xml` 文件批量订阅（上限 1 MB） |

#### 关键词过滤

| 命令 | 说明 |
|---|---|
| `/feed filter-set <url> include:<csv> exclude:<csv>` | 设置单个 feed 的关键词过滤（两个参数都可选） |
| `/feed filter-show <url>` | 查看当前过滤规则 |
| `/feed filter-clear <url>` | 移除过滤 |

匹配行为：**title + summary 合并、大小写不敏感、子串匹配**。`include` 需要"至少包含一个"，`exclude` 需要"一个都不包含"，两者叠加评估。规则为空 = 不过滤。

#### AI 日报 / 周报

| 命令 | 说明 |
|---|---|
| `/digest enable schedule:<daily\|weekly> hour_utc:<0-23> [weekday:<0-6>] [language:<code>] [include_filtered:<bool>] [max_articles:<n>]` | 启用或更新日报 / 周报 |
| `/digest show` | 查看当前配置 |
| `/digest disable` | 关闭（配置保留） |
| `/digest now` | 立即生成并投递一次（测试用） |

- **需要配置 `OPENAI_API_KEY`**，复用翻译用的 key，默认模型 `gpt-5.4-mini`
- 窗口 = "自上次投递以来"；首次则过去 24 小时（日报）或 7 天（周报）
- 默认只归纳"**实际推送的文章**"；若想把过滤掉的也纳入，加 `include_filtered:true`
- 与逐篇推送**并存** —— 用户既会收到每篇实时推送，也会按时收到汇总

#### 其他

| 命令 | 说明 |
|---|---|
| `/status` | Bot 整体状态 |

### Telegram 命令

#### Feed 管理

| 命令 | 说明 |
|---|---|
| `/add <url>` | 订阅 |
| `/remove <url>` | 退订 |
| `/pause <url>` | 暂停 |
| `/resume <url>` | 恢复 |
| `/list [page]` | 分页列出 |
| `/test <url>` | 测试 URL |
| `/info <url>` | 单 feed 详情（等于 Discord 的 `/feed status`） |

#### 翻译设置

| 命令 | 说明 |
|---|---|
| `/language <code>` | 频道级翻译语言 |
| `/translate <on\|off>` | 频道级翻译开关 |
| `/setlang <url> <code>` | 单 feed 语言 |
| `/settrans <url> <on\|off>` | 单 feed 翻译开关 |

#### OPML

| 命令 | 说明 |
|---|---|
| `/export` | 导出 OPML 文件 |
| `/import <url>` | 从 URL 获取 OPML 导入 |
| *上传 `.opml` 文件* | 拖进聊天即自动导入，**无需命令** |

#### 关键词过滤

| 命令 | 说明 |
|---|---|
| `/filter <url>` | 显示当前过滤规则 |
| `/filter <url> clear` | 移除过滤 |
| `/filter <url> include=a,b exclude=c,d` | 设置过滤（两个字段都可选） |

#### AI 日报 / 周报

| 命令 | 说明 |
|---|---|
| `/digest show` | 显示当前配置 |
| `/digest enable daily <hour_utc> [lang]` | 启用日报 |
| `/digest enable weekly <weekday> <hour_utc> [lang]` | 启用周报（weekday 支持 `mon`…`sun` 或 `0`…`6`） |
| `/digest disable` | 关闭 |
| `/digest now` | 立即投递一次 |

#### 其他

| 命令 | 说明 |
|---|---|
| `/start`、`/help` | 帮助 |
| `/status` | Bot 状态 |

### 常用语言代码

`zh-CN`（简中）· `zh-TW`（繁中）· `en` · `ja` · `ko` · `fr` · `de` · `es` · `ru` · `ar` · ...（DeepL / OpenAI 支持的任意 BCP-47 代码）

---

## 📋 OPML 导入导出

**场景**：从 Feedly、Reeder、NetNewsWire 搬家；备份订阅列表；在多个频道 / 实例间迁移。

### 导出

- **Discord**：`/feed export` → bot 返回一个 `.opml` 文件附件
- **Telegram**：`/export` → bot 发送 `.opml` 文件

### 导入

- **Discord**：`/feed import file:<上传文件>`
- **Telegram**：直接把 `.opml` 文件拖进聊天（无需输入命令），或 `/import <url>` 从 URL 获取

### 预置的源列表

仓库 `samples/curated-feeds.opml` 提供 23 个精选源（WSJ、FT、NYT、Bloomberg、Economist、Reuters、Atlantic、Foreign Affairs、Nautilus、Longreads、Cloudflare Blog、EFF 等）。

**使用**：下载该文件到本地电脑 → Discord `/feed import` 上传 or Telegram 直接拖文件 → 23 个源一次订阅完。

> **批量导入不会刷屏** —— 避免一次推 20+ 条预览消息，导入走"安静"流程，等下一轮 dispatch 循环时正常发送新文章。

---

## 🔌 REST API（可选）

`.env` 里设 `API_ENABLED=true` 启用。

> ⚠️ **当前 API 无认证 + CORS 允许所有来源**。仅建议在内网 / 本机使用，公网暴露前请加 nginx 反代 + Basic Auth 或 API Key 中间件。

### 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务状态（简单存活） |
| `GET` | `/ready` | 就绪检查（含 DB 连接） |
| `GET` | `/live` | 存活探针（K8s 友好） |
| `GET` | `/api/feeds` | 列出所有 feed |
| `POST` | `/api/feeds` | 添加 feed |
| `GET` | `/api/feeds/{id}` | 单 feed 详情 |
| `DELETE` | `/api/feeds/{id}` | 删 feed |
| `POST` | `/api/feeds/{id}/refresh` | 手动触发抓取 |
| `POST` | `/api/feeds/test` | 测试 URL |
| `GET` | `/api/stats` | 整体统计 |
| `GET` | `/api/stats/feeds` | 每个 feed 的统计 |

`LOG_LEVEL=DEBUG` 时自动暴露 `/docs`（Swagger UI）。

---

## 🔧 高级部署

### Docker Compose Profiles

```bash
# 加 Redis（多实例或想让翻译缓存跨重启）
docker compose -f docker/docker-compose.yml --profile with-redis up -d

# 加 Postgres（订阅上 10 万条再考虑；SQLite 之前都够用）
docker compose -f docker/docker-compose.yml --profile with-postgres up -d

# 全栈
docker compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres up -d
```

### systemd + venv（无 Docker）

```bash
sudo apt install -y python3 python3-venv python3-pip libxml2-dev libxslt1-dev build-essential

cd /opt && sudo git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e ".[all]"
sudo cp .env.example .env && sudo nano .env

sudo tee /etc/systemd/system/newsflow.service > /dev/null <<'EOF'
[Unit]
Description=NewsFlow Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=newsflow
WorkingDirectory=/opt/NewsFlow-Bot
EnvironmentFile=/opt/NewsFlow-Bot/.env
ExecStart=/opt/NewsFlow-Bot/.venv/bin/python -m newsflow.main
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/NewsFlow-Bot/data

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now newsflow
sudo journalctl -u newsflow -f
```

### 升级到新版本

```bash
cd ~/NewsFlow-Bot
git pull
docker compose -f docker/docker-compose.yml up -d --build
# alembic 迁移会在启动时自动跑，无需手动操作
```

### 备份

```bash
# SQLite 模式：单文件备份
docker cp newsflow-bot:/app/data/newsflow.db ./backup-$(date +%F).db

# Postgres 模式：
docker compose -f docker/docker-compose.yml exec postgres pg_dump -U newsflow > backup-$(date +%F).sql
```

---

## 🩺 健康检查与监控

Bot 在 `data/heartbeat/` 下维护 4 个文件，每个任务自己负责刷新：

| 文件 | 刷新者 | 刷新频率 |
|---|---|---|
| `dispatch` | dispatch loop | 每轮抓取结束后 |
| `cleanup` | cleanup loop | 每轮清理结束后 |
| `digest` | digest loop | 每次 digest 检查循环结束 |
| `discord` | platform monitor | Discord 连接活跃时每 30 秒 |
| `telegram` | platform monitor | Telegram 连接活跃时每 30 秒 |

Dockerfile 的 `HEALTHCHECK` 检查**所有文件都在 120 分钟内被刷新过**；任何一个老化 → 容器状态 `unhealthy`。

查看各任务健康：

```bash
docker exec newsflow-bot ls -la /app/data/heartbeat/
```

查看 bot 内部统计（如果启用了 REST API）：

```bash
curl http://localhost:8000/api/stats | jq
```

---

## 🐛 常见问题

<details>
<summary><b>订阅成功了但等了很久没有新消息</b></summary>

正常行为。Bot 在订阅成功几秒内会推送 1 条**预览**（最新那条文章），之后按 `FETCH_INTERVAL_MINUTES`（默认 60 分钟）的周期抓取新内容。如果源没有新文章，自然就不会有推送。

用 `/feed status <url>` 查看上次成功抓取时间、错误计数等。
</details>

<details>
<summary><b>Bot 无限重启，日志里有 <code>InvalidToken</code></b></summary>

多半是 `.env` 里的 token 还是 `.env.example` 里的占位符（如 `your_discord_bot_token`），或者 token 输错了。确认后重启容器。
</details>

<details>
<summary><b>某个 feed 从未成功过 —— 网络问题</b></summary>

容器内 DNS 或出站 HTTPS 被拦。先测：

```bash
docker exec newsflow-bot python -c "import socket; print(socket.gethostbyname('hnrss.org'))"
```

能解析就是 DNS OK。若无法解析，在 `docker-compose.yml` 的 `newsflow` 服务下确认有：

```yaml
    dns:
      - 1.1.1.1
      - 8.8.8.8
```

然后 `docker compose up -d --force-recreate newsflow`。

HTTPS 到特定源超时（其他源工作正常）通常是源端或 Cloudflare 针对 VPS IP 的限流，等一会儿再试或换源。
</details>

<details>
<summary><b>翻译没起作用</b></summary>

依次检查：
1. `.env` 里 `TRANSLATION_ENABLED=true`
2. `TRANSLATION_PROVIDER` 对应的 key 已配置（`DEEPL_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_CREDENTIALS_PATH`）
3. 订阅本身开了翻译：`/feed status <url>` 看 Translation: On
4. 重启容器让配置生效
5. `LOG_LEVEL=DEBUG` 看详细日志里的翻译调用
</details>

<details>
<summary><b>升级后 <code>alembic</code> 报错 "table feeds already exists"</b></summary>

你的 DB 是 alembic 接入之前由 `init_db()` 创建的。初始 migration 内建了 idempotent 检查能自动处理这种情况，如果仍报错：

```bash
# 手工标记当前 schema 为最新
docker compose exec newsflow alembic stamp head
```
</details>

<details>
<summary><b>想重置所有数据</b></summary>

SQLite 模式下，停容器并删除 data volume：

```bash
docker compose -f docker/docker-compose.yml down
docker volume rm docker_newsflow-data
docker compose -f docker/docker-compose.yml up -d
```
</details>

<details>
<summary><b>日志里 <code>No adapter for platform: discord</code></b></summary>

Discord 登录还没成功（或网络瞬断）。Dispatch loop 会跳过此轮，下轮自动恢复。持续出现则检查 `DISCORD_TOKEN` 和容器网络。
</details>

---

## 🤝 贡献与开发

想修 bug 或加功能？先看 [DEVELOPMENT.md](DEVELOPMENT.md) —— 那里有完整的架构说明、分层约定、代码风格、扩展点示范、日常工作流。

### 快速开发循环

```bash
# 建虚拟环境（推荐 uv 快 10×）
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio

# 跑测试
make test         # 或: .venv/bin/python -m pytest tests/ -v

# 类型检查 + lint
make typecheck
make lint
make format
```

### 项目结构

```
NewsFlow-Bot/
├── src/newsflow/
│   ├── main.py                 # 启动入口
│   ├── config.py               # pydantic-settings 配置
│   ├── core/                   # 无状态原语
│   │   ├── feed_fetcher.py     # HTTP + feedparser
│   │   ├── content_processor.py
│   │   ├── url_security.py     # SSRF 校验
│   │   ├── opml.py             # OPML parse/build
│   │   └── timeutil.py         # 相对时间格式化
│   ├── models/                 # SQLAlchemy ORM
│   ├── repositories/           # DB 查询封装
│   ├── services/               # 业务逻辑
│   │   ├── dispatcher.py       # ★ 中央调度循环
│   │   ├── feed_service.py
│   │   ├── subscription_service.py
│   │   ├── cache.py
│   │   └── translation/        # 翻译 provider + 工厂
│   ├── adapters/               # 平台 I/O
│   │   ├── discord/bot.py
│   │   └── telegram/bot.py
│   └── api/                    # 可选 FastAPI 路由
├── alembic/                    # 数据库迁移
├── docker/                     # Dockerfile + compose
├── samples/                    # 预置 OPML 等资源
├── tests/                      # 单元测试
├── pyproject.toml              # 依赖权威
├── Makefile                    # 常用命令
├── DEVELOPMENT.md              # 开发者文档
├── README.md                   # 英文主文档
└── README_CN.md                # 本文档（中文翻译）
```

---

## 📄 许可证

[MIT](LICENSE)

---

<div align="center">

**为自托管社区而生 ❤️**

觉得有用欢迎点 ⭐ 和分享

[报告 bug](https://github.com/Lynthar/NewsFlow-Bot/issues) · [功能建议](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Pull Request](https://github.com/Lynthar/NewsFlow-Bot/pulls)

</div>
