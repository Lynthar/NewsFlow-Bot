# NewsFlow-Bot

[![license](https://img.shields.io/github/license/Lynthar/NewsFlow-Bot)](LICENSE)
[![tests](https://img.shields.io/github/actions/workflow/status/Lynthar/NewsFlow-Bot/test.yml?branch=main&label=tests)](https://github.com/Lynthar/NewsFlow-Bot/actions/workflows/test.yml)
[![image](https://img.shields.io/github/actions/workflow/status/Lynthar/NewsFlow-Bot/docker-publish.yml?branch=main&label=image)](https://github.com/Lynthar/NewsFlow-Bot/actions/workflows/docker-publish.yml)
[![release](https://img.shields.io/github/v/release/Lynthar/NewsFlow-Bot)](https://github.com/Lynthar/NewsFlow-Bot/releases)

自托管信息流投递后端：RSS / JSON API / IMAP 邮件推到 Discord、Telegram 与 webhook，带翻译与 AI 日报

[English](README.md) | 简体中文

这个工具可以同时接收 RSS、JSON API 或 IMAP 形式的信息源，按照用户设定的方式过滤、
翻译，再推到 Discord、Telegram 或 webhook（也许以后会接别的），我的目标是尽量简单，
所以它只是一个容器加一个 SQLite 文件。

大部分管理操作都可以在 Discord 或 Telegram 里用预设命令完成，没有 Web 界面，我觉得
它没复杂到需要 Web 界面，想脚本化可以用 REST API。

| 进 | 出 |
|---|---|
| RSS / Atom 订阅源 | Discord bot |
| 用 JSONPath 取值的 JSON API | Telegram bot |
| IMAP 信箱，给那些从来没做过订阅源的 newsletter | 七种格式的出站 webhook——generic、Slack、ntfy、飞书、企业微信、Discord、Matrix，可选 HMAC-SHA256 签名 |
| 任何东西都能 POST 的入站 webhook 端点 | |

进和出之间做的事：每条订阅可以配关键词与正则过滤；翻译可以用 DeepL、Google 或任何
OpenAI 兼容端点（本地模型也行）；日报与周报由 AI 生成；语言与显示样式按频道分别设置；
支持 OPML 导入导出；一个源连续失败十次会自动停掉，不再继续重试。测试在 CI 的
Python 3.11 与 3.13 上跑。

## 安装

Docker 是设计时预设的部署方式。至少要有一个 bot token——Discord 或 Telegram 都行；纯
webhook 部署两个都可以不填。

```bash
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
cp .env.example .env
chmod 600 .env
```

在 `.env` 里填上 `DISCORD_TOKEN` 或 `TELEGRAM_TOKEN`，然后：

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

这会拉取 `ghcr.io/lynthar/newsflow-bot`，镜像里所有可选组件都装好了，启动时自动执行
数据库迁移。想用 Redis 或 PostgreSQL 的话，compose 里有对应的 profile。

从源码运行需要 Python 3.11 到 3.13。3.14 暂时不行，`lxml` 还没有对应的 wheel：

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
```

部署前可以先跑一次离线自检，不访问网络，也不访问数据库：

```bash
python -m newsflow.checkconfig
```

## 用法

在任何 bot 能看见的频道里订阅：

```
/feed add https://news.ycombinator.com/rss
```

几秒内会推一条预览，之后按轮询间隔更新。

```
/feed list                 # 这个频道订阅了什么
/feed status <url>         # 报错、退避窗口、最近的文章
/feed filter-set <url> …   # 关键词或 /正则/ 过滤
/digest enable …           # 打开日报或周报
```

Discord 侧是 `/feed`、`/settings`、`/digest` 三个命令组，要求「管理服务器」权限、
只能在服务器频道里用（私信里不可用），另有一个对所有人开放的 `/status`。Telegram 侧是
同样的能力，写成平铺命令（`/add`、`/remove`、`/filter`、`/digest` 这些）。

## 配置

进程本身读环境变量或 `.env`，另有两个 YAML：`webhooks.yaml` 声明出站目标，
`sources.yaml` 声明非 RSS 的源。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DISCORD_TOKEN` / `TELEGRAM_TOKEN` | 无 | 至少填一个，除非你只用 webhook |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | 换成 `postgresql+asyncpg://…` 即上 Postgres |
| `FETCH_INTERVAL_MINUTES` | `60` | 轮询间隔 |
| `TRANSLATION_ENABLED` / `TRANSLATION_PROVIDER` | `false` / `deepl` | 三选一：`google`、`deepl`、`openai` |
| `API_ENABLED` / `API_KEY` | `false` / 无 | REST API 与入站 `/api/ingest` |
| `INGEST_API_KEY` | 无 | 只开放 `/api/ingest` 的独立密钥，给往里推内容的外部系统用 |
| `OPENAI_BASE_URL` | 无 | 把翻译或日报指向本地模型 |

改完 `.env` 要 `up -d` 才生效，`restart` 不会重新读。

## 能力边界

- **Matrix 是走 webhook 的，不是原生支持。** 有一个面向 matrix-hookshot 的 `matrix`
  格式，但没有 Matrix 适配器，也没有 Matrix 侧的命令。
- **不支持 Microsoft Teams。** 老的 O365 connector 已于 2026-05 停用，新路径需要一个
  M365 租户才能验。
- **只支持单实例。** Redis 只是翻译缓存，不是协调层；两个副本连同一个数据库不在设计范围内。
- **webhook 只是出口**，不能用来管理订阅；要改就得改 `webhooks.yaml` 再重启。
- **0.x 期间配置面会在小版本之间变**，哪些在兼容性承诺范围内、哪些不在，兼容性文档里
  写清楚了。

## 文档

- [用户指南](docs/user-guide.md) —— 每条命令、每个设置、常见问题。
- [兼容性说明](docs/compatibility.md) —— 0.x 保证什么、不保证什么。
- [变更日志](CHANGELOG.md)

## 安全

`.env` 里存着 bot token 和 API key，记得 `chmod 600`。

自带的 compose 把 REST API 绑在 `127.0.0.1`。如果你打开 `API_ENABLED=true` 又改了绑定
地址，注意：不设 `API_KEY` 时读端点是不需要鉴权的，而 VPS 上并没有一层可以依赖的
「局域网」。

用户提交的订阅源 URL 会对内网地址段做校验，重定向每一跳都重新校验。这不能替代出口
限制：DNS rebinding 挡不住，而 `localhost`、云厂商 metadata 这类主机名是**有意放行**的。

## 许可证

GNU Affero 通用公共许可证 v3.0 only —— 见 [LICENSE](LICENSE)。Copyright (c) 2026 Lynthar。

### 第三方许可证

本项目按 **LGPL v3** 使用 **[python-telegram-bot](https://python-telegram-bot.org/)**
——该库是 GPL v3 / LGPL v3 双许可、由接收方选择。本项目未修改它，你可以把它换成
自己构建的、接口兼容的版本。

两份许可证正文随该库一起分发，在 `python_telegram_bot-*.dist-info/` 里
（`LICENSE.lesser` 是 LGPL、`LICENSE` 是 GPL），`ghcr.io/lynthar/newsflow-bot`
镜像的 `/opt/venv` 下同样有。
