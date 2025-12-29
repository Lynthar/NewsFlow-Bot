# NewsFlow Bot

<div align="center">

**自托管 RSS 新闻推送机器人，支持 Discord/Telegram 双平台与自动翻译**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![Telegram](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)

[English](README_EN.md) | 简体中文

</div>

---

## ✨ 功能特点

- 🌐 **多平台支持** - 同时推送到 Discord 和 Telegram
- 🏠 **自托管优先** - 在你自己的 VPS 上运行，数据完全可控
- 🌍 **自动翻译** - 支持 DeepL、OpenAI、Google 三种翻译服务
- ⚡ **智能缓存** - 条件请求 + 翻译缓存，节省带宽和 API 调用
- 👥 **频道隔离** - 每个频道独立管理订阅，互不影响
- 🔌 **REST API** - 可选的管理接口，方便外部集成
- 🐳 **Docker 就绪** - 一键部署，开箱即用

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        NewsFlow 核心引擎                          │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │  RSS 抓取器  │─▶│  内容处理器   │─▶│     翻译服务          │   │
│  │ (feedparser) │  │ (HTML清理)   │  │ (DeepL/OpenAI/Google)│   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│          │                                      │                 │
│          ▼                                      ▼                 │
│  ┌──────────────┐                    ┌──────────────────────┐   │
│  │    数据库    │◀──────────────────▶│      消息分发器       │   │
│  │   (SQLite)   │                    │    (Dispatcher)      │   │
│  └──────────────┘                    └──────────────────────┘   │
│                                                 │                 │
└─────────────────────────────────────────────────┼─────────────────┘
                                                  │
                    ┌─────────────────────────────┼─────────────────────────────┐
                    │                             │                             │
                    ▼                             ▼                             ▼
           ┌──────────────┐             ┌──────────────┐             ┌──────────────┐
           │   Discord    │             │   Telegram   │             │   REST API   │
           │    适配器     │             │    适配器     │             │   (可选)     │
           └──────────────┘             └──────────────┘             └──────────────┘
```

---

## 🛠️ 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| **运行时** | Python 3.11+ | 原生 async/await 支持 |
| **Discord** | discord.py 2.3+ | 斜杠命令（Slash Commands） |
| **Telegram** | python-telegram-bot 20.7+ | Bot API 封装 |
| **数据库** | SQLAlchemy 2.0 + SQLite | 零配置，单文件存储 |
| **RSS 解析** | feedparser + aiohttp | 异步抓取与解析 |
| **翻译** | DeepL / OpenAI / Google | 多服务商可选 |
| **API** | FastAPI + Uvicorn | 可选的 REST 接口 |
| **缓存** | 内存 LRU / Redis | 翻译结果缓存 |

---

## 🚀 快速开始

### 环境要求

- Python 3.11 或更高版本
- Discord Bot Token 和/或 Telegram Bot Token
- （可选）翻译服务 API Key

### 获取 Bot Token

<details>
<summary><b>Discord Bot Token 获取方式</b></summary>

1. 访问 [Discord Developer Portal](https://discord.com/developers/applications)
2. 点击 "New Application" 创建应用
3. 进入 "Bot" 页面，点击 "Add Bot"
4. 点击 "Reset Token" 获取 Token
5. 开启 "MESSAGE CONTENT INTENT"
6. 在 "OAuth2 → URL Generator" 中选择 `bot` 和 `applications.commands`
7. 选择所需权限（Send Messages、Embed Links 等）
8. 使用生成的链接邀请机器人到你的服务器

</details>

<details>
<summary><b>Telegram Bot Token 获取方式</b></summary>

1. 在 Telegram 中搜索 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 命令
3. 按提示设置机器人名称和用户名
4. 获取 Token（格式如：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`）

</details>

### 安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/NewsFlow-Bot.git
cd NewsFlow-Bot

# 安装依赖
pip install -e .

# 或安装完整功能（包含 API、翻译等）
pip install -e ".[all]"
```

### 配置

在项目根目录创建 `.env` 文件：

```bash
# ===== 必填（至少填一个）=====
DISCORD_TOKEN=你的Discord机器人Token
TELEGRAM_TOKEN=你的Telegram机器人Token

# ===== 翻译功能（可选）=====
TRANSLATION_ENABLED=true
TRANSLATION_PROVIDER=deepl  # 可选: deepl, openai, google

# DeepL（推荐，翻译质量高）
DEEPL_API_KEY=你的DeepL_API_Key

# 或使用 OpenAI（支持更多语言）
# TRANSLATION_PROVIDER=openai
# OPENAI_API_KEY=你的OpenAI_API_Key
# OPENAI_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=  # 可选，用于兼容其他 API

# ===== 抓取设置 =====
FETCH_INTERVAL_MINUTES=60  # 检查间隔（分钟）

# ===== REST API（可选）=====
API_ENABLED=false
API_PORT=8000
```

### 运行

```bash
# 直接运行
python -m newsflow.main

# 或使用 make
make run
```

---

## 🐳 Docker 部署

### 使用 Docker Compose（推荐）

```bash
# 1. 创建并编辑 .env 文件（参考上方配置）

# 2. 启动服务
docker-compose -f docker/docker-compose.yml up -d

# 3. 查看日志
docker-compose -f docker/docker-compose.yml logs -f

# 4. 停止服务
docker-compose -f docker/docker-compose.yml down
```

### 使用纯 Docker

```bash
# 构建镜像
docker build -f docker/Dockerfile -t newsflow-bot .

# 运行容器
docker run -d \
  --name newsflow-bot \
  --restart unless-stopped \
  --env-file .env \
  -v newsflow-data:/app/data \
  -p 8000:8000 \
  newsflow-bot
```

---

## 📱 机器人命令

### Discord 命令（斜杠命令）

| 命令 | 说明 |
|------|------|
| `/feed add <url>` | 订阅 RSS 源 |
| `/feed remove <url>` | 取消订阅 |
| `/feed list` | 查看已订阅的源 |
| `/feed test <url>` | 测试 RSS 源是否有效 |
| `/settings language <代码>` | 设置翻译目标语言（如 `zh-CN`） |
| `/settings translate <on/off>` | 开启/关闭翻译 |
| `/status` | 查看机器人状态 |

### Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 开始使用，显示帮助 |
| `/add <url>` | 订阅 RSS 源 |
| `/remove <url>` | 取消订阅 |
| `/list` | 查看已订阅的源 |
| `/test <url>` | 测试 RSS 源是否有效 |
| `/language <代码>` | 设置翻译目标语言 |
| `/translate <on/off>` | 开启/关闭翻译 |
| `/status` | 查看机器人状态 |
| `/help` | 显示帮助信息 |

### 常用语言代码

| 代码 | 语言 |
|------|------|
| `zh-CN` | 简体中文 |
| `zh-TW` | 繁体中文 |
| `en` | 英语 |
| `ja` | 日语 |
| `ko` | 韩语 |

---

## 🔌 REST API

启用 API 后（`API_ENABLED=true`），可通过以下端点管理：

### 健康检查

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 服务状态 |
| `/ready` | GET | 就绪检查（含数据库连接） |
| `/live` | GET | 存活探针 |

### Feed 管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/feeds` | GET | 列出所有 Feed |
| `/api/feeds` | POST | 添加新 Feed |
| `/api/feeds/{id}` | GET | 获取 Feed 详情 |
| `/api/feeds/{id}` | DELETE | 删除 Feed |
| `/api/feeds/{id}/refresh` | POST | 强制刷新 Feed |
| `/api/feeds/test` | POST | 测试 Feed URL |

### 统计信息

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/stats` | GET | 总体统计 |
| `/api/stats/feeds` | GET | 各 Feed 统计 |

---

## ⚙️ 完整配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DISCORD_TOKEN` | - | Discord 机器人 Token |
| `TELEGRAM_TOKEN` | - | Telegram 机器人 Token |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | 数据库连接 URL |
| `TRANSLATION_ENABLED` | `false` | 是否启用翻译 |
| `TRANSLATION_PROVIDER` | `deepl` | 翻译服务商 |
| `DEEPL_API_KEY` | - | DeepL API Key |
| `OPENAI_API_KEY` | - | OpenAI API Key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI 模型 |
| `OPENAI_BASE_URL` | - | OpenAI 兼容 API 地址 |
| `GOOGLE_CREDENTIALS_PATH` | - | Google Cloud 凭据路径 |
| `FETCH_INTERVAL_MINUTES` | `60` | 抓取间隔（分钟） |
| `ENTRY_RETENTION_DAYS` | `7` | 条目保留天数 |
| `CACHE_BACKEND` | `memory` | 缓存后端（memory/redis） |
| `REDIS_URL` | - | Redis 连接 URL |
| `API_ENABLED` | `false` | 是否启用 REST API |
| `API_HOST` | `0.0.0.0` | API 监听地址 |
| `API_PORT` | `8000` | API 监听端口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 🔄 工作原理

### 1. Feed 抓取

```
定时任务触发 → 获取所有活跃 Feed → 发送条件请求（ETag/Last-Modified）
                                           ↓
                                    内容有更新？
                                    ↓         ↓
                                   是         否
                                   ↓          ↓
                            解析并存储新条目   跳过
```

### 2. 消息分发

```
发现新条目 → 查找订阅该 Feed 的频道 → 检查翻译设置
                                         ↓
                                    需要翻译？
                                    ↓         ↓
                                   是         否
                                   ↓          ↓
                              翻译内容      直接发送
                                   ↓
                              发送到频道
```

### 3. 翻译缓存

翻译结果会在两个层级进行缓存：

- **数据库缓存**：永久存储，按条目+语言存储
- **服务缓存**：内存/Redis，减少重复 API 调用

```
请求翻译 → 检查数据库缓存 → 检查服务缓存 → 调用翻译 API → 缓存结果
              ↓ 命中           ↓ 命中
           直接返回          直接返回
```

---

## 📁 项目结构

```
newsflow-bot/
├── src/newsflow/
│   ├── main.py                 # 程序入口
│   ├── config.py               # 配置管理
│   │
│   ├── core/                   # 核心模块
│   │   ├── feed_fetcher.py     # RSS 抓取
│   │   ├── content_processor.py # 内容处理
│   │   └── scheduler.py        # 定时任务
│   │
│   ├── models/                 # 数据模型
│   │   ├── feed.py             # Feed, FeedEntry
│   │   └── subscription.py     # Subscription
│   │
│   ├── repositories/           # 数据访问层
│   │   ├── feed_repository.py
│   │   └── subscription_repository.py
│   │
│   ├── services/               # 业务服务层
│   │   ├── feed_service.py
│   │   ├── subscription_service.py
│   │   ├── dispatcher.py       # 消息分发
│   │   ├── cache.py            # 缓存服务
│   │   └── translation/        # 翻译服务
│   │       ├── base.py
│   │       ├── deepl.py
│   │       ├── openai.py
│   │       └── google.py
│   │
│   ├── adapters/               # 平台适配器
│   │   ├── base.py
│   │   ├── discord/
│   │   └── telegram/
│   │
│   └── api/                    # REST API
│       ├── routes/
│       └── deps.py
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
│
├── pyproject.toml
├── Makefile
└── README.md
```

---

## 🗺️ 开发路线

- [x] **Phase 1**: 核心基础架构
- [x] **Phase 2**: Repository、Service、命令处理
- [x] **Phase 3**: 翻译服务集成
- [x] **Phase 4**: REST API
- [ ] **Phase 5**: Docker 生产就绪化
- [ ] **Phase 6**: Web 管理界面（可选）

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

## 📄 许可证

[MIT License](LICENSE)

---

<div align="center">

**为自托管社区而生 ❤️**

如果这个项目对你有帮助，欢迎 Star ⭐

</div>
