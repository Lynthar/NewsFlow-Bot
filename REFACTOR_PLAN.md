# NewsFlow-Bot 重构计划

## 一、设计目标与核心原则

### 核心定位：自托管优先，预留托管扩展

```
┌─────────────────────────────────────────────────────────────┐
│                    NewsFlow Core                             │
│               (设计为自托管友好、零配置启动)                    │
└─────────────────────────────────────────────────────────────┘
                            │
           ┌────────────────┴────────────────┐
           ▼                                 ▼
    ┌──────────────┐                ┌──────────────────┐
    │   自托管模式  │                │   托管服务模式    │
    │   (核心场景)  │                │   (未来扩展)      │
    │              │                │                  │
    │ • Docker一键 │                │ • 多租户隔离      │
    │ • 用户自己Key │                │ • 用量计费       │
    │ • SQLite默认 │                │ • PostgreSQL     │
    │ • 无限制使用  │                │ • 配额管理       │
    └──────────────┘                └──────────────────┘
```

### 设计原则

1. **零配置启动**: 只需 Discord/Telegram Token 即可运行
2. **渐进式复杂度**: 基础功能简单，高级功能可选
3. **用户拥有数据**: 自托管用户完全控制自己的数据
4. **扩展不侵入**: 托管服务扩展不影响核心代码

### 功能目标

1. **多平台支持**: Discord、Telegram（优先），Webhook（扩展）
2. **灵活部署**: Docker容器化，支持VPS/云服务器/本地部署
3. **用户自主管理**: 添加/删除RSS源、选择翻译、设置推送频率
4. **插件化架构**: 易于添加新平台、新翻译服务
5. **生产就绪**: 日志、健康检查、优雅关闭

---

## 二、关于推送方式的建议

### 当前方案 vs 替代方案对比

| 推送方式 | 优点 | 缺点 | 推荐场景 |
|---------|------|------|---------|
| **Discord Bot** | 群组共享、交互命令、免费 | 需要Discord账号 | 团队/社区使用 |
| **Telegram Bot** | 轻量、全球可用、API优秀 | 国内需代理 | 个人/小团队 |
| **Webhook** | 通用性强、可对接任何系统 | 需要接收端 | 开发者/自动化 |
| **RSS输出** | 用户用自己喜欢的阅读器 | 无推送，需轮询 | 高级用户 |
| **Email摘要** | 普及度高、可定时汇总 | 易进垃圾箱 | 日报/周报 |
| **Web推送** | 浏览器原生支持 | 需要前端 | 有Web界面时 |

### 推荐方案

采用**多渠道统一架构**，核心逻辑与推送渠道解耦：

```
┌─────────────────────────────────────────────────────────────┐
│                     NewsFlow Core                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ RSS获取  │──│ 内容处理 │──│ 翻译服务 │──│ 消息队列 │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Discord    │    │   Telegram   │    │   Webhook    │
│   Adapter    │    │   Adapter    │    │   Adapter    │
└──────────────┘    └──────────────┘    └──────────────┘
```

---

## 三、技术栈选型

### 自托管优先的技术选择

| 层级 | 自托管默认 | 托管服务可选 | 理由 |
|------|-----------|-------------|------|
| **语言** | Python 3.11+ | - | 异步生态成熟、库丰富 |
| **数据库** | SQLite | PostgreSQL | SQLite零配置，单文件备份 |
| **缓存** | 内存 LRU | Redis | 自托管无需额外服务 |
| **任务调度** | APScheduler | Celery | 单进程足够，无需消息队列 |
| **配置** | .env 文件 | 数据库 | 简单直观 |
| **消息平台** | discord.py / python-telegram-bot | - | 官方推荐库 |

### 可选组件（按需启用）

| 组件 | 用途 | 何时需要 |
|------|------|---------|
| **FastAPI** | REST API / 健康检查 | 需要外部管理或监控时 |
| **Redis** | 翻译缓存 / 速率限制 | 高频翻译或多实例时 |
| **PostgreSQL** | 大规模数据 | 条目超过10万或需要多实例 |

### 依赖分层策略

```toml
# 核心依赖（必须）
[tool.poetry.dependencies]
python = "^3.11"
discord-py = "^2.3.0"
python-telegram-bot = "^20.7"
feedparser = "^6.0.0"
aiohttp = "^3.9.0"
sqlalchemy = "^2.0.0"
aiosqlite = "^0.19.0"
pydantic-settings = "^2.1.0"

# 可选依赖（按需安装）
[tool.poetry.extras]
api = ["fastapi", "uvicorn"]           # REST API
redis = ["redis"]                       # Redis缓存
postgres = ["asyncpg"]                  # PostgreSQL
translation = ["google-cloud-translate", "deepl"]  # 翻译服务
all = ["api", "redis", "postgres", "translation"]
```

**安装示例**:
```bash
# 最小安装（自托管）
pip install newsflow-bot

# 带翻译功能
pip install newsflow-bot[translation]

# 完整安装（托管服务）
pip install newsflow-bot[all]
```

---

## 四、项目结构设计

```
newsflow-bot/
├── src/
│   └── newsflow/
│       ├── __init__.py
│       ├── main.py                 # 应用入口
│       ├── config.py               # 配置管理 (pydantic-settings)
│       │
│       ├── core/                   # 核心业务逻辑
│       │   ├── __init__.py
│       │   ├── feed_fetcher.py     # RSS获取
│       │   ├── content_processor.py # 内容清洗、摘要
│       │   └── scheduler.py        # 任务调度
│       │
│       ├── models/                 # 数据模型
│       │   ├── __init__.py
│       │   ├── base.py             # SQLAlchemy Base
│       │   ├── feed.py             # Feed, FeedEntry
│       │   ├── user.py             # User, Subscription
│       │   └── channel.py          # Channel (Discord/Telegram)
│       │
│       ├── services/               # 外部服务
│       │   ├── __init__.py
│       │   ├── translation/
│       │   │   ├── base.py         # 翻译接口抽象
│       │   │   ├── google.py
│       │   │   ├── deepl.py
│       │   │   └── openai.py       # GPT翻译（可选）
│       │   └── cache.py            # 缓存服务
│       │
│       ├── adapters/               # 平台适配器
│       │   ├── __init__.py
│       │   ├── base.py             # 适配器抽象基类
│       │   ├── discord/
│       │   │   ├── bot.py
│       │   │   └── commands.py     # Slash Commands
│       │   ├── telegram/
│       │   │   ├── bot.py
│       │   │   └── handlers.py
│       │   └── webhook/
│       │       └── sender.py
│       │
│       ├── api/                    # REST API
│       │   ├── __init__.py
│       │   ├── routes/
│       │   │   ├── feeds.py        # /api/feeds
│       │   │   ├── users.py        # /api/users
│       │   │   └── health.py       # /health
│       │   └── deps.py             # 依赖注入
│       │
│       └── utils/
│           ├── html.py             # HTML清理
│           └── rate_limiter.py     # 速率限制
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
│
├── migrations/                     # Alembic数据库迁移
│   └── versions/
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
│
├── config/
│   ├── .env.example
│   └── logging.yaml
│
├── pyproject.toml                  # 项目配置 (Poetry/PDM)
├── README.md
└── Makefile                        # 常用命令
```

---

## 五、核心模块设计

### 5.1 配置管理 (config.py)

**设计原则**: 最少必填项，合理默认值

```python
from pydantic_settings import BaseSettings
from typing import Literal

class Settings(BaseSettings):
    """
    自托管模式：只需填写 DISCORD_TOKEN 或 TELEGRAM_TOKEN 即可启动
    """

    # ===== 必填（至少一个） =====
    discord_token: str | None = None
    telegram_token: str | None = None

    # ===== 可选配置（有合理默认值） =====

    # 数据库（默认SQLite，零配置）
    database_url: str = "sqlite+aiosqlite:///./data/newsflow.db"

    # 翻译（默认关闭，用户可选择开启）
    translation_enabled: bool = False
    translation_provider: Literal["google", "deepl", "openai"] = "google"
    google_credentials_path: str | None = None
    deepl_api_key: str | None = None
    openai_api_key: str | None = None

    # 调度
    fetch_interval_minutes: int = 60

    # 缓存（默认内存，可选Redis）
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None

    # API服务（默认关闭）
    api_enabled: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ===== 托管服务扩展（自托管用户忽略） =====

    # 多租户模式
    multi_tenant: bool = False

    # 配额限制（0=无限制）
    max_feeds_per_channel: int = 0
    max_entries_retention_days: int = 7

    # 管理员
    admin_user_ids: list[str] = []

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def validate_minimal_config(self) -> bool:
        """验证最小配置：至少需要一个平台Token"""
        return bool(self.discord_token or self.telegram_token)
```

**最简 .env 示例**:
```bash
# 只需这一行即可启动 Discord 机器人
DISCORD_TOKEN=your_discord_bot_token

# 或者 Telegram
# TELEGRAM_TOKEN=your_telegram_bot_token
```

**完整 .env 示例**:
```bash
# === 平台配置 ===
DISCORD_TOKEN=your_discord_bot_token
TELEGRAM_TOKEN=your_telegram_bot_token

# === 翻译配置（可选） ===
TRANSLATION_ENABLED=true
TRANSLATION_PROVIDER=deepl
DEEPL_API_KEY=your_deepl_api_key

# === 高级配置（可选） ===
DATABASE_URL=sqlite+aiosqlite:///./data/newsflow.db
FETCH_INTERVAL_MINUTES=30
CACHE_BACKEND=memory

# === API配置（可选） ===
API_ENABLED=true
API_PORT=8000
```

### 5.2 数据模型 (models/)

```python
# models/feed.py
from sqlalchemy import String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime

class Feed(Base):
    """RSS源"""
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    title: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(default=True)

    # 缓存控制
    etag: Mapped[str | None] = mapped_column(String(256))
    last_modified: Mapped[str | None] = mapped_column(String(256))
    last_fetched_at: Mapped[datetime | None]

    # 关系
    entries: Mapped[list["FeedEntry"]] = relationship(back_populates="feed")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="feed")


class FeedEntry(Base):
    """RSS条目"""
    __tablename__ = "feed_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))

    guid: Mapped[str] = mapped_column(String(2048))  # 唯一标识
    title: Mapped[str] = mapped_column(String(1024))
    link: Mapped[str] = mapped_column(String(2048))
    summary: Mapped[str | None]
    published_at: Mapped[datetime | None]

    # 翻译缓存
    title_translated: Mapped[str | None]
    summary_translated: Mapped[str | None]
    translation_lang: Mapped[str | None] = mapped_column(String(10))

    # 元数据
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    feed: Mapped["Feed"] = relationship(back_populates="entries")


# models/user.py
class Subscription(Base):
    """用户订阅"""
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)

    # 平台标识
    platform: Mapped[str] = mapped_column(String(20))  # discord, telegram
    platform_user_id: Mapped[str] = mapped_column(String(64))
    platform_channel_id: Mapped[str] = mapped_column(String(64))

    # 订阅的Feed
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))

    # 用户偏好
    translate: Mapped[bool] = mapped_column(default=True)
    target_language: Mapped[str] = mapped_column(String(10), default="zh-CN")
    is_active: Mapped[bool] = mapped_column(default=True)

    feed: Mapped["Feed"] = relationship(back_populates="subscriptions")
```

### 5.3 适配器抽象 (adapters/base.py)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Message:
    """统一消息格式"""
    title: str
    summary: str
    link: str
    source: str
    published_at: str | None
    image_url: str | None = None


class BaseAdapter(ABC):
    """平台适配器基类"""

    @abstractmethod
    async def start(self) -> None:
        """启动适配器"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        pass

    @abstractmethod
    async def send_message(
        self,
        channel_id: str,
        message: Message
    ) -> bool:
        """发送消息到指定频道"""
        pass

    @abstractmethod
    async def register_commands(self) -> None:
        """注册交互命令"""
        pass
```

### 5.4 翻译服务抽象 (services/translation/base.py)

```python
from abc import ABC, abstractmethod
from functools import lru_cache

class TranslationProvider(ABC):
    """翻译服务抽象"""

    @abstractmethod
    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None
    ) -> str:
        pass

    @abstractmethod
    def supports_language(self, lang_code: str) -> bool:
        pass


class CachedTranslationService:
    """带缓存的翻译服务"""

    def __init__(
        self,
        provider: TranslationProvider,
        cache_backend: CacheBackend | None = None
    ):
        self.provider = provider
        self.cache = cache_backend

    async def translate(self, text: str, target_lang: str) -> str:
        # 生成缓存key
        cache_key = f"trans:{hash(text)}:{target_lang}"

        # 检查缓存
        if self.cache:
            cached = await self.cache.get(cache_key)
            if cached:
                return cached

        # 调用翻译
        result = await self.provider.translate(text, target_lang)

        # 存入缓存
        if self.cache:
            await self.cache.set(cache_key, result, ttl=86400 * 7)  # 7天

        return result
```

---

## 六、用户交互设计

### 6.1 Discord Slash Commands

```
/feed add <url>              - 添加RSS源
/feed remove <url>           - 移除RSS源
/feed list                   - 列出所有订阅
/feed test <url>             - 测试RSS源是否有效

/settings language <code>    - 设置翻译目标语言
/settings translate <on/off> - 开启/关闭翻译
/settings interval <minutes> - 设置检查间隔

/status                      - 查看机器人状态
```

### 6.2 Telegram Commands

```
/start                       - 初始化机器人
/add <url>                   - 添加RSS源
/remove <url>                - 移除RSS源
/list                        - 列出订阅
/language <code>             - 设置语言
/translate <on/off>          - 翻译开关
/help                        - 帮助信息
```

---

## 七、部署方案

### 7.1 Docker Compose (推荐)

```yaml
# docker/docker-compose.yml
version: "3.8"

services:
  newsflow:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: newsflow-bot
    restart: unless-stopped
    env_file:
      - ../.env
    volumes:
      - newsflow-data:/app/data
    ports:
      - "8000:8000"  # API端口（可选）
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    container_name: newsflow-redis
    restart: unless-stopped
    volumes:
      - redis-data:/data

volumes:
  newsflow-data:
  redis-data:
```

### 7.2 Dockerfile

```dockerfile
# docker/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装Python依赖
COPY pyproject.toml poetry.lock ./
RUN pip install poetry && \
    poetry config virtualenvs.create false && \
    poetry install --no-dev --no-interaction

# 复制代码
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY config/ ./config/

# 创建数据目录
RUN mkdir -p /app/data

ENV PYTHONPATH=/app/src
ENV DATABASE_URL=sqlite+aiosqlite:///./data/newsflow.db

EXPOSE 8000

CMD ["python", "-m", "newsflow.main"]
```

### 7.3 一键部署脚本

```bash
#!/bin/bash
# deploy.sh

# 克隆仓库
git clone https://github.com/yourname/newsflow-bot.git
cd newsflow-bot

# 创建环境变量
cp .env.example .env
echo "请编辑 .env 文件填入你的Token..."
read -p "按Enter继续..."

# 启动服务
docker-compose -f docker/docker-compose.yml up -d

# 查看日志
docker-compose -f docker/docker-compose.yml logs -f
```

---

## 八、实施路线图

### Phase 1: 核心重构 (基础框架)
- [ ] 初始化项目结构，配置 pyproject.toml
- [ ] 实现配置管理 (pydantic-settings)
- [ ] 设计数据库模型，配置 Alembic
- [ ] 实现 RSS 获取模块
- [ ] 实现内容处理模块 (HTML清理)

### Phase 2: 平台适配器
- [ ] 实现适配器基类
- [ ] 重构 Discord 适配器 (Slash Commands)
- [ ] 实现 Telegram 适配器
- [ ] 实现 Webhook 适配器

### Phase 3: 翻译与缓存
- [ ] 实现翻译服务抽象
- [ ] 集成 Google/DeepL/OpenAI
- [ ] 实现 Redis 缓存
- [ ] 添加翻译缓存机制

### Phase 4: API 与管理
- [ ] 实现 FastAPI 基础路由
- [ ] 实现 Feed 管理 API
- [ ] 添加健康检查端点
- [ ] 添加 API 文档

### Phase 5: 生产就绪
- [ ] Docker 容器化
- [ ] 编写 docker-compose
- [ ] 添加日志配置
- [ ] 编写单元测试
- [ ] 编写部署文档

### Phase 6: 扩展功能 (可选)
- [ ] Web 管理界面 (React/Vue)
- [ ] RSS 输出功能
- [ ] AI 智能摘要
- [ ] 消息去重优化

---

## 九、托管服务扩展预留

虽然当前优先自托管，但架构中预留了以下扩展点，方便未来提供托管服务：

### 9.1 多租户隔离

当前设计已按 `platform + channel_id` 自然隔离：

```python
# 当前：按频道隔离（自托管够用）
class Subscription(Base):
    platform: str           # discord, telegram
    platform_channel_id: str  # 频道ID，天然隔离

# 未来扩展：添加租户层
class Tenant(Base):           # 新增
    id: int
    name: str
    plan: str                 # free, pro, enterprise
    quota_feeds: int
    quota_api_calls: int

class Subscription(Base):
    tenant_id: int | None     # 可选，托管模式时启用
    # ... 其他字段
```

### 9.2 配额与计费扩展点

```python
# services/quota.py

class QuotaService:
    """配额服务 - 自托管模式返回无限制"""

    async def check_feed_limit(self, channel_id: str) -> bool:
        if not settings.multi_tenant:
            return True  # 自托管无限制

        # 托管模式：检查配额
        count = await self.get_feed_count(channel_id)
        limit = await self.get_channel_limit(channel_id)
        return count < limit

    async def record_api_usage(self, channel_id: str, api: str):
        if not settings.multi_tenant:
            return  # 自托管不记录

        # 托管模式：记录用量用于计费
        await self.usage_repo.increment(channel_id, api)
```

### 9.3 数据库切换

代码使用 SQLAlchemy，天然支持切换：

```bash
# 自托管（SQLite）
DATABASE_URL=sqlite+aiosqlite:///./data/newsflow.db

# 托管服务（PostgreSQL）
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/newsflow
```

### 9.4 缓存切换

```python
# services/cache.py

def get_cache_backend() -> CacheBackend:
    if settings.cache_backend == "redis":
        return RedisCache(settings.redis_url)
    return MemoryCache()  # 默认内存缓存
```

### 9.5 未来托管服务架构（参考）

```
                    ┌─────────────────┐
                    │   Load Balancer │
                    └────────┬────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   ┌──────────┐        ┌──────────┐        ┌──────────┐
   │ NewsFlow │        │ NewsFlow │        │ NewsFlow │
   │ Instance │        │ Instance │        │ Instance │
   └────┬─────┘        └────┬─────┘        └────┬─────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
        ┌──────────┐               ┌──────────┐
        │ Postgres │               │  Redis   │
        │ (主库)   │               │ (缓存)   │
        └──────────┘               └──────────┘
```

---

## 十、技术决策记录

### 为什么选择「自托管优先」？

1. **降低运营成本**: 翻译API费用由用户承担
2. **规避法律风险**: 用户自己获取内容，你不参与分发
3. **开源社区价值**: 吸引贡献者，提升项目质量
4. **简化架构**: 无需复杂的多租户、计费系统

### 为什么选择 SQLite 作为默认数据库？

1. **零配置**: 无需安装额外服务
2. **性能足够**: 对于小于10万条目的场景性能优秀
3. **便于迁移**: 单文件，备份/迁移简单
4. **可升级**: 代码兼容PostgreSQL，需要时可无缝切换

### 为什么翻译默认关闭？

1. **降低门槛**: 用户无需立即配置翻译API
2. **成本透明**: 用户主动开启，明确知道需要API Key
3. **适应场景**: 部分用户只需英文源，无需翻译

### 为什么使用 Slash Commands 而非前缀命令？

1. **官方推荐**: Discord 官方推荐 Slash Commands
2. **用户体验**: 自动补全、参数提示
3. **权限控制**: 更细粒度的权限管理
4. **未来兼容**: 前缀命令可能被弃用

### 为什么 FastAPI 设为可选？

1. **自托管不必须**: 大多数用户通过Bot命令交互即可
2. **减少资源占用**: 不开启时无需额外端口
3. **按需启用**: 需要健康检查或外部集成时再开启

---

## 十一、预估工作量

| 阶段 | 复杂度 | 备注 |
|------|--------|------|
| Phase 1 | 中 | 主要是重构现有逻辑 |
| Phase 2 | 中 | Discord部分可复用现有代码 |
| Phase 3 | 低 | 翻译逻辑已有，主要添加缓存 |
| Phase 4 | 低 | FastAPI开发效率高 |
| Phase 5 | 低 | Docker配置相对标准化 |
| Phase 6 | 高 | Web界面工作量较大 |

---

## 附录：完整依赖列表

```toml
# pyproject.toml
[tool.poetry]
name = "newsflow-bot"
version = "0.1.0"
description = "Self-hosted RSS to Discord/Telegram bot with translation support"
authors = ["Your Name <you@example.com>"]
license = "MIT"
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.11"

# === 核心依赖（必须） ===
# 消息平台
"discord.py" = "^2.3.0"
python-telegram-bot = { extras = ["job-queue"], version = "^20.7" }

# RSS解析
feedparser = "^6.0.0"
aiohttp = "^3.9.0"

# 数据库
sqlalchemy = { extras = ["asyncio"], version = "^2.0.0" }
aiosqlite = "^0.19.0"
alembic = "^1.13.0"

# 配置
pydantic-settings = "^2.1.0"

# HTML处理
beautifulsoup4 = "^4.12.0"
lxml = "^5.0.0"

# 调度
apscheduler = "^3.10.0"

# 日志
structlog = "^24.1.0"

# === 可选依赖 ===
# API服务
fastapi = { version = "^0.109.0", optional = true }
uvicorn = { extras = ["standard"], version = "^0.27.0", optional = true }

# 翻译服务
google-cloud-translate = { version = "^3.14.0", optional = true }
deepl = { version = "^1.16.0", optional = true }
openai = { version = "^1.10.0", optional = true }

# 缓存
redis = { extras = ["hiredis"], version = "^5.0.0", optional = true }

# PostgreSQL
asyncpg = { version = "^0.29.0", optional = true }

[tool.poetry.extras]
api = ["fastapi", "uvicorn"]
translation-google = ["google-cloud-translate"]
translation-deepl = ["deepl"]
translation-openai = ["openai"]
translation = ["google-cloud-translate", "deepl"]
cache = ["redis"]
postgres = ["asyncpg"]
# 完整安装
all = [
    "fastapi", "uvicorn",
    "google-cloud-translate", "deepl", "openai",
    "redis", "asyncpg"
]

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.0"
pytest-asyncio = "^0.23.0"
pytest-cov = "^4.1.0"
ruff = "^0.1.0"
mypy = "^1.8.0"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"

[tool.poetry.scripts]
newsflow = "newsflow.main:cli"
```

### 安装命令

```bash
# 开发环境
git clone https://github.com/yourname/newsflow-bot.git
cd newsflow-bot
poetry install

# 生产环境 - 最小安装
pip install newsflow-bot

# 生产环境 - 带翻译
pip install newsflow-bot[translation-deepl]

# 生产环境 - 完整功能
pip install newsflow-bot[all]

# Docker（推荐）
docker pull yourname/newsflow-bot
docker run -d --env-file .env yourname/newsflow-bot
```
