# NewsFlow-Bot 详细指南

README 是"能跑起来"的最小路径；本文档是**部署运维 + 二次开发**的详细参考。

- **第 1-6 部分：使用 / 运维**（面向把它跑起来、配置、日常管理的用户）
- **第 7-14 部分：开发 / 架构**（面向想贡献代码或做深度二次开发的人）

---

## 📖 目录

### 使用 / 运维
1. [完整命令参考](#一完整命令参考)
2. [完整配置项](#二完整配置项)
3. [AI 功能详解（翻译 / 日报 / 提示词定制）](#三ai-功能详解)
4. [Webhook 推送](#四webhook-推送)
5. [OPML 导入导出](#五opml-导入导出)
6. [REST API](#六rest-api)
7. [高级部署与运维](#七高级部署与运维)

### 开发 / 架构
8. [项目定位与设计理念](#八项目定位与设计理念)
9. [技术栈](#九技术栈)
10. [架构总览](#十架构总览)
11. [关键设计决策与"为什么"](#十一关键设计决策与为什么)
12. [代码风格与约定](#十二代码风格与约定)
13. [开发环境搭建](#十三开发环境搭建)
14. [日常开发流程](#十四日常开发流程)
15. [常见陷阱 / FAQ](#十五常见陷阱--faq)
16. [贡献流程](#十六贡献流程)
17. [参考：项目文件速查](#十七参考项目文件速查)

---

## 一、完整命令参考

所有命令按"功能类别"分组。Discord 为 slash command（斜杠 + 自动补全），Telegram 为前缀命令（空格分隔参数）。

### 1.1 Discord 斜杠命令

**Feed 管理**

| 命令 | 说明 |
|---|---|
| `/feed add <url>` | 订阅；成功后几秒内自动推一条预览 |
| `/feed remove <url>` | 退订 |
| `/feed pause <url>` | 暂停（不删，可 resume） |
| `/feed resume <url>` | 恢复已暂停的订阅 |
| `/feed list [page]` | 分页列出本频道订阅（每页 20 条） |
| `/feed test <url>` | 不订阅，只测试 URL 是否是合法 feed |
| `/feed status <url>` | 单 feed 详情：健康、最近失败时间、最近 5 篇文章 |

**翻译设置**

| 命令 | 说明 |
|---|---|
| `/settings language <code>` | 设置本频道**所有订阅**的翻译目标语言 |
| `/settings translate <on\|off>` | 开关本频道**所有订阅**的翻译 |
| `/feed language <url> <code>` | **单条订阅**的语言（覆盖频道默认） |
| `/feed translate <url> <on\|off>` | **单条订阅**的翻译开关（覆盖频道默认） |

**关键词过滤**

| 命令 | 说明 |
|---|---|
| `/feed filter-set <url> include:<csv> exclude:<csv>` | 设置过滤（两个参数都可选；都空等于清除） |
| `/feed filter-show <url>` | 查看当前过滤规则 |
| `/feed filter-clear <url>` | 移除过滤 |

匹配行为：**title + summary 合并、大小写不敏感、子串匹配**。`include` 需要"至少包含一个"，`exclude` 需要"一个都不包含"，两者叠加评估。

**OPML**

| 命令 | 说明 |
|---|---|
| `/feed export` | 导出本频道订阅为 OPML 文件 |
| `/feed import <file>` | 上传 `.opml` / `.xml` 文件批量订阅（上限 1 MB） |

**AI 日报 / 周报**

| 命令 | 说明 |
|---|---|
| `/digest enable schedule:<daily\|weekly> hour_utc:<0-23> [weekday:<0-6>] [language:<code>] [include_filtered:<bool>] [max_articles:<n>]` | 启用或更新 |
| `/digest show` | 查看当前配置 |
| `/digest disable` | 关闭（配置保留） |
| `/digest now` | 立即生成并投递一次（测试用） |

**其他**

| 命令 | 说明 |
|---|---|
| `/status` | Bot 整体状态 |

### 1.2 Telegram 命令

**Feed 管理**

| 命令 | 说明 |
|---|---|
| `/add <url>` | 订阅 |
| `/remove <url>` | 退订 |
| `/pause <url>` | 暂停 |
| `/resume <url>` | 恢复 |
| `/list [page]` | 分页列出 |
| `/test <url>` | 测试 URL |
| `/info <url>` | 单 feed 详情 |

**翻译设置**

| 命令 | 说明 |
|---|---|
| `/language <code>` | 频道级翻译语言 |
| `/translate <on\|off>` | 频道级翻译开关 |
| `/setlang <url> <code>` | 单 feed 语言覆盖 |
| `/settrans <url> <on\|off>` | 单 feed 翻译开关覆盖 |

**关键词过滤**

| 命令 | 说明 |
|---|---|
| `/filter <url>` | 显示当前过滤规则 |
| `/filter <url> clear` | 移除过滤 |
| `/filter <url> include=a,b exclude=c,d` | 设置过滤（两字段都可选） |

**OPML**

| 命令 | 说明 |
|---|---|
| `/export` | 导出 OPML |
| `/import <url>` | 从 URL 获取 OPML 导入 |
| *上传 `.opml` 文件* | 直接拖进聊天自动导入，**无需命令** |

**AI 日报 / 周报**

| 命令 | 说明 |
|---|---|
| `/digest show` | 显示当前配置 |
| `/digest enable daily <hour_utc> [lang]` | 启用日报 |
| `/digest enable weekly <weekday> <hour_utc> [lang]` | 启用周报（weekday 支持 `mon…sun` 或 `0…6`） |
| `/digest disable` | 关闭 |
| `/digest now` | 立即投递一次 |

**其他**

| 命令 | 说明 |
|---|---|
| `/start`、`/help` | 帮助 |
| `/status` | Bot 状态 |

### 1.3 常用语言代码

`zh-CN`（简中）· `zh-TW`（繁中）· `en` · `ja` · `ko` · `fr` · `de` · `es` · `ru` · `ar` · 任何 BCP-47 代码（provider 支持的范围内）

---

## 二、完整配置项

所有配置通过 `.env` 文件或环境变量注入。最小启动只需一个 platform token。

### 2.1 必填

| 变量 | 说明 |
|---|---|
| `DISCORD_TOKEN` | Discord bot token（二选一，也可两者） |
| `TELEGRAM_TOKEN` | Telegram bot token |

### 2.2 数据库

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | 改成 `postgresql+asyncpg://user:pass@host/db` 即切 Postgres |

### 2.3 翻译

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TRANSLATION_ENABLED` | `false` | 总开关 |
| `TRANSLATION_PROVIDER` | `deepl` | `deepl` / `openai` / `google` |
| `DEEPL_API_KEY` | 空 | DeepL key |
| `OPENAI_API_KEY` | 空 | OpenAI 或兼容 API 的 key |
| `OPENAI_MODEL` | `gpt-5.4-nano` | OpenAI 模型名（翻译） |
| `OPENAI_BASE_URL` | 空 | OpenAI-compatible 端点（DeepSeek / Qwen / Kimi / 本地 Ollama 等） |
| `TRANSLATION_SYSTEM_PROMPT` | 空 | 翻译 prompt 覆盖（见 §3.3） |
| `GOOGLE_CREDENTIALS_PATH` | 空 | Google Cloud 服务账号 JSON 路径 |
| `GOOGLE_PROJECT_ID` | 空 | GCP 项目 ID |

### 2.4 调度

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FETCH_INTERVAL_MINUTES` | `60` | 抓取循环间隔 |
| `CLEANUP_INTERVAL_HOURS` | `24` | 清理循环间隔 |
| `ENTRY_RETENTION_DAYS` | `7` | 保留多少天的 FeedEntry |

### 2.5 缓存

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CACHE_BACKEND` | `memory` | `memory`（进程内 LRU）或 `redis` |
| `REDIS_URL` | 空 | 如 `redis://redis:6379/0` |
| `TRANSLATION_CACHE_TTL_DAYS` | `7` | 翻译结果缓存 TTL |

### 2.6 AI 日报 / 周报

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DIGEST_PROVIDER` | `openai` | 目前只支持 openai-compatible |
| `DIGEST_MODEL` | `gpt-5.4-mini` | 生成 digest 用的模型 |
| `DIGEST_MAX_ARTICLES` | `50` | 单次 digest 最多纳入的文章数 |
| `DIGEST_MAX_INPUT_CHARS_PER_ARTICLE` | `300` | 单篇文章喂给 LLM 时的字符上限 |
| `DIGEST_CHECK_INTERVAL_MINUTES` | `5` | 调度循环的检查间隔 |
| `DIGEST_SYSTEM_PROMPT` | 空 | 日报 prompt 覆盖（见 §3.3） |

### 2.7 REST API（可选）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_ENABLED` | `false` | 启用 FastAPI 管理端点 |
| `API_HOST` | `0.0.0.0` | 监听地址 |
| `API_PORT` | `8000` | 监听端口 |

### 2.8 日志

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console`（彩色）或 `json`（结构化） |

### 2.9 配额（0 = 无限制）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_FEEDS_PER_CHANNEL` | `0` | 单频道最多订阅数 |
| `MAX_ENTRIES_PER_FEED` | `0` | 单 feed 保留多少条 |

---

## 三、AI 功能详解

### 3.1 翻译

订阅时默认 `translate=True`，所以一旦全局 `TRANSLATION_ENABLED=true` 并配好 provider，新订阅会自动翻译标题和摘要到 `target_language`（默认 `zh-CN`）。

**两层缓存**：

```
请求翻译
   │
   ▼
检查 DB 缓存（FeedEntry.title_translated，按条目 + 语言）
   │ 命中 → 返回
   ▼ miss
检查服务缓存（memory 或 Redis，按文本 hash + 语言，TTL 7 天）
   │ 命中 → 返回，并回填到 DB 缓存
   ▼ miss
调用 Provider API → 两层缓存都写 → 返回
```

这意味着同一篇文章推给多个同语言订阅时，**只翻译一次**；重启服务后 DB 缓存仍在，只是内存缓存需重建。

### 3.2 AI 日报 / 周报

Digest 会按频道的配置周期性生成。**默认关闭**，需要通过命令（`/digest enable`）对每个频道单独启用。

**工作流程**：

```
每 DIGEST_CHECK_INTERVAL_MINUTES（默认 5）分钟唤醒
   │
   ▼
SELECT * FROM channel_digests WHERE enabled=True
   │
   ▼ 对每个 config 调 is_due(config, now)：
     - now.hour == delivery_hour_utc
     - 如果 weekly，weekday 也匹配
     - last_delivered_at 距今 ≥ 去重阈值（日 23h / 周 6d）
   │
   ▼ 命中的 config：
   │
   ▼
查询该频道在 (last_delivered_at, now] 窗口里推送过的 FeedEntry
（默认只算 was_filtered=False 的；include_filtered=True 时包括被过滤掉的）
   │
   ▼
窗口空 → mark_delivered 推进游标 → 下一个
   │
   ▼
窗口有文章 → 调 LLM 生成 Markdown → 按 Discord(1900 char) / Telegram(3800 char) 分片 → 发送 → mark_delivered
```

**成本估算**（以 gpt-5.4-mini 为例）：50 篇文章 × 300 chars ≈ 25k input + 2k output = **约 $0.028 / 次**；每日跑 = $0.84/月，每周跑 = $0.12/月。

**默认 prompt 产出**：3-5 个主题聚合、每主题 2-4 句、内联 `[N]` 引用、末尾完整链接列表、Markdown 格式。若需不同风格（技术向、学院派、段子手等）见下节。

### 3.3 自定义 AI 提示词

**目的**：在不改代码的情况下，调整翻译口吻或日报风格。通过环境变量覆盖。

#### 翻译 prompt

**默认值**（见 `src/newsflow/services/translation/openai.py::DEFAULT_TRANSLATION_PROMPT`）：

```
You are a professional translator. Translate the following text from
{source_desc} to {target_name}. Preserve the original meaning and tone.
Only output the translated text, nothing else.
```

**可用占位符**：

| 占位符 | 填入值 |
|---|---|
| `{source_desc}` | 源语言的英文名（如 `English`）；source 未指定时填 `the source language (auto-detect)` |
| `{target_name}` | 目标语言的英文名（如 `Simplified Chinese`） |

**覆盖方式**：`.env` 里设 `TRANSLATION_SYSTEM_PROMPT`。

**示例 —— 科技评论风**：

```bash
TRANSLATION_SYSTEM_PROMPT="You are translating a tech blog from {source_desc} to {target_name}. Keep code snippets, product names, and technical acronyms in the original language. Use natural, conversational tone in the target language. Output only the translation."
```

#### Digest prompt

**默认值**（见 `src/newsflow/services/summarization/openai.py::SYSTEM_PROMPT_TEMPLATE`）：一段"you are a news editor"模板，要求 3-5 主题聚合、不虚构事实、引用编号、Markdown 输出。

**可用占位符**：

| 占位符 | 填入值 |
|---|---|
| `{window}` | 时间窗口描述（如 `the past 24 hours`、`the past 7 days`） |
| `{lang}` | 目标语言英文名 |

**覆盖方式**：`.env` 里设 `DIGEST_SYSTEM_PROMPT`。多行字符串在 `.env` 中建议用单行 + `\n` 或者避开多行。

**示例 —— 学院派分析风**：

```bash
DIGEST_SYSTEM_PROMPT="You are a senior analyst writing an executive briefing in {lang}. Review articles from {window} and produce: (1) a two-sentence executive summary; (2) 3-5 themed sections with causal analysis; (3) an outlook paragraph identifying what to watch next. Cite each source with [N]. Avoid clickbait tone."
```

**示例 —— 极简速览风**：

```bash
DIGEST_SYSTEM_PROMPT="Produce a one-screen brief in {lang} covering the past {window}. Format: a single bulleted list, max 10 bullets, each under 30 words. End with numbered citations [N] → title."
```

#### 测试提示词

改完 `.env`、重启容器后，不必等到下一个交付时间点：

```
/digest now    # 立刻生成并投递当前 window 的 digest
```

直接看输出质量是否符合预期。不满意就再改、再测，直到满意。

#### 防错机制

- 占位符拼错（如 `{window}` 写成 `{windoow}`）会在运行时触发 KeyError，自动**回退到内置默认 prompt** 并记录 warning 日志
- 温度固定 `0.3`（保守，稳定）；`max_tokens` 为 2000，覆盖常规日报长度。若要改得更"跳脱"或"更长"，目前需要改代码 —— 可改天做成 env var 配置

### 3.4 Provider 支持度

| 功能 | 支持的 provider |
|---|---|
| **翻译** | DeepL、OpenAI（或任何 OpenAI-compatible：DeepSeek / Qwen / Kimi / OpenRouter / 本地 Ollama 等）、Google Cloud Translation |
| **Digest** | OpenAI-compatible（同上） |

通过 `OPENAI_BASE_URL` 指向任意 OpenAI-compatible 端点即可换 provider，无需改代码。

---

## 四、Webhook 推送

把 feed 更新推送到任意 HTTP 端点。一个声明式 YAML 文件搞定所有目的地，无需 bot 命令。

### 4.1 什么时候用

- 想发到 Slack / ntfy / 飞书（Lark）/ 企业微信，但不想为每个再写一个完整 adapter
- 想让 n8n / Zapier / 自己的后端接到 RSS 事件做二次处理（触发 CI、入库、转发等）
- 想给个人手机推送最新文章（ntfy 自托管或 ntfy.sh）

### 4.2 快速上手

1. 把 `samples/webhooks.example.yaml` 复制到 `data/webhooks.yaml`
2. 编辑文件，填你自己的目的地和订阅
3. 重启 bot；启动日志会打印 `Webhook: ✓ enabled`

删掉 `data/webhooks.yaml` 即可完全关闭该功能，不影响其他平台。

### 4.3 配置文件结构

```yaml
destinations:
  <name>:                            # 用户可读别名，订阅用它引用
    url: <http endpoint>
    format: generic | slack | ntfy | lark | wecom
    secret: <可选, HMAC-SHA256 key>
    headers:                         # 可选, 任意自定义 HTTP headers
      Authorization: "Bearer xxx"
    timeout_s: 10                    # 可选, 请求超时, 默认 10 s
    translate: true                  # 可选, 是否对此目的地启用翻译
    language: zh-CN                  # 可选, translate=true 时的目标语言

subscriptions:
  <name>:                            # 必须已在 destinations 里定义
    - <feed_url>
    - <feed_url>
```

**完整带注释的示例**：`samples/webhooks.example.yaml` —— Slack / ntfy / 飞书 / 企业微信 / n8n 五种目的地全覆盖。

### 4.4 Payload 格式

每种 `format` 生成不同的请求体，对应各自服务的 API 要求：

| format | Content-Type | 结构 |
|---|---|---|
| `generic` | `application/json` | NewsFlow 自定义 JSON（见下） |
| `slack` | `application/json` | Slack [Block Kit](https://api.slack.com/block-kit)，含 fallback text |
| `ntfy` | `text/plain` | body 是摘要；标题 / 点击链接 / 附图走 HTTP headers（`Title` / `Click` / `Attach`，非 ASCII 用 RFC 2047 编码） |
| `lark` | `application/json` | 飞书 / Lark post 卡片（`msg_type: "post"`） |
| `wecom` | `application/json` | 企业微信群机器人 markdown（`msgtype: "markdown"`） |

**generic 格式**（推荐给 n8n / Zapier / 自写端点）：

```json
{
  "event": "feed.entry.new",
  "timestamp": "2026-04-23T06:30:00+00:00",
  "entry": {
    "title": "原始标题",
    "title_translated": "翻译后标题（可能为 null）",
    "link": "https://example.com/article",
    "summary": "正文摘要纯文本（HTML 已 strip）",
    "summary_translated": "翻译后摘要（可能为 null）",
    "source": "源名称",
    "published_at": "2026-04-22T15:00:00+00:00",
    "image_url": "https://example.com/cover.jpg"
  }
}
```

系统通知（例如 feed 被自动禁用的告警）结构不同：

```json
{
  "event": "system.notification",
  "timestamp": "...",
  "text": "⚠️ The RSS feed ... has been auto-disabled ..."
}
```

### 4.5 HMAC 签名

destination 里设 `secret` 后，bot 会在每次请求加一个 header：

```
X-NewsFlow-Signature: sha256=<hex>
```

签名是 `HMAC-SHA256(secret, 请求 body 的原始字节)`。接收端用同一 secret 重算并比较，防止 URL 被截获后被任意第三方调用 —— 暴露在公网的 webhook endpoint 建议都启用。

Python 验签示例：

```python
import hmac, hashlib

def verify(body: bytes, header_value: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value)
```

**关键点**：验签用的 `body` 必须是**收到的原始字节**（例如 FastAPI 里 `await request.body()`），不能是 `json.loads` 之后再 `json.dumps` 的结果 —— 空格、键顺序、编码任一不同都会让 HMAC 不匹配。

### 4.6 数据模型与启动流程

- 表：`webhook_destinations`（`name` 唯一 / `url` / `format` / `secret` / `headers` JSON / `timeout_s`）
- 订阅：沿用现有 `Subscription` 表，`platform="webhook"`，`platform_channel_id=<destination name>`（不是 URL，避免 secret token 混进日志）
- 启动序列：`alembic upgrade head` → 若 `webhooks.yaml` 存在则 `sync_webhooks()` 对比并更新 → 启动各 adapter

**yaml → DB 协调规则**（见 `src/newsflow/services/webhook_sync.py`）：

1. yaml 里新增的 destination / 订阅会被 **插入**
2. yaml 里改动的 url / format / secret / headers / timeout 会被 **更新**
3. yaml 里删除的 destination 会被 **删除**（连带其所有订阅）
4. yaml 里第一次出现的 feed URL 会被 **自动 add_feed**（一次性网络抓取；失败则跳过并告警，不中断启动）
5. 不在 yaml 里的 webhook 订阅（但在 DB 里）会被 **删除**

所以工作流就是：改 yaml + 重启 bot。不用命令、不用 API。yaml 本身可以进 git，和订阅列表一起版本化管理。

### 4.7 故障排查

| 症状 | 可能原因 |
|---|---|
| 启动日志没看到 `Webhook: ✓ enabled` | `webhooks.yaml` 不在 `WEBHOOKS_CONFIG_PATH`（默认 `./data/webhooks.yaml`）；检查文件路径和权限 |
| `webhooks.yaml is invalid; aborting startup` | YAML 语法错或字段不符合 schema；报错信息里会点出具体位置 |
| 某 feed URL 没被订阅但没报错 | 首次自动 add_feed 失败（404 / 解析错 / SSRF 校验拒绝）；启动日志有 `webhook_sync: skipping <url>: <error>` |
| 接收方收到 HTTP 200 但内容显示成 raw JSON | `format` 选错；比如 Slack webhook 收到 `generic` 格式会直接显示 `{"event": ...}` 字符串 |
| 自己验签总是失败 | 见 4.5 "关键点" —— 必须用收到的原始字节，不是反序列化后的对象 |
| 飞书 / 企业微信 URL 里含签名参数，发不出去 | 把完整 URL（含 `?key=...` 或签名参数）原样贴进 yaml；bot 不会重组 URL |
| 改 yaml 后重启没变化 | 检查启动日志 `webhook_sync: N destination(s), M subscription(s)`；若数量不符，说明解析器没拿到最新文件 |

### 4.8 扩展一个新 format

比如要加 "Discord webhook"（Discord 有独立的 webhook URL，和 bot token 不同），在 `src/newsflow/adapters/webhook/formats.py` 加两个函数：

```python
def _to_discord(m: Message) -> WireRequest:
    return _json({
        "content": f"**{m.display_title}**\n{m.display_summary}\n{m.link}"
    })

def _to_discord_text(text: str) -> WireRequest:
    return _json({"content": text})
```

然后在文件尾的 `_ENTRY_CONVERTERS` 和 `_TEXT_CONVERTERS` 两个 dict 里各加一行 `"discord": _to_discord` / `"discord": _to_discord_text`。完事。不需要改 adapter 主体、sync 逻辑、model 或 migration —— 只是往 dispatch 表里加了一种选项。

---

## 五、OPML 导入导出

**用途**：从 Feedly / Reeder / NetNewsWire 搬家；备份订阅列表；在多个频道 / 实例间迁移。

### 4.1 导出

- Discord：`/feed export` → bot 返回 `.opml` 附件
- Telegram：`/export` → bot 以 document 形式发送

### 4.2 导入

- Discord：`/feed import file:<上传文件>`
- Telegram：直接拖 `.opml` 文件到聊天（无需命令）；或 `/import <url>` 从 URL 抓取

### 4.3 预置源列表

仓库 `samples/curated-feeds.opml` 提供 23 个精选源（WSJ、FT、NYT、Bloomberg、Economist、Reuters、Atlantic、Foreign Affairs、Nautilus、Longreads、Cloudflare Blog、EFF 等）。

**使用**：下载该文件到本地 → 用上面任一导入方式批量订阅。

> **批量导入不会刷屏** —— 不会一次推 23 条预览消息。导入走"安静流程"，文章在下一轮 dispatch 时正常发送。

---

## 六、REST API

`.env` 里设 `API_ENABLED=true` 启用。

> ⚠️ **当前 API 无认证 + CORS 允许所有来源**。仅建议内网 / 本机使用。公网暴露前请加 nginx 反代 + Basic Auth 或 API Key 中间件。

### 5.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务状态 |
| `GET` | `/ready` | 就绪检查（含 DB 连接） |
| `GET` | `/live` | 存活探针（K8s 友好） |
| `GET` | `/api/feeds` | 列全部 feed |
| `POST` | `/api/feeds` | 添加 feed |
| `GET` | `/api/feeds/{id}` | 单 feed 详情 |
| `DELETE` | `/api/feeds/{id}` | 删 feed |
| `POST` | `/api/feeds/{id}/refresh` | 强制刷新 |
| `POST` | `/api/feeds/test` | 测试 URL |
| `GET` | `/api/stats` | 总体统计 |
| `GET` | `/api/stats/feeds` | 每个 feed 的统计 |

`LOG_LEVEL=DEBUG` 时自动暴露 `/docs`（Swagger UI）。

---

## 七、高级部署与运维

### 6.1 Compose Profiles

```bash
# 加 Redis（多实例或想让翻译缓存跨重启）
docker compose -f docker/docker-compose.yml --profile with-redis up -d

# 加 Postgres（订阅超 10 万条再考虑；SQLite 之前都够用）
docker compose -f docker/docker-compose.yml --profile with-postgres up -d

# 全栈
docker compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres up -d
```

### 6.2 systemd + venv（无 Docker）

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

### 6.3 升级到新版本

```bash
cd ~/NewsFlow-Bot
git pull
docker compose -f docker/docker-compose.yml up -d --build
# alembic 迁移会在启动时自动跑，无需手工操作
```

### 6.4 备份

```bash
# SQLite 模式：单文件备份
docker cp newsflow-bot:/app/data/newsflow.db ./backup-$(date +%F).db

# Postgres 模式
docker compose -f docker/docker-compose.yml exec postgres pg_dump -U newsflow > backup-$(date +%F).sql
```

### 6.5 健康检查详解

Docker 镜像的 HEALTHCHECK 每 60s 扫描 `/app/data/heartbeat/` 目录 —— **任一文件超过 120 分钟未更新就算 unhealthy**。每个长期运行的任务各自负责刷新自己的文件：

| 文件 | 来源 | 刷新频率 |
|---|---|---|
| `dispatch` | `Dispatcher.dispatch_once` 末尾 | 每轮抓取完 |
| `cleanup` | `run_cleanup_loop` 每轮末尾 | 每 24h |
| `digest` | `run_digest_loop` 每轮末尾 | 每 5min |
| `discord` | `run_platform_monitor` 看到 `bot.is_ready()` | 每 30s |
| `telegram` | `run_platform_monitor` 看到 updater.running | 每 30s |

查看各任务 heartbeat 状态：

```bash
docker exec newsflow-bot ls -la /app/data/heartbeat/
```

---

## 八、项目定位与设计理念

### 7.1 目标场景

- **后端角色**：为 Discord / Telegram 机器人提供 RSS（或任何符合 RSS 格式的源）推送后端
- **部署形态**：Linux VPS 或任何 24/7 联网主机，Docker 容器或直接 systemd 托管
- **使用者画像**：自己的服务器、自己的 token、自己的翻译 API key —— 一切由用户拥有

### 7.2 核心设计原则

1. **自托管优先**。中心化 / 多租户版本会在另一个独立仓库做，本仓库不为假想的多租户场景引入抽象。
2. **零配置启动**。`.env` 里只填一个 `DISCORD_TOKEN` 或 `TELEGRAM_TOKEN` 就能跑起来。其余所有开关都有合理默认。
3. **渐进式复杂度**。翻译服务、REST API、Redis 缓存、Postgres 都是可选功能 —— 通过 `pyproject.toml` 的 extras 按需安装，代码里用懒 import 保证没安装对应包时也能启动。
4. **组件可替换**。平台适配器、翻译服务商、缓存后端都通过抽象基类 + 工厂切换，添加新的不需要改核心。
5. **数据属于用户**。默认 SQLite 单文件，`rsync data/` 就是全量备份。可选升级到 Postgres 也是一条连接串的事。

### 7.3 不做什么（反设计）

- ❌ 不在核心里内置多租户、配额、计费逻辑（那些属于未来的中心化版本）
- ❌ 不为"可能会用到"的扩展点提前抽象，YAGNI
- ❌ 不内置 UI / Web 管理界面 —— 管理通过 bot 命令或可选 REST API
- ❌ 不自己实现限流、重试、连接池等平台库已经处理的东西

---

## 九、技术栈

| 层 | 技术 | 选型理由 |
|---|---|---|
| 语言 | **Python 3.11 – 3.13** | 原生 async/await 成熟，生态丰富。3.14 暂不支持（`lxml` 尚无 wheel） |
| 包管理 | **Poetry**（源头） / **uv**（快 10×） | `pyproject.toml` 是权威，`[tool.poetry.dependencies]` 定义依赖；uv 读同一份文件可替代 poetry |
| 数据库 | **SQLAlchemy 2.0 asyncio** + SQLite / Postgres | 默认 `aiosqlite` 零配置；改连接串即可换 `asyncpg` |
| 迁移 | **Alembic** 1.13+ | 模型变更由 alembic 管，启动时自动 `upgrade head` |
| 配置 | **pydantic-settings** | 环境变量 + `.env` + 类型校验 + 合理默认 |
| RSS 抓取 | **feedparser** + **aiohttp** | feedparser 行业标准；aiohttp 提供异步 HTTP 和条件请求 |
| HTML 清洗 | **BeautifulSoup4** + **lxml** | 处理 summary/content 里的富文本 |
| 调度 | 纯 **asyncio loop**（不用 APScheduler） | `asyncio.sleep(interval)` 驱动，逻辑清晰、无额外线程 |
| Discord | **discord.py** 2.3+ | 官方推荐 slash commands；内部自带 HTTP 桶限流 |
| Telegram | **python-telegram-bot** 20.7+，启用 `rate-limiter` extra | 使用 `AIORateLimiter` 处理 30/s 全局、1/s 每 chat、20/min 每 group 限流 |
| 可选 API | **FastAPI** + **Uvicorn** | 健康检查 + REST 管理接口 |
| 翻译 | DeepL / OpenAI-compatible / Google Cloud Translation | 抽象 `TranslationProvider`，工厂按 `TRANSLATION_PROVIDER` 切换 |
| 缓存 | 内存 LRU（默认） / Redis（可选） | `CacheBackend` 抽象；Redis 适合未来多实例，但本仓库定位单实例 |
| 日志 | **structlog** | 结构化日志，JSON 或 console renderer 可切 |

---

## 十、架构总览

### 9.1 分层

```
┌─────────────────────────────────────────────────────────┐
│  adapters/               api/                           │
│  ├ discord/bot.py        └ routes/*.py                  │
│  └ telegram/bot.py                                      │
│     ────────────  平台 I/O / REST 入口  ─────────       │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  services/                                              │
│  ├ dispatcher.py        ← 中央调度 + cleanup + digest   │
│  ├ feed_service.py      ← 抓取 + 存储业务逻辑           │
│  ├ subscription_service.py ← 订阅 / 过滤 / OPML / 翻译设置 │
│  ├ digest_service.py    ← 周期 AI 日报/周报生成          │
│  ├ cache.py             ← 内存 / Redis 抽象              │
│  ├ translation/         ← 翻译 provider + 工厂 + 缓存    │
│  └ summarization/       ← Digest LLM provider + 工厂     │
│     ────────────  业务逻辑（fat layer） ─────────       │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  repositories/                                          │
│  ├ feed_repository.py       ← Feed / FeedEntry 查询     │
│  ├ subscription_repository.py ← Subscription / SentEntry │
│  └ digest_repository.py     ← ChannelDigest CRUD +      │
│                               时间窗内文章聚合查询         │
│     ────────────  数据访问  ─────────                   │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  models/                                                │
│  ├ base.py (Base + engine + session_factory)            │
│  ├ feed.py (Feed, FeedEntry)                            │
│  ├ subscription.py (Subscription, SentEntry)            │
│  ├ digest.py (ChannelDigest)                            │
│  └ migrate.py (alembic upgrade 封装)                    │
│     ────────────  ORM 模型  ─────────                   │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  core/                                                  │
│  ├ feed_fetcher.py      ← HTTP 抓取 + 条件请求 + 限流   │
│  ├ content_processor.py ← HTML 清洗 + 源名映射          │
│  ├ url_security.py      ← SSRF 防护                     │
│  ├ filter.py            ← FilterRule + 关键词匹配        │
│  ├ opml.py              ← OPML parse / build             │
│  └ timeutil.py          ← 相对时间 / 倒计时格式化        │
│     ────────────  无状态原语  ─────────                 │
└─────────────────────────────────────────────────────────┘
```

**分层约定（硬约束，别破坏）**：

- `adapters/` 和 `api/` 不允许直接访问 `repositories/` 和 `models/` —— 必须通过 `services/`。
- `services/` 不直接调用 `adapters/` —— 走 `Dispatcher` 的 `MessageSender` 注册机制。
- `core/` 不依赖任何上层，必须无状态。
- 循环依赖会被 mypy/ruff 或 import 顺序直接拒绝。

### 9.2 启动拓扑（main.py）

`main.py::main()` 依次做这些事：

1. 读配置（`get_settings()`）、校验至少有一个 platform token
2. 建日志（structlog + JSON/console 可选）
3. 创建 `data/` 目录
4. 跑 alembic `upgrade head`（在 worker 线程里，避免和主 asyncio loop 冲突）
5. 初始化 cache（memory 或 redis）
6. 装 SIGTERM / SIGINT 信号处理
7. **并发启动**以下任务：
   - `start_discord_bot()`（如果启用）
   - `start_telegram_bot()`（如果启用）
   - `start_api_server()`（如果启用）
   - `start_dispatch_loop()` —— 中央调度
   - `start_cleanup_loop()` —— 清理过期条目 / 发送记录
   - `start_platform_monitor()` —— 每 30s 检查 adapter 连通性，写 heartbeat
   - `start_digest_loop()` —— 周期性 AI 日报/周报生成与投递

各任务是独立的 `asyncio.Task`，任何一个挂掉不影响其他。每个任务都在 `data/heartbeat/<name>` 维护自己的 heartbeat 文件，供容器 HEALTHCHECK 判定存活。

### 9.3 Dispatch 数据流

```
定时触发（run_dispatch_loop 内的 asyncio.sleep）
   │
   ▼
等待 adapters 全部注册好（_ready_event，首次启动有 60s 超时）
   │
   ▼
dispatch_once():
   ├─▶ FeedService.fetch_all_feeds()
   │   ├─ 从 DB 拿"待 fetch" 的 feed（已过 next_retry_at 且 is_active）
   │   ├─ fetch_multiple() 并发抓取（信号量限流 max_concurrent=10）
   │   └─ 串行 _apply_fetch_result() 写入 DB（AsyncSession 不能并发）
   │
   ├─▶ SubscriptionRepository.get_all_active_subscriptions()
   │
   ├─▶ for each subscription:
   │   ├─ get_unsent_entries_for_subscription()（用 SentEntry 去重）
   │   ├─ 每条 entry：
   │   │   ├─ 按订阅语言走翻译（两层缓存：DB cache → memory/Redis → provider）
   │   │   ├─ adapter.send_message()（平台库内部限流）
   │   │   └─ 成功 → mark_entry_sent() 插入 SentEntry
   │   │   └─ 失败 → 不标记，下轮重试
   │   └─ 每条间 smoothing sleep 0.1s
   │
   ├─▶ commit session
   └─▶ touch data/.heartbeat（HEALTHCHECK 用）
```

---

## 十一、关键设计决策与"为什么"

读代码时可能会想"为什么这么写"——以下是那些不自明的选择：

### 10.1 为什么新订阅要 seed SentEntry？

`SubscriptionService.subscribe()` 创建新订阅成功后，会调用
`SubscriptionRepository.seed_sent_entries()`，把该 feed 当前所有 `FeedEntry`
预先插入 `SentEntry` 表。

**目的**：避免新订阅的频道被 feed 的历史文章（可能几十上百条）瞬间淹没。
订阅时点之后才到达的新条目才会被推送。

如果想保留"订阅后推送最近 N 条"作为预览体验，把 seed 改成"跳过最新 N 条"
即可 —— 扩展点留得很窄。

### 10.2 为什么 dispatcher 要等 adapters 就绪？

Discord/Telegram bot 是和 dispatch loop 并发启动的 asyncio 任务。bot
需要先连上平台才能 `register_adapter` 到 dispatcher。如果第一轮 dispatch
在 bot 连上之前触发，`self._adapters.get("discord")` 返回 None，该轮发给
Discord 的消息全部静默跳过 —— 用户要等整整一个 `FETCH_INTERVAL_MINUTES`
才看得到第一条消息。

所以 `Dispatcher.__init__` 按 `settings.discord_enabled` /
`telegram_enabled` 组建 `_expected_platforms` 集合；`register_adapter`
清集合里对应项；集合空了就 `set()` `_ready_event`；`run_dispatch_loop`
首轮前 `await wait_for_adapters(timeout=60)`。

超时不会抛错 —— 某个平台挂掉不应该拖垮其他平台的 dispatch。

### 10.3 为什么 HTTP 抓取并发、DB 写入串行？

`FeedService.fetch_all_feeds` 对 N 个 feed 的处理分两阶段：

- 第一阶段 `fetcher.fetch_multiple()`：`asyncio.gather` + 信号量控制
  （`max_concurrent=10`），HTTP 真正并行，不会因为一个慢源拖累整体
- 第二阶段 `for feed, fr in zip(...): await _apply_fetch_result(feed, fr)`：
  串行写 DB

后者是硬约束：**SQLAlchemy 的 AsyncSession 不是线程/任务安全的**，同一个
session 只能同时有一个 await 操作。如果想并发写，必须每个任务开独立
session —— 但那样 feed 间的事务隔离、锁、连接池压力都要重新设计，
得不偿失。

### 10.4 为什么 alembic 初始 migration 有 `has_table` 早返回？

`alembic/versions/20260420_1624_9ef238497e99_initial_schema.py` 的
`upgrade()` 函数开头：

```python
bind = op.get_bind()
if sa.inspect(bind).has_table("feeds"):
    return
```

**目的**：让老的、从 `init_db()`（`metadata.create_all`）bootstrap 出来的
DB 能无痛迁移到 alembic 管理。没这段的话，已有 `feeds` 表的 DB 跑第一次
`alembic upgrade` 会因为 `CREATE TABLE feeds` 重复报错。有了早返回，
alembic 发现啥都没做，但会把当前 revision 记录到 `alembic_version` 表
—— 等效于自动 "stamp head"。之后的 migration 从这个基线线性推进。

### 10.5 为什么 `settings/`、`cache/` 等是进程级单例？

`get_settings()`（`@lru_cache`）、`get_dispatcher()`、`get_fetcher()`、
`get_translation_service()`、`get_cache()`、`get_engine()` 等都是懒初始化
单例。这不是 DI 洁癖，而是因为：

- 本项目是**单进程单实例**部署。没有多租户，没有多 event loop。
- 配置、HTTP client、数据库引擎等资源是全局共享的，没必要每次注入。
- 测试里如果需要隔离，通过 `monkeypatch.setattr` 或 `patch("module.get_xxx")` 即可。
- 写得直接，入门读者更容易懂。

### 10.6 为什么 discord.py 不需要自己限流而 PTB 需要？

- `discord.py` 在 HTTPClient 层面自动跟踪每个 bucket 的 rate limit header
  并在 429 时 `await` 等待。调用方（我们）直接 `await channel.send(...)`
  就行。
- `python-telegram-bot` v20 需要显式 `ApplicationBuilder().rate_limiter(AIORateLimiter())`
  才启用限流，默认裸调 API 不限速，容易被 429。

所以 `adapters/telegram/bot.py` 里启用 `AIORateLimiter`，`adapters/discord/bot.py`
什么都不用做。`dispatcher` 里那个 `asyncio.sleep(0.1)` 是小 smoothing buffer，
不是主要限流手段。

### 10.7 为什么翻译要两层缓存？

`TranslationService.translate` 的缓存顺序：

```
请求 → 检查 memory/Redis 缓存 → 命中返回
                  │
                  ↓ miss
         调用 provider API → 写缓存 → 返回
```

再往上一层，`FeedEntry` 表有 `title_translated` / `summary_translated`
字段，是"这条 entry 在目标语言的永久结果"。Dispatcher 的 `_translate_entry`
优先读这个字段，miss 才走 `TranslationService`。

- 服务缓存（内存/Redis）：**短 TTL，按文本哈希**。减少 API 调用。
- DB 缓存（FeedEntry 字段）：**永久，按 entry+语言**。同一条 entry 推给
  多个同语言订阅只翻一次。

加翻译相关代码时，保持这个"DB → 服务 → provider"的查询顺序。

### 10.8 为什么没有 APScheduler？

历史上引入过 `core/scheduler.py` + APScheduler，但实际上周期性工作全是
`while True: await asyncio.sleep(N)` 形式的简单循环。APScheduler 带来的
job 持久化、cron 语法、线程池等能力都用不上 —— 删了，简化心智模型。

如果未来要加动态间隔、cron 表达式、持久化调度，可以把 APScheduler 重新
引回来；但别为了"以后可能用到"保留一块不跑的代码。

### 10.9 为什么过滤要在 dispatch 时做而不是 ingest 时？

`Subscription.filter_rule`（JSON 存 `FilterRule`）在**每轮 dispatch** 时
针对每篇待推条目评估。不在抓取/入库时过滤。

理由：
- 规则可能随时变（用户一会儿加 `include`、一会儿清），ingest 时过滤等于冻结历史 —— 用户改了规则，历史条目没法回溯
- 同一个 Feed 可能被多个频道订阅，各自规则不同 —— 入库一次、dispatch 时各自评估最合理
- 过滤评估是纯字符串子串匹配，~微秒级，对 dispatch 循环几乎零开销

被过滤掉的条目仍会写 `SentEntry{was_filtered=True}`，**防止下一轮重新评估同一条**。
`get_unsent_entries_for_subscription` 的 `NOT IN SentEntry` 过滤把两类都挡在外。
统计时可以区分 "实际推送" vs "被过滤"。

### 10.10 为什么 Digest 看原文而不是翻译缓存？

`DigestService.generate` 读 `FeedEntry.title` / `.summary` / `.content`
（原文），**不读** `title_translated` / `summary_translated`（翻译缓存）。

理由：
- **避免双层误差**：先翻译再摘要 = 翻译误差 + 摘要误差叠加；直接让 LLM 从原文跨语言摘要更准
- **现代 LLM 跨语言能力足够**：`gpt-5.4-mini` 级别"读英文→出中文"质量远高于"读机翻中文→出中文"
- **一致性**：不管订阅是否开了翻译，digest 流程都走同一条路径

Prompt 里对输出语言直接下命令（`"...in {language}..."`），LLM 按 `ChannelDigest.language`
产出目标语言的摘要。

### 10.11 为什么 Digest 循环 5 分钟轮询一次？

`run_digest_loop` 每 `DIGEST_CHECK_INTERVAL_MINUTES`（默认 5）唤醒，
对每个 `ChannelDigest` 调 `is_due(config, now)`：

- 小时等于 `delivery_hour_utc`
- 如果 `weekly`，`weekday` 也要匹配
- `last_delivered_at` 距今 ≥ 去重阈值（日 23h / 周 6d）

选 5 分钟而不是 1 小时：loop 自己开销几乎为零（一次 `SELECT WHERE enabled=True`），
但 5min 粒度让配置改动能较快生效（比如用户刚 `/digest enable hour_utc:9`，
9:00 UTC 前 5 分钟内生效）。

### 10.12 为什么 digest 空窗口也 mark_delivered？

如果时间窗内一篇文章都没有（频道订阅的源那段时间全没更新），`DigestService.generate`
返回 `None`。但我们仍然调用 `mark_delivered` 推进 `last_delivered_at`。

理由：如果不推进，下一次 loop 唤醒（5 分钟后）仍在同一 hour slot，`is_due` 仍然返回
True，会一直尝试生成→返回 None→不推进，浪费 CPU。推进之后下一次就得等到下一个
交付时间点。

### 10.13 为什么有 SSRF 校验而不是纯靠网络边界？

`core/url_security.validate_feed_url` 在 feed URL 进入 fetcher 前拦截：

- 非 http/https scheme（阻止 `file://` 读本地、`gopher://` 打内网）
- IP 字面量落在 private / loopback / link-local / reserved 段
  （阻止 `127.0.0.1`、`10.0.0.1`、`169.254.169.254` 云元数据端点）
- URL 长度上限

**没覆盖**：主机名在 fetch 时解析到内网 IP（DNS rebinding 或恶意 DNS）。
防这类需要自定义 aiohttp connector 做 IP pinning，复杂度高。目前靠容器
egress 策略 / VPS 网络边界作为第二层防御。

如果你在公网直连的 VPS 上跑 bot，又想彻底防 DNS SSRF，建议：

- 容器 `--network` 限制到没有内网路由的网络命名空间
- 或者在 firewall / iptables 层拦截到私网段的出站

---

## 十二、代码风格与约定

### 11.1 工具配置

```toml
# pyproject.toml
[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP"]
ignore = ["E501"]     # 宽松对待长行（100 char 是建议而非硬线）

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_ignores = true
disallow_untyped_defs = true   # 所有函数必须标注类型
```

`make lint` = `ruff check src/`；`make format` = `ruff format + ruff check --fix`；
`make typecheck` = `mypy src/`。提 PR 前三者都要过。

### 11.2 类型注解

- **所有函数必须加类型**。`disallow_untyped_defs = true` 强制。
- 使用 PEP 604 语法：`str | None` 而不是 `Optional[str]`，`list[X]` 而不是 `List[X]`。
- SQLAlchemy 模型用 `Mapped[T]` / `mapped_column(...)`，不要写老式 `Column`。

### 11.3 注释

- **默认不写**。命名已经说明 "做什么"，注释只说 "为什么"。
- 写注释的正确场景：
  - 非平凡的算法选择（e.g. "为什么 HTTP 并发但 DB 串行"）
  - 历史决策（e.g. "这里不用 APScheduler 是因为..."）
  - 外部约定（e.g. "discord.py 在这一层已经限流，我们不要重复"）
  - 文件系统 / 协议的非显然行为
- 不写：
  - 描述代码"是什么"的（`# increment counter`）
  - 指向任务 / PR / 修复的（`# fixed in #123` —— 放 PR 描述里）
  - 临时调试遗物

### 11.4 异常处理

- **边界捕获**：网络 I/O、第三方 API、用户输入、文件系统操作 —— 这些地方主动 try/except，并返回结构化错误（`FetchResult(success=False, error=str(e))`）给调用方。
- **内部信任**：本项目自己的函数之间不互相做防御性校验。类型系统 + 单元测试兜底。
- **不要 swallow**：`except Exception: pass` 只在两类情况允许：
  1. heartbeat 之类真正"失败也无所谓"的最佳努力操作
  2. 顶层事件循环防止一个任务死整棵树
  其他地方至少要 `logger.exception(...)`。

### 11.5 日志

- 用 `logger = logging.getLogger(__name__)`，不要 `print`。
- 级别约定：
  - `DEBUG`：冗余调试（cache hit、skipped not_modified、...）
  - `INFO`：正常事件（dispatch 完成、订阅添加、adapter 注册、...）
  - `WARNING`：可恢复问题（fetch 失败、channel 权限不足、...）
  - `ERROR` / `exception()`：未预期异常

### 11.6 配置

新增配置项流程：

1. 在 `src/newsflow/config.py::Settings` 加字段，**必须有类型和默认值**
2. 在 `.env.example` 加示范，带注释说明作用
3. 如果只有特定部署需要，加到 `Hosting-service extensions` 区段（注明自托管可忽略）
4. 在代码里访问：`get_settings().your_field`

不要再新增全局 `os.environ` 读取 —— 所有配置必须过 `Settings`。

### 11.7 依赖管理

- **核心依赖**（`[tool.poetry.dependencies]`）：没了就启动不了的必填项。
- **可选 extras**（`[tool.poetry.extras]`）：按功能分组，`translation-deepl`、`api`、`cache`、`postgres`、`all` 等。
- **懒 import**：如果一个模块需要可选依赖，import 必须放在函数内部，而不是模块顶部：

```python
# bad — import 失败会让整个模块不可用
from openai import AsyncOpenAI

# good — 只在真正需要时报错
def _get_client(self):
    from openai import AsyncOpenAI   # ← 懒 import
    return AsyncOpenAI(...)
```

这是为什么最简安装不需要装翻译 SDK / Redis / FastAPI 也能启动。

---

## 十三、开发环境搭建

### 12.1 安装

**Poetry 路线**（原始方式）：

```bash
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
poetry install --all-extras
cp .env.example .env   # 填 token
poetry run python -m newsflow.main
```

**uv 路线**（快得多）：

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio
cp .env.example .env
.venv/bin/python -m newsflow.main     # Windows: .venv\Scripts\python.exe ...
```

> **Python 版本**：3.11 – 3.13。3.14 暂不能用，因为 `lxml` 还没发布 3.14 的
> 预编译 wheel，Windows 上从源码编译需要 libxml2 开发头。等 lxml 上游发
> wheel 后自动可用。

### 12.2 Make 常用命令

| 命令 | 说明 |
|---|---|
| `make dev` / `make run` | 启动 bot（开发模式）|
| `make test` | 跑所有测试 |
| `make test-cov` | 跑测试 + HTML 覆盖率报告 |
| `make lint` | ruff check |
| `make format` | ruff format + autofix |
| `make typecheck` | mypy |
| `make db-upgrade` | 手动运行迁移（main.py 启动时也会自动跑）|
| `make db-migrate msg="..."` | 从模型变更 autogenerate 一个新 migration |
| `make db-stamp` | 把现有 DB 标记为最新，不跑任何 migration（老 DB 接入 alembic 时用）|
| `make docker-up` | docker-compose up -d |

### 12.3 跑单个测试

```bash
poetry run pytest tests/unit/test_feed_service.py::test_apply_fetch_result_stores_new_entries -v
```

---

## 十四、日常开发流程

### 13.1 改代码的标准流程

```
1. 拉分支                       git checkout -b feat/your-thing
2. 改代码
3. 加 / 改测试                  tests/unit/test_yourthing.py
4. make format && make lint     过不了别提 PR
5. make typecheck               同上
6. make test                    必须全绿
7. （可选）实际启动 bot验证      make dev
8. 提 commit，PR
```

### 13.2 修改数据库 schema

1. 改 `src/newsflow/models/{feed,subscription,base}.py`
2. 自动生成 migration：`make db-migrate msg="add feed.foo column"`
3. **人工 review 生成的 migration 文件**（`alembic/versions/<timestamp>_<hash>_add_feed_foo_column.py`）
   - 确认 `upgrade()` / `downgrade()` 都合理
   - SQLite 的 ALTER 依赖 `render_as_batch=True`（已在 env.py 配好）
   - 如果生成的 diff 意外（比如意外 drop 了列），检查是不是模型改错了
4. 本地跑一次 `make db-upgrade`（或让 main.py 启动时自动跑）验证
5. 加相应的测试（如果是行为变更）
6. commit 一起（migration + model）

### 13.3 添加一个新的平台 adapter（例：Matrix / 企业微信 / Slack）

步骤在代码里体现为：

1. 建 `src/newsflow/adapters/<platform>/bot.py`
2. 实现 `BaseAdapter` 抽象类（见 `adapters/base.py`）：`platform_name` / `start` / `stop` / `send_message` / `send_text`
3. 在 `Settings` 里加 `<platform>_token` 字段 + `<platform>_enabled` 属性
4. `main.py` 里加 `start_<platform>_bot()` 函数（模仿 `start_discord_bot`），`main()` 里按开关追加任务
5. `Dispatcher.__init__` 里把平台加入 `_expected_platforms`（这样 `wait_for_adapters` 会等它）
6. bot 的 start 流程结束后调用 `dispatcher.register_adapter("<platform>", adapter_instance)`
7. adapter 实例的 `send_message` 里处理该平台的消息格式化和实际 API 调用
8. 加测试：可以 mock `BaseAdapter` 的具体实现，测 dispatcher 能正确路由

### 13.4 添加一个新的翻译 provider（例：Anthropic / 通义千问）

1. 建 `src/newsflow/services/translation/<provider>.py`
2. 实现 `TranslationProvider` 抽象类（`translation/base.py`）
3. 在 `services/translation/factory.py::create_translation_provider` 里加分支，**用懒 import**
4. 在 `pyproject.toml` 加可选 extras（`translation-xxx = ["xxx-sdk"]`）
5. `Settings` 加 `xxx_api_key` / `xxx_model` 等字段
6. `config.py::get_translation_api_key()` 加对应分支
7. `.env.example` 加示范
8. 测试：mock `provider.translate` 返回值，验证 `TranslationService` 的缓存和 fallback 逻辑

### 13.5 添加一个新的 API 端点

1. 建 / 改 `src/newsflow/api/routes/<resource>.py`
2. 用 `APIRouter()`，写 Pydantic Request/Response 模型
3. 通过 `Depends(get_db)` 注入 session，调用 `services/` 层
4. 在 `src/newsflow/api/__init__.py::create_app()` 里 `app.include_router(your.router, prefix="/api/<resource>", tags=["..."])`
5. 注意：当前 **API 没有认证**。如果你的端点有写入副作用，把这条加到 issue 列表：中心化之前一定要加 auth + rate-limit。

### 13.6 加测试

- **位置**：`tests/unit/` 里，按被测模块命名 `test_<module>.py`
- **async**：`pyproject.toml` 里 `asyncio_mode = "auto"`，async 函数自动变成异步测试
- **DB fixture**：`tests/conftest.py::session`，每个测试一个内存 SQLite engine
- **隔离单例**：需要时用 `patch("newsflow.services.dispatcher.get_settings", return_value=MagicMock(...))`
- **命名原则**：`test_<function>_<scenario>`（`test_seed_sent_entries_marks_all_existing`）

推荐的测试层次：

- Repository 层：直接用 `session` fixture，不 mock。验证 SQL 行为。
- Service 层：给 service 传 session，mock 外部 I/O（fetcher、translation、adapter）。
- Adapter / Route：mock 整个 service，验证路由/命令解析。

不要给私有函数（`_foo`）写单独测试，除非它独立承担核心逻辑（e.g. `_apply_fetch_result`、`_write_heartbeat`）。

---

## 十五、常见陷阱 / FAQ

### 14.1 "第一次加了订阅但没收到消息"

**预期行为**。新订阅会 seed 当前 feed 所有历史条目为"已发"，只推送订阅之后新到达的。等源发表新文章 + 一轮 fetch 间隔才会收到第一条消息。

如果想手动触发一轮测试：

- API 启用时调 `POST /api/feeds/<id>/refresh`
- 或者直接改 `FETCH_INTERVAL_MINUTES=1` 启动测试

### 14.2 "Windows 上 `make dev` 崩了"

`loop.add_signal_handler(SIGINT, ...)` 在 Windows 的 asyncio 上抛
`NotImplementedError`。这是 Python 的已知限制。

解决办法（待修）：用 try/except 包一下，Windows 上 fallback 到默认 KeyboardInterrupt。
PR welcome。目前 Windows 开发建议跑测试验证，实际运行用 Docker / Linux VPS。

### 14.3 "AsyncSession 不能 gather"

```python
# ❌ 报错 / 数据损坏
results = await asyncio.gather(*[repo.do_something(session, x) for x in items])
```

一个 `AsyncSession` 同一时间只能有一个 `await` 飞行中的操作。要并发，
要么每个任务开自己的 session，要么把 I/O（HTTP 等）先 gather 完再串行写 DB
（这是 `FeedService.fetch_all_feeds` 的做法）。

### 14.4 "alembic revision 生成了意外 diff"

通常是以下之一：

- 某个模型文件没 import 到 `alembic/env.py`（本项目已显式 import `feed` 和 `subscription`，加新模型文件记得在 env.py 里 import）
- 数据库的实际状态和 alembic 认知不一致（比如手工改过 schema）—— 跑 `make db-stamp` 重新同步
- 模型字段默认值 / 可空性不一致 —— 读 diff 手动修正

### 14.5 "我要删一条 feed 的所有数据"

直接删 `Feed` 行。`FeedEntry` 和 `Subscription` 有 `ondelete="CASCADE"`，
`SentEntry` 又 cascade 自 `FeedEntry` / `Subscription`。一条 SQL 清到底。

### 14.6 "翻译结果怎么强制刷新？"

清 DB 里 `FeedEntry.title_translated` / `summary_translated` /
`translation_language` 为 `NULL`，同时清服务缓存（Redis `FLUSHDB` 或内存重启）。
下轮 dispatch 会重翻。

### 14.7 "services/cache.py 和 services/cache/ 能共存吗？"

**不行，千万别同时有**。Python 包目录会优先于同名 `.py` 文件，空目录
`cache/__init__.py` 会遮蔽 `cache.py` 导致所有导入失败。本项目的
`services/cache/` 目录已经在清理中删除。如果你想把 cache 拆成子模块，
要么完全换成 package 形式（把 `cache.py` 内容挪到 `cache/__init__.py` 或
拆分到 `cache/memory.py`、`cache/redis.py`），要么保留单文件。

---

## 十六、贡献流程

1. **Issue 先行**：非 trivial 改动先开 issue 讨论方向，避免白干。
2. **小 PR**：一个 PR 一件事。三件事分三个 PR。
3. **描述**：PR 描述写清楚 **为什么** 和 **改动要点**，而不是罗列 commit。
4. **测试必须过**：`make lint && make typecheck && make test` 全绿。
5. **新行为要有测试**：fix 要有回归测试；feature 要有正向测试。
6. **文档同步**：加了新配置项、新命令、新 extras 要同步改 `.env.example`、
   `README.md`（或 `README_EN.md`）、本文档。

### Commit message

推荐 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

`type` 常用：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf`。
`scope` 可选，比如 `dispatcher`、`telegram`、`alembic`。

---

## 十七、参考：项目文件速查

```
NewsFlow-Bot/
├── alembic/                      # 迁移脚本 + env.py
├── docker/                       # Dockerfile + compose
├── samples/                      # 预置 OPML（curated-feeds.opml）等
├── src/newsflow/
│   ├── main.py                   # 启动入口
│   ├── config.py                 # Settings（pydantic-settings）
│   ├── core/                     # 无状态原语
│   │   ├── feed_fetcher.py       # HTTP + feedparser + 条件请求
│   │   ├── content_processor.py  # HTML 清洗、源名映射
│   │   ├── url_security.py       # SSRF 校验
│   │   ├── filter.py             # FilterRule 关键词匹配
│   │   ├── opml.py               # OPML 解析 / 生成
│   │   └── timeutil.py           # 相对时间格式化
│   ├── models/                   # SQLAlchemy ORM
│   │   ├── base.py               # Base + engine + FK pragma
│   │   ├── feed.py               # Feed / FeedEntry
│   │   ├── subscription.py       # Subscription / SentEntry
│   │   ├── digest.py             # ChannelDigest
│   │   └── migrate.py            # alembic upgrade 封装
│   ├── repositories/             # DB 查询
│   │   ├── feed_repository.py
│   │   ├── subscription_repository.py
│   │   └── digest_repository.py
│   ├── services/                 # 业务逻辑
│   │   ├── dispatcher.py         # ★ 中央循环 + cleanup + digest + monitor
│   │   ├── feed_service.py
│   │   ├── subscription_service.py   # 订阅 / 过滤 / OPML / 翻译配置
│   │   ├── digest_service.py         # AI 日报/周报生成
│   │   ├── cache.py                  # 内存 / Redis 抽象
│   │   ├── translation/              # 翻译 provider + 工厂
│   │   └── summarization/            # Digest LLM provider + 工厂
│   ├── adapters/                 # 平台 I/O
│   │   ├── base.py               # BaseAdapter + Message + Protocol
│   │   ├── discord/bot.py        # /feed, /settings, /digest 命令
│   │   └── telegram/bot.py       # /add, /filter, /digest 命令
│   └── api/                      # FastAPI 路由（可选）
├── tests/
│   ├── conftest.py               # 内存 SQLite session fixture
│   └── unit/                     # 134 个测试
├── pyproject.toml                # 依赖权威
├── alembic.ini                   # 迁移配置
├── Makefile                      # 常用命令
├── README.md                     # 用户文档（英文主）
├── README_CN.md                  # 用户文档（中文）
└── GUIDE.md                      # 本文档（详细指南）
```

---

读到这里就差不多了。剩下的细节建议直接读代码 —— 这个项目不大，
`src/newsflow/` 总共 ~3500 行，一下午能通读。有任何这份文档没覆盖到的
陷阱、决策、或困惑，欢迎开 issue 或直接 PR 补充到这里。
