# NewsFlow-Bot 详细指南

README 是"能跑起来"的最小路径；本文档是**部署与运维**的详细参考：命令、配置项、AI 功能、webhook 与非 RSS 源、OPML、REST API、部署运维与常见问题。

---

## 目录

- §1 [完整命令参考](#一完整命令参考)
- §2 [完整配置项](#二完整配置项)
- §3 [AI 功能详解（翻译 / 日报 / 提示词定制）](#三ai-功能详解)
- §4 [Webhook 推送（出站）](#四webhook-推送)
- §4B [非 RSS 信息源（JSON-API / 邮件 / 入站）](#四b非-rss-信息源sourcesyaml)
- §5 [OPML 导入导出](#五opml-导入导出)
- §6 [REST API](#六rest-api)
- §7 [高级部署与运维](#七高级部署与运维)

---

## 一、完整命令参考

所有命令按"功能类别"分组。Discord 为 slash command（斜杠 + 自动补全），Telegram 为前缀命令（空格分隔参数）。

> **Discord 不需要特权 intent**：本项目只用斜杠命令，Developer Portal → Bot → Privileged Gateway Intents 三个开关（Presence / Server Members / Message Content）**全部关闭**即可正常工作。v0.9.1 之前的版本会申请 Message Content intent，Portal 里没开就直接 `PrivilegedIntentsRequired` 崩溃循环；从那之前的版本升级上来的话，这个开关现在是多余授权，建议关掉。

> **Discord 可用频道**：`/feed add` 在普通文字频道、子区 / 帖子（thread）、论坛帖、以及语音频道自带的文字聊天里都能用——推送会投递到运行命令的那个频道。

> **URL 参数自动补全**：除 `/feed add` 和 `/feed test`（输入的本来就是新 URL）外，所有带 `<url>` 参数的命令输入时都会从**本频道订阅列表**弹出建议，可按标题或 URL 关键字过滤；`/feed pause` 只建议未暂停的订阅，`/feed resume` 只建议已暂停的并额外提供 `all`。建议的值就是库里存储的真实 feed URL——粘网站首页订阅时实际入库的是自动发现的 feed 地址，之后管理直接选建议即可，无需手抄 `/feed list` 里的 URL。（超过 100 字符的 URL 受 Discord 选项上限约束不进建议，仍可手动粘贴。）

### 1.1 Discord 斜杠命令

**Feed 管理**

| 命令 | 说明 |
|---|---|
| `/feed add <url>` | 订阅；`<url>` 可是 feed 地址、**网站首页**（自动发现）、**JSON Feed** 或简写（见下表后说明）。成功后几秒内自动推一条预览。对已暂停的订阅执行等同恢复，回执会写明，暂停期间积下的条目随后补发 |
| `/feed remove <url>` | 退订 |
| `/feed pause <url>` | 暂停（不删，可 resume；暂停的订阅仍显示在 `/feed list`，带 ⏸ 标注） |
| `/feed resume <url>` | 恢复已暂停的订阅；被自动禁用的 feed 也会一并复活重新抓取。`/feed resume all` 恢复本频道全部暂停订阅（bot 被踢出重邀后批量恢复用） |
| `/feed list [page]` | 分页列出本频道订阅（每页 20 条，含暂停/静默/故障状态标注） |
| `/feed test <url>` | 不订阅，只测试 URL 是否是合法 feed |
| `/feed status <url>` | 单 feed 详情：状态、积压条数、没送达的条目（排队期间过期被清理的、平台反复拒收而放弃的）、翻译设置、最近一次成功抓取与最近一次尝试的时间、最近错误、最近 5 篇文章 |

> **`/feed add` 接受的输入**：
> - **feed 地址**：常规 RSS / Atom / **JSON Feed**（[jsonfeed.org](https://jsonfeed.org)）URL。
> - **网站首页**：粘 `https://example.com`，自动解析页面 `<link rel="alternate">` 找到真正的 feed 再订阅（添加时解析一次，之后直接抓真实 feed）。
> - **简写**（自动展开成真实 feed URL；含 `://` 守卫，绝不改动正常 URL）：`gh:owner/repo`（GitHub releases）、`gnews:关键词`（Google News 搜索）、`yt:频道ID`、`pypi:包名`、`reddit:版块`、`masto:user@instance`。
>
> 这些只是 RSS 抓取的便利层。**非 RSS 源**（JSON-API / 邮件 / 入站 webhook）走声明式 `sources.yaml`，见 [§4B](#四b非-rss-信息源sourcesyaml)。

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
| `/feed filter-set <url> include:<csv 或 /regex/> exclude:<csv 或 /regex/>` | 设置过滤（两个参数都可选；都空等于清除） |
| `/feed filter-show <url>` | 查看当前过滤规则 |
| `/feed filter-clear <url>` | 移除过滤 |

匹配行为：对**清理后的 title + summary + 正文**（HTML 已剥离——exclude 词不会再被链接域名/标签属性误触发）做大小写不敏感匹配。**纯 ASCII 词形关键词按整词匹配**（`ai` 不再命中 brain/said；短语如 `machine learning` 同理），**CJK 与含符号关键词保持子串匹配**（中文没有词间空格，子串就是自然语义）。`include` 需要"至少命中一个"，`exclude` 需要"一个都不命中"，两者叠加评估。**regex**：某个字段整体写成 `/模式/` 时视为单条大小写不敏感正则（多项用 `|` 表达，逗号不再与 CSV 冲突；上限 256 字符），如 `include:/\bGPT-\d+|Claude/`。

**静默 / 日报模式**

| 命令 | 说明 |
|---|---|
| `/feed silent <url> <bool>` | 单订阅静默：不推即时消息，但条目仍进日报 |
| `/feed display <url> [summary:<bool>] [image:<bool>]` | 单订阅显示控制：`summary:False` = 只推标题的紧凑模式；`image:False` = 不带图。翻译缓存不受影响 |
| `/settings silent <on\|off>` | 频道级批量静默：一次切换本频道所有订阅 |

行为：静默订阅**不**调用 `send_message`，但仍写 `SentEntry(was_filtered=False)`，所以日报照常吃这些条目（关键词过滤拦掉的还是 `was_filtered=True`，不进日报）。开了 silent 后**首次** `/add` 仍会发一条预览，给用户确认订阅成功；后续按 silent 投递。`/feed list` 用 🔇 chip 标识静默订阅。

**频道级默认（`channel_settings` 表）**：频道级命令（`/settings language|translate|silent`，Telegram 端 `/language` `/translate` `/silent`）除了批量改现有订阅，还会把偏好**持久化为频道默认值**，之后 `/add` 的新订阅自动继承 language / translate / silent 三项。这意味着：① **空频道**先 `/settings silent on` 再 `/add`，第一个订阅就是 silent（旧版"空频道限制"已消除）；② 频道改过语言后新加的 feed 不再回落到全局默认 zh-CN；③ 命令在空频道执行也算成功（回显"已存为频道默认，更新了 0 条现有订阅"）。未记录过偏好的频道走旧行为：silent 用"现有订阅全 silent 则继承"的启发式，language/translate 用全局默认。per-feed 覆盖（`/setlang` 等）永远优先——继承只发生在订阅创建那一刻。

**消息模板（自定义排版）**

| 命令 | 说明 |
|---|---|
| `/feed template <url>` | 查看当前模板（未设则列出全部占位符） |
| `/feed template <url\|all> template:<文本>` | 设置模板；`url` 处填 `all` 批量套用到本频道全部现有订阅 |
| `/feed template <url\|all> reset:True` | 清除模板，回到默认排版 |

模板是带 `{占位符}` 的 Markdown 文本，**完全接管消息正文**（未设模板的订阅零变化）。占位符：`{title}` `{summary}`（生效值——有译文用译文，否则原文）、`{original_title}` `{translated_title}` `{original_summary}` `{translated_summary}`（双语排版用）、`{url}`（别名 `{link}`）、`{source}` `{published}` `{image_url}`、`{mention}`（本订阅配置的提及对象，见下方提及小节；未配置时为空）。规则：

- **Markdown 子集**：`**粗体**`、`[文字](链接)`，两端同一语法（Discord 原生渲染；Telegram 转成 HTML，实体被拒时自动降级纯文本，链接预览保持开启，预览卡固定取条目自身的链接）。
- **换行**：Discord 的选项输入框是单行的，用 `\n` 表示换行；Telegram 端 `/template` 可直接写多行，`\n` 也认。`{{` `}}` 输出字面花括号。
- **空值行折叠**：某一行的占位符**全部**解析为空时整行删除（`🖼 {image_url}` 在无图条目上不会留下悬空的 🖼）；行内有任一占位符非空则保留。
- **校验**：设置时拒绝词形未知的占位符（如拼错的 `{tittle}`）并列出合法集，也拒绝一个占位符都不含的模板（否则每条推送都是同一段文字）；已存模板在投递时对未知占位符原样放行（不因此丢文章）。模板本体 ≤1000 字符；渲染结果按平台截断（Discord 2000 字符；Telegram 3500 字符，在转成 HTML 之前截）。
- **与显示控制的关系**：设模板后 `show_summary` 不再参与（正文由模板全权决定）；`show_image` 仍控制图片附带（Discord 以纯图 embed 附在模板消息下）。
- 设置成功即回一条**预览**（用该 feed 最近一条真实条目渲染；`all` 或空 feed 用样例数据）。新订阅**不**继承模板（没有频道级模板默认）。

示例（Telegram 多行直书；Discord 把换行写成 `\n`）：

```
/template https://hn.example/rss
📌 **{title}**
{summary}
原文：{original_title}
🔗 {url} · {source} · {published}
```

**提及（Mention，响铃通知）**

| 命令 | 说明 |
|---|---|
| `/feed mention <url>` | 查看当前提及对象 |
| `/feed mention <url\|all> target:<@角色或@成员>` | 该 feed 出新文章时 @ 对方（原生选择器选人/选角色，无需手打 id） |
| `/feed mention <url\|all> clear:True` | 移除提及 |

作用：embed 推送本身**不触发任何通知**，频道不点开就没人知道有新消息；设置 mention 后，推送会带一行真正"响铃"的 @（默认布局下在 embed 上方；模板订阅默认加在首行，模板里写了 `{mention}` 则由模板控制位置）。**Ping 安全基线**：bot 客户端全局默认 `AllowedMentions.none()`——feed 标题/摘要里混进的 `@everyone` / `@用户` 永远不会真的 ping 到任何人，投递时只放行你显式配置的那一个角色/用户。

**OPML**

| 命令 | 说明 |
|---|---|
| `/feed export` | 导出本频道订阅为 OPML 文件 |
| `/feed import <file>` | 上传 `.opml` / `.xml` 文件批量订阅（上限 1 MB） |

**AI 日报 / 周报**

| 命令 | 说明 |
|---|---|
| `/digest enable schedule:<daily\|weekly> [hour:<0-23>] [weekday:<0-6>] [language:<code>] [timezone:<tz>] [include_filtered:<bool>] [max_articles:<n>]` | 启用或更新。`hour` 缺省 9；`hour`/`weekday` 按 `timezone` 解释（缺省 UTC）；`timezone` 收 IANA 名（`Asia/Shanghai`）或固定偏移（`+8`、`-5:30`），启用时刻换算成 UTC 入库（DST 地区冬夏偏 1 小时，重新 enable 即校正），确认回显同时给本地与 UTC。启用或改时间后从**下一个**投递时段开始，不会当场补发一期 |
| `/digest show` | 查看当前配置 |
| `/digest disable` | 关闭（配置保留） |
| `/digest now` | 立即生成一份预览：不置顶、不 `@here`、不计入投递记录，当天的定时 digest 照常到达 |

**其他**

| 命令 | 说明 |
|---|---|
| `/status` | Bot 整体状态 |

### 1.2 Telegram 命令

> **频道（Channel）支持——私聊绑定**：Telegram 频道里没法执行命令（频道帖没有"发送者"，bot 收不到命令语义）。管理频道的方式是：① 把 bot 以**管理员**身份加进频道（它要发帖本来就需要）；② 私聊 bot，把频道引用放在命令第一个参数：`/add @频道用户名 <url>`、`/list @频道用户名`、`/digest @频道用户名 enable daily 21 zh-CN +8`……除 `/test`、`/import`、`/status`、`/settopic` 外全部管理命令都支持这个形式。**私有频道**没有 @用户名，用 `-100…` 数字 id 代替（网页版频道 URL 里可见）。校验规则：bot 必须能访问该频道、你必须是该频道管理员（`ADMIN_USER_IDS` 旁路），判定结果与群管理员检查共用 60s 缓存。推送、预览、digest 都会投到频道本身。

> **群组前置**：bot 默认开启 privacy mode 也能收到 `/command` 形式的命令（斜杠命令不受 privacy mode 限制）；但要在群里**拖 OPML 文件导入**，需要在 BotFather 里 `/setprivacy` 关闭 privacy mode 或把 bot 设为群管理员，否则 bot 看不到文件消息、静默无反应。

**Feed 管理**

| 命令 | 说明 |
|---|---|
| `/add <url>` | 订阅；对已暂停的订阅执行等同恢复，回执会写明，暂停期间积下的条目随后补发 |
| `/remove <url>` | 退订 |
| `/pause <url>` | 暂停（暂停的订阅仍显示在 `/list`，带 ⏸ 标注） |
| `/resume <url>` | 恢复；被自动禁用的 feed 一并复活。`/resume all` 恢复本频道全部暂停订阅 |
| `/list [page]` | 分页列出（带 ◀/▶ 翻页按钮） |
| `/manage [page]` | **按钮式管理面板**：每个订阅一个按钮，点开后可直接 暂停/恢复、静默/取消静默、删除（删除需二次确认）——全程不用手抄 URL。打开面板是只读的；每次**按下会改状态的按钮**都实时校验权限（群内按 `TELEGRAM_ADMIN_ONLY` 规则，频道面板要求频道管理员），别人打开的面板你按也一样要过权限 |
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
| `/filter <url> include=a,b exclude=c,d` | 设置过滤（两字段都可选）。匹配语义同 Discord 端（清理后全文、ASCII 整词/CJK 子串） |
| `/filter <url> include=/regex/` | 字段整体 `/.../` 为单条正则（Telegram 参数按空格切分，正则内空白请写 `\s`） |

**静默 / 日报模式**

| 命令 | 说明 |
|---|---|
| `/silent <on\|off>` | 频道级批量静默 |
| `/setsilent <url> <on\|off>` | 单订阅静默（覆盖频道默认） |
| `/setdisplay <url> <summary\|image> <on\|off>` | 单订阅显示控制：`summary off` = 只推标题的紧凑模式；`image off` = 不带图 |

语义同 Discord 的静默模式（见上）：静默订阅不推即时消息，但条目仍进日报；首次 `/add` 仍发一条预览。

**消息模板**

| 命令 | 说明 |
|---|---|
| `/template <url>` | 查看当前模板（未设则列出全部占位符） |
| `/template <url\|all> <模板文本>` | 设置模板，**可直接多行输入**；`all` 批量套用到本频道全部订阅 |
| `/template <url\|all> reset` | 清除模板，回到默认排版 |

占位符、Markdown 子集、空值行折叠、校验等语义同 Discord 端 `/feed template`（见 1.1 消息模板小节）；设置成功即回预览。群里 set/reset 形式受 `TELEGRAM_ADMIN_ONLY` 管辖，裸 `/template <url>` 查看对所有成员开放。

**论坛话题（Topics）投递**

| 命令 | 说明 |
|---|---|
| *在话题里 `/add` / `/import` / 拖 OPML* | 新订阅自动记住该话题，以后的推送都进那里 |
| `/settopic <url>` | **在目标话题里执行**：把该 feed 的推送指到这个话题 |
| `/settopic all` | 本群全部订阅指到当前话题 |
| `/settopic <url\|all> clear` | 回到 General（默认视图） |

适用于开启"话题"模式的超级群；在 General 或普通群执行等同 clear。同一 feed 在一个群里只有一份订阅，因此归属一个话题。**话题被删除**后投递自动自愈：该订阅回落 General（日志出 warning），之后可再 `/settopic` 指向新话题。命令回复与订阅预览天然落在你输入命令的那个话题。`/settopic` 是变更型命令，群里始终要求管理员；频道没有话题概念，故不支持频道绑定形式。

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
| `/digest enable daily <hour> [lang] [tz] [options]` | 启用日报。`hour` 按 `tz` 解释；`tz` 收 `Asia/Shanghai` / `+8` / `utc`（缺省 UTC），两个尾参顺序随意。options 是 `max_articles=<1-200>`（缺省 50）与 `include_filtered=on\|off`（缺省 off），位置随意；不写的项保留当前值 |
| `/digest enable weekly <weekday> <hour> [lang] [tz] [options]` | 启用周报（weekday 支持 `mon…sun` 或 `0…6`，同样按 `tz` 解释；options 同上） |
| `/digest disable` | 关闭 |
| `/digest now` | 立即生成一份预览（不置顶、不计入投递记录，定时 digest 照常到达） |

**其他**

| 命令 | 说明 |
|---|---|
| `/start`、`/help` | 帮助；Telegram 自动显示 "/" 命令菜单，`/start` 与 `/help` 附 My feeds / Manage / Status / Help 四个快捷按钮 |
| `/status` | Bot 状态 |

### 1.3 常用语言代码

`zh-CN`（简中）· `zh-TW`（繁中）· `en` · `ja` · `ko` · `fr` · `de` · `es` · `ru` · `ar` · 任何 BCP-47 代码（provider 支持的范围内）

---

## 二、完整配置项

所有配置通过 `.env` 文件或环境变量注入。最小启动只需一个 platform token，或者一份 `webhooks.yaml`（§2.8）。

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
| `OPENAI_MODEL` | `gpt-6-luna` | OpenAI 模型名（翻译） |
| `OPENAI_REASONING_EFFORT` | `none` | 翻译请求的 `reasoning_effort`：`none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`，其他值启动即报错。**留空 = 不发这个参数**，给不认它的兼容端点和老模型用。只有留空或 `none` 才发 `temperature`，其他值不发（OpenAI 在推理开启时只接受默认温度），并给输出上限加推理余量 |
| `OPENAI_BASE_URL` | 空 | OpenAI-compatible 端点（DeepSeek / Qwen / Kimi / 本地 Ollama 等） |
| `TRANSLATION_SYSTEM_PROMPT` | 空 | 翻译 prompt 覆盖（见 §3.3） |
| `GOOGLE_CREDENTIALS_PATH` | 空 | Google Cloud 服务账号 JSON 路径 |
| `GOOGLE_PROJECT_ID` | 空 | GCP 项目 ID |

### 2.4 调度

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FETCH_INTERVAL_MINUTES` | `60` | 抓取循环间隔 |
| `FEED_MAX_CONCURRENT` | `10` | 每轮并发抓取的最大 feed 数（限流信号量 + HTTP 连接数）；feed 多且主机快可调高 |
| `CLEANUP_INTERVAL_HOURS` | `24` | 清理循环间隔 |
| `ENTRY_RETENTION_DAYS` | `10` | 保留多少天的 FeedEntry（按 `created_at`）。默认留了 3 天余量给 weekly digest 的 7 天输入窗口——设回 ≤7 会让清理在周报生成前删掉窗口最早的文章（启动时告警）。近 7 天内才被处理的条目（例如暂停后恢复的订阅）不论入库多久都会留到周报用完 |
| `SENT_ENTRY_RETENTION_DAYS` | `90` | 保留多少天的 `SentEntry`（去重信号），从源最后一次还列着该条目时算起。必须大于 `ENTRY_RETENTION_DAYS`，否则启动报错；不大于 `MAX_ENTRY_PUBLISH_AGE_DAYS` 时告警——这两种情况下源 feed 重新 serve 同一 GUID 都会被当作新条目重复推送 |
| `MAX_ENTRY_PUBLISH_AGE_DAYS` | `14` | dispatch 时跳过 `published_at` 早于此值的条目，防止 feed 突然吐 archive 老文章。`published_at IS NULL` 总是放行；`0` = 禁用此过滤 |

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
| `DIGEST_MODEL` | `gpt-6.1-sol` | 生成 digest 用的模型 |
| `DIGEST_REASONING_EFFORT` | `low` | digest 请求的 `reasoning_effort`，取值与留空的含义同 `OPENAI_REASONING_EFFORT`。`gpt-6.1-sol` 不接受 `none`；换成接受 `none` 的模型时可以设回 `none` |
| `DIGEST_MAX_INPUT_CHARS_PER_ARTICLE` | `300` | 单篇文章喂给 LLM 时的字符上限 |
| `DIGEST_CHECK_INTERVAL_MINUTES` | `5` | 调度循环的检查间隔 |
| `DIGEST_SYSTEM_PROMPT` | 空 | 日报 prompt 覆盖（见 §3.3） |
| `DIGEST_MENTION_ON_DELIVERY` | `false` | 投递 digest 时加一行可见抬头，Discord 端附带 `@here`，让定时日报在刷屏频道里不被淹没。**默认关**，老部署升级后不会突然开始 ping 人。Discord 端应用会放行这个代码加的 `@here`（同时把 LLM 正文里可能出现的 @everyone/@here 中和掉，只有抬头能响铃）；此外 bot 还需要该频道的 "Mention Everyone" 权限，缺权限时 Discord 会把 `@here` 显示为纯文字。其他平台只加抬头 |
| `DIGEST_AUTO_PIN` | `false` | 每次投递自动置顶本次 digest 并取消上一次的置顶，让频道置顶列表恒为"最新一期"。需要 Discord 的 "Manage Messages" / Telegram 管理员权限。置顶失败（缺权限、到 50 条上限）优雅降级：digest 照发、旧置顶保留、日志出 warning。webhook 目的地静默跳过 |

### 2.7 REST API（可选）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_ENABLED` | `false` | 启用 FastAPI 管理端点 |
| `API_HOST` | `127.0.0.1` | 监听地址。**默认回环**（GET 端点会暴露 feed URL——常内嵌 token——与错误详情）；Docker 镜像内部覆盖为 `0.0.0.0`（容器的暴露边界是端口映射），裸机要对外需自行设 `0.0.0.0` |
| `API_PORT` | `8000` | 监听端口 |
| `API_KEY` | `（空）` | API 共享密钥。**空 = 禁用所有写端点（fail-closed）**、读端点开放；**一旦设置，读端点（health/ready/live 探针除外）同样要求携带**。请求带 `Authorization: Bearer <API_KEY>` |
| `INGEST_API_KEY` | `（空）` | 只开放 `/api/ingest` 的独立密钥，给往里推内容的外部系统（NAS、CI、n8n）用：它泄露了也碰不到管理端点。空 = `API_KEY` 兼管 ingest。设了它之后 `API_KEY` 仍然能推 |
| `API_CORS_ORIGINS` | 空 | 浏览器跨域来源白名单（逗号或 JSON 数组）。**空 = 完全不发 CORS 头**；写 `*` 恢复旧的全放行 |

### 2.8 声明式配置文件

这两项指向 YAML 文件，**文件存在与否就是功能开关**——想关掉某个功能，删文件即可，不需要改任何环境变量。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEBHOOKS_CONFIG_PATH` | `./data/webhooks.yaml` | 出站 webhook 目的地与订阅声明（见 §4）。文件存在即启用，启动日志打印 `Webhook: ✓ enabled` |
| `SOURCES_CONFIG_PATH` | `./data/sources.yaml` | 非 RSS 源（JSON-API / IMAP / 入站 webhook）声明（见 §4B）。文件存在即启用同步。删掉文件只是不再同步，已同步的源照常抓取与投递；要撤下全部源，把文件清空为 `sources: {}` 再重启或热重载 |

> **Docker 下别用上面的默认值**：compose 已把两者分别覆盖为 `/app/config/webhooks.yaml` 和 `/app/config/sources.yaml`，对应宿主机上挂载进去的 `config/` 目录（只读）。也就是说 Docker 部署只要把文件放进仓库根目录的 `config/` 就行，`.env` 里**不用**设这两个变量。表中的路径是裸机运行的默认值。

### 2.9 日志

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console`（彩色）或 `json`（结构化） |
| `DB_ECHO` | `false` | 把每条 SQL（含绑定参数）打进日志。**故意与 `LOG_LEVEL` 解耦**：调应用问题把 LOG_LEVEL 开到 DEBUG 时，不应该顺带把 DB 里存的密钥（webhook URL 里的 token、HMAC secret、API key）冲进日志。只在本地调 SQL 时开 |

### 2.10 配额（0 = 无限制）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_FEEDS_PER_CHANNEL` | `0` | 单频道最多订阅数 |
| `MULTI_TENANT` | `false` | 多租户模式开关，为将来的托管服务预留。**自建用户忽略即可**，代码里目前没有依赖它的逻辑分支 |

### 2.11 权限

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TELEGRAM_ADMIN_ONLY` | `true` | Telegram **群组**里的变更型命令（/add、/remove、/pause、/resume、/language、/translate、/setlang、/settrans、/silent、/setsilent、/setdisplay、/settopic、/import、/filter 与 /template 的 set/clear/reset 形式、/digest 的 enable/disable/now）仅群主/管理员可用；只读命令（/list、/info、/status、/export、/test、/digest show、裸 /filter 与 /template 查看等）所有成员可用。**私聊永不受限**。设为 `false` 恢复旧的任何成员可管理行为 |
| `ADMIN_USER_IDS` | 空 | 全局管理员用户 id 列表（数字 id），**始终**通过 Telegram 群管理员检查。支持逗号分隔（`123,456`）或 JSON 数组两种写法。Discord 端不消费此项——Discord 用原生命令权限（见下） |

Discord 端无需配置：`/feed`、`/settings`、`/digest` 三个命令组自带 `default_permissions`（Manage Server），普通成员默认看不到这些命令；服务器管理员可在 **服务器设置 → 整合（Integrations）** 里按角色/频道放宽整组。三个命令组**只在服务器频道里可用、私信里不可用**：私信不受 Manage Server 限制，否则任何与 bot 同服务器的人都能在私信里订阅、抓取、开 digest，消耗部署者的抓取、翻译与 LLM 额度。顶级 `/status` 保持公开。Discord 平台限制：权限只能设到命令组一级，无法给组内单个子命令单独放行。管理员身份判断结果在 bot 侧缓存 60 秒（Telegram），提升/撤销管理员最迟 1 分钟生效。

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
检查服务缓存（memory 或 Redis，按文本 hash + 语言 + 模型 / 端点 / prompt，TTL 7 天）
   │ 命中 → 返回，并回填到 DB 缓存
   ▼ miss
调用 Provider API → 两层缓存都写 → 返回
```

这意味着同一篇文章推给多个同语言订阅时，**只翻译一次**；重启服务后 DB 缓存仍在，只是内存缓存需重建。

**同语言短路**（省钱路径）：源语言已经等于目标语言时不再花翻译调用——两级判定：① 免费的脚本启发式（zh/ja/ko 文字系统唯一可判定；简繁体是不同"变体"，zh-TW 目标 + 简体原文**不会**被短路，转换场景照常走翻译；拉丁字母语言无法从字形区分故永不启发式跳过）；② provider 回传的 `detected_source_lang` 等于目标时（如英文源 + `en` 目标），丢弃"润色"结果、跳过 summary 调用，并把原文回填 DB 缓存让其他同目标订阅零调用（zh 除外，检测器只报笼统的 ZH 看不见简繁边界）。

### 3.2 AI 日报 / 周报

Digest 会按频道的配置周期性生成。**默认关闭**，需要通过命令（`/digest enable`）对每个频道单独启用。

**什么时候发**：调度循环每 `DIGEST_CHECK_INTERVAL_MINUTES`（默认 5）分钟醒来一次，对每个启用的频道算出最近一个应投递的时刻（槽位），上一期服务过的槽位早于它就投递。所以：

- 进程跨过投递小时宕机或重启，恢复后这一期**晚发补上**，而不是静默跳过；补发记的仍是原槽位，下一期准点回到整点。
- `/digest enable`（首次启用、改时间、停用后重新启用都算）把当前槽位记为已服务，第一期从下一个槽位开始，不会当场炸出一期。
- `/digest now` 是预览：不置顶、不 `@here`、什么都不记录，当天的定时 digest 照常到达。
- 定时生成或发送失败后按检查间隔 × 2^失败次数退避（默认 10、20、40、80 分钟，之后每 160 分钟），不会每 5 分钟把整个窗口再喂给 LLM 一次；一次成功清零。

**总结哪些文章**：上一次定时投递之后、到现在为止，推送过的条目（第一次没有上一次，取过去 24 小时或 7 天）。默认只算真正推出去的；`include_filtered=True` 时连被关键词过滤掉的也算。窗口为空就只推进游标，不生成。

**怎么生成和发送**：LLM 只写正文和内联 `[N]` 引用，代码再把每个 `[N]` 换成指向该篇文章的链接（杜绝模型复述 URL 时截断或幻觉）；按平台分片（Discord 1900 字符；Telegram 3800，只算显示出来的文字）发送。Telegram 端逐片 Markdown→HTML、`parse_mode=HTML`、关闭链接预览，实体解析被拒时降级纯文本；Discord 原生渲染 Markdown。

**成本估算**（以默认的 gpt-6.1-sol、effort `low` 为例）：50 篇文章 × 300 chars ≈ 7k input + 2k 正文 + 1–2k 推理 output = **约 $0.05 / 次**；每日跑 ≈ $1.5/月，每周跑 ≈ $0.2/月。换 gpt-6-luna（effort `none`）约为其 1/30。

**默认 prompt 产出**：按具体话题聚合（分几组由素材决定）、每组 2-4 句、内联 `[N]` 引用、纯 Markdown、总长 1500-3500 字符。**链接不由 LLM 书写**——投递前由代码把正文里每个 `[N]` 换成可点击的 `[N]`，指向真实文章数组里的第 N 篇，点标号直达原文，末尾不再附来源列表（链接用 `<…>` 角括号包住，压掉 Discord 的预览）。编号认 `[1][3]`，也认模型常写偏的 `[1, 3]`、`[1-3]` 与全角 `［1］`、`【1】`，一律展开成单独可点的标号；不对应任何文章的编号原样留着。模型一个编号都没引用时，兜底在末尾列出全部输入文章（标题压成一行、截 80 字符）。模型（或教它这么做的自定义 `DIGEST_SYSTEM_PROMPT`）若自己写了来源列表，正文末尾以编号或列表符号开头、带 URL 的行会被剥掉。正文里模型写的 Markdown 链接若不指向输入文章之一，只留 `[文字]`、去掉链接——文章原文会进 prompt，不能让它借模型之手把任意地址包装成引用；直接写出的 URL 照常显示，与普通投递一致。若需不同风格（技术向、学院派、段子手等）见下节。

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

**覆盖方式**：`.env` 里设 `TRANSLATION_SYSTEM_PROMPT`。改 prompt、`OPENAI_MODEL` 或 `OPENAI_REASONING_EFFORT` 之后，
已经翻译过的条目沿用旧译文（译文存在条目上），之后翻译的条目才用新配置。

**示例 —— 科技评论风**：

```bash
TRANSLATION_SYSTEM_PROMPT="You are translating a tech blog from {source_desc} to {target_name}. Keep code snippets, product names, and technical acronyms in the original language. Use natural, conversational tone in the target language. Output only the translation."
```

#### Digest prompt

**默认值**（见 `src/newsflow/services/summarization/openai.py::SYSTEM_PROMPT_TEMPLATE`）：一段 "you are a news editor" 模板，要求按话题聚合、不虚构事实、每条事实带 `[N]` 引用、纯 Markdown、总长 1500-3500 字符。

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
- 输出上限写死在代码里：翻译 2000 token、digest 4000 token；开了推理（effort 不是空也不是 `none`）时再加一段推理余量。温度 `0.3` 只在 effort 为空或 `none` 时发送，其他 effort 下不发（OpenAI 开启推理后只接受默认温度）。要改这些目前得改代码

### 3.4 Provider 支持度

| 功能 | 支持的 provider |
|---|---|
| **翻译** | DeepL、OpenAI（或任何 OpenAI-compatible：DeepSeek / Qwen / Kimi / OpenRouter / 本地 Ollama 等）、Google Cloud Translation |
| **Digest** | OpenAI-compatible（同上） |

通过 `OPENAI_BASE_URL` 指向任意 OpenAI-compatible 端点即可换 provider，无需改代码。单次请求的超时是翻译 60 秒、摘要 300 秒，各重试一次；翻译超时的条目按原文投递，摘要超时按 §3.2 的退避曲线稍后再试。

### 3.5 接本地大模型（Ollama / vLLM / LM Studio / LocalAI）

```bash
TRANSLATION_ENABLED=true
TRANSLATION_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_MODEL=qwen2.5:14b
OPENAI_API_KEY=any-non-empty-string
DIGEST_MODEL=qwen2.5:14b
OPENAI_REASONING_EFFORT=
DIGEST_REASONING_EFFORT=
```

四个容易踩的点：

- **`OPENAI_API_KEY` 必须非空，哪怕本地服务根本不校验它。** 翻译与 digest 两个工厂都拿它当「是否已配置」的开关，留空等于没配：digest 只在第一次到期时打一行 `Digest disabled: ...`，翻译在启动时打一条 `Config:` 告警然后按原文投递，两边都不报错退出，看起来像功能坏了。填任意字符串即可。
- **翻译与 digest 共用同一个 `OPENAI_BASE_URL` 和同一个 key，只有模型名分开**（`OPENAI_MODEL` / `DIGEST_MODEL`）。所以做不到「翻译走本地、digest 走云端」——要么都本地，要么都云端。
- **两个 `*_REASONING_EFFORT` 要显式留空。** 默认值是给 OpenAI 的（翻译 `none`、digest `low`），不认 `reasoning_effort` 的端点会把每次请求都拒掉，翻译全部按原文投递、digest 一份也出不来。本地推理模型认这个参数的，按它的取值填。
- **Docker 部署下 `localhost` 指的是容器自己。** 模型服务跑在宿主机就要写 `host.docker.internal`，或者把它接进同一个 compose 网络。

**选型提示**：翻译对模型能力要求不高，本地小模型够用。digest 不一样——可点的来源标号是代码从模型输出的 `[N]` 引文换出来的（见 §3.2），模型若不稳定遵守这个约定，正文照出但出处缺失。digest 建议用参数量大一些的模型。

---

## 四、Webhook 推送

把 feed 更新推送到任意 HTTP 端点。一个声明式 YAML 文件搞定所有目的地，无需 bot 命令。

### 4.1 什么时候用

- 想发到 Slack / ntfy / 飞书（Lark）/ 企业微信，但不想为每个再写一个完整 adapter
- 想推到 Discord 频道但**不想装 bot**：频道设置里建个 webhook 就行，不需要 bot token、不需要把应用邀请进服务器，也不需要「管理服务器」权限。代价是没有斜杠命令、按钮面板和置顶，投递本身完全一样
- 想推到 Matrix 房间（经 [matrix-hookshot](https://matrix-org.github.io/matrix-hookshot/latest/setup/webhooks.html) 的 generic webhook）
- 想让 n8n / Zapier / 自己的后端接到 RSS 事件做二次处理（触发 CI、入库、转发等）
- 想给个人手机推送最新文章（ntfy 自托管或 ntfy.sh）

### 4.2 快速上手

1. 把 `samples/webhooks.example.yaml` 复制到 `config/webhooks.yaml`（Docker 部署；裸机运行默认路径是 `data/webhooks.yaml`）
2. 编辑文件，填你自己的目的地和订阅
3. 重启 bot；启动日志会打印 `Webhook: ✓ enabled`

删掉 `config/webhooks.yaml` 即可完全关闭该功能，不影响其他平台。

### 4.3 配置文件结构

```yaml
destinations:
  <name>:                            # 用户可读别名，订阅用它引用
    url: <http endpoint>             # 可写 ${VAR}，见下
    format: generic | slack | discord | matrix | ntfy | lark | wecom
    secret: <可选, HMAC-SHA256 key>  # 可写 ${VAR}
    headers:                         # 可选, 任意自定义 HTTP headers, 值可写 ${VAR}
      Authorization: "Bearer ${MY_HOOK_TOKEN}"
    timeout_s: 10                    # 可选, 请求超时, 默认 10 s, 上限 60 s
    translate: false                 # 可选, 是否对此目的地启用翻译, 默认 false
    language: zh-CN                  # 可选, translate=true 时的目标语言

subscriptions:
  <name>:                            # 必须已在 destinations 里定义
    - <feed_url>
    - <feed_url>
```

**凭据用 `${VAR}` 引用环境变量**：`url`、`secret` 与 `headers` 的值里的 `${VAR}` 在**发送时**才从进程环境展开，数据库里存的是引用本身，凭据不进库、不进备份、不随异常文本进日志。Slack、Discord、企业微信、飞书的 webhook URL 本身就是凭据，建议整条写成 `url: ${SLACK_HOOK_URL}`。变量必须出现在**解析这份文件的进程环境**里（启动、热重载、`make checkconfig`）：没设置会直接报错并点名变量，不会等到发送时才失败。Docker 下 compose 会把 `.env` 原样传进容器；裸机运行时 `.env` 只被 bot 自己的配置读取，引用的变量要另外 `export` 或写进 systemd 的 `EnvironmentFile`。

**`translate` 默认关闭**，与 `sources.yaml` 的订阅者一致：开了全局翻译的部署，没写这个键的目的地收到的是原文。

**完整带注释的示例**：`samples/webhooks.example.yaml` —— Slack / Discord / Matrix / ntfy / 飞书 / 企业微信 / n8n 七种目的地全覆盖。

### 4.4 Payload 格式

每种 `format` 生成不同的请求体，对应各自服务的 API 要求：

| format | Content-Type | 结构 |
|---|---|---|
| `generic` | `application/json` | NewsFlow 自定义 JSON（见下） |
| `slack` | `application/json` | Slack [Block Kit](https://api.slack.com/block-kit)，含 fallback text |
| `discord` | `application/json` | Discord [embed](https://discord.com/developers/docs/resources/webhook#execute-webhook)，标题带链接、摘要进 Summary 字段、来源进 footer |
| `matrix` | `application/json` | matrix-hookshot generic webhook 的 `text` + `html` 双体 |
| `ntfy` | `text/plain` | body 是摘要；标题走 `Title` 头（非 ASCII 用 RFC 2047 编码）；链接与图片走 `Click` / `Attach` 头，只有纯 ASCII 的 http(s) URL 才带上，否则省略 |
| `lark` | `application/json` | 飞书 / Lark post 卡片（`msg_type: "post"`） |
| `wecom` | `application/json` | 企业微信群机器人 markdown（`msgtype: "markdown"`） |

**discord 格式**：URL 在「频道设置 → 整合 → 创建 Webhook」里拿，和 bot token 无关。文章正文全部放在 embed 里，且 payload 固定带 `allowed_mentions: {"parse": []}` —— feed 里出现 `@everyone` 也不会真的通知任何人。标题走 embed 的 title 字段而非 markdown 链接，所以标题里的 `](` 无法伪造出可点链接。想推到子区（thread）就直接把 `?thread_id=<id>` 拼在 URL 末尾，bot 原样使用。

**slack / wecom / lark 格式**：feed 文本里的提及语法同样不会生效。slack 按平台规范把 `&` `<` `>` 写成实体，`<!channel>` 只会显示成文字，不是 http(s) 的链接不渲染成链接；wecom 的 text 与 markdown 都认 `<@userid>`，bot 在 `<` 与 `@` 之间插一个零宽空格；lark 的系统通知也发 post 富文本，只有显式的 `at` 元素才会 @ 人，正文里的 `<at user_id="all">` 只是文字。wecom 的 markdown 按 4096 字节、text 按 2048 字节截断，超长不会被接收端整条拒收。

**matrix 格式**：URL 指向 matrix-hookshot 的 `/webhook/<id>`。**不要给这个 webhook 配 transformation function** —— 我们发的 `text` / `html` 正是 hookshot 默认识别的键，配了 JS 转换反而会把它们覆盖掉。feed 文本在 html 侧全部转义，不依赖客户端的标签白名单。

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
3. yaml 里删除的 destination 会被 **删除**（它名下由本文件声明的订阅一起删）
4. yaml 里第一次出现的 feed URL 会被 **自动 add_feed**（一次性网络抓取，和 `/feed add` 一样支持简写与网站首页自动发现）。
   抓取失败（源暂时不可达、返回错误）时**照样建订阅**，由投递循环按退避重试，连续失败十次自动停用并通知该目的地；
   URL 本身不可抓（不是 http(s)、私网或回环地址的字面量）则跳过并告警。都不中断启动
5. 本文件声明过、如今不在 yaml 里的订阅会被 **删除**。同步只动自己建的行（`platform_user_id="yaml"`）：`sources.yaml` 或 REST API 建的 webhook 订阅不归它管
6. 新订阅**不会收到它建立之前发表的文章**（没有日期的也算作之前）。源在声明时还没抓取成功的，这条在它第一次抓取成功时生效，
   所以首抓失败期间新发表的文章照样会投递

所以工作流就是：改 yaml + 重启 bot。不用命令、不用 API。yaml 本身可以进 git，和订阅列表一起版本化管理。

### 4.7 故障排查

| 症状 | 可能原因 |
|---|---|
| 启动日志没看到 `Webhook: ✓ enabled` | `webhooks.yaml` 不在 `WEBHOOKS_CONFIG_PATH`（Docker 默认 `config/webhooks.yaml`，裸机 `./data/webhooks.yaml`）；检查文件路径和权限 |
| `webhooks.yaml is invalid; aborting startup` | YAML 语法错或字段不符合 schema；报错信息里会点出具体位置 |
| 某 feed URL 没被订阅但没报错 | URL 本身不可抓（非 http(s) 或私网 / 回环地址字面量）；启动日志有 `webhook_sync: skipping <url> — <原因>`。首抓失败（404 / 超时 / 解析错）的源照样会被订阅，日志是 `first fetch of <url> failed … subscribing anyway` |
| 接收方收到 HTTP 200 但内容显示成 raw JSON | `format` 选错；比如 Slack webhook 收到 `generic` 格式会直接显示 `{"event": ...}` 字符串 |
| 自己验签总是失败 | 见 4.5 "关键点" —— 必须用收到的原始字节，不是反序列化后的对象 |
| 企业微信 URL 里带 `?key=...` | 把完整 URL 原样贴进 yaml；bot 不会重组 URL |
| 日志出现 `refused: wecom code …` / `refused: lark code …` | 这两家出错时也回 HTTP 200，错误码在响应体里，bot 按它判定成败。企业微信 93000 是 key 失效或机器人已被移出群；飞书 19021 是开了「签名校验」——它要求每次请求在 body 里现算 `timestamp` 与 `sign`，bot 不支持，改用「自定义关键词」或「IP 白名单」；19024 是消息里没有你设的自定义关键词 |
| 改 yaml 后重启没变化 | 检查启动日志 `webhook_sync: N destination(s), M subscription(s)`；若数量不符，说明解析器没拿到最新文件 |
| Discord 收到 HTTP 400 / 什么都没发出来 | 目的地填了 `format: generic`（或漏填）。Discord 原生 webhook 只认 `content` / `embeds`，收到我们的自定义 JSON 会直接 400 —— 改成 `format: discord`。这种配置错误不会丢条目：bot 只在同一目的地确实收下了一条提示时才放弃条目，格式不对时提示也会被拒 |
| Matrix 房间里出现的是一整包 JSON | 该 hookshot webhook 配了 transformation function，把我们的 `text` / `html` 覆盖了；删掉转换脚本即可 |
| 日志出现 `rate-limited; retrying in Ns` | 接收端在限流。发送会按对方给的等待时间重试一次，**不计入熔断计数**；等待超过 5 秒则跳过本轮，条目留到下一轮再发（分发是串行的，不能为一个端点卡住其他平台） |
| 日志出现 `auto-disabled after 10 straight failures` | **目的地熔断**：连续 10 次发送失败（HTTP 非 2xx / 超时 / 连接错 / 企业微信与飞书响应体里的非零错误码，其中限流码 45009 / 11232 与 429 一样不计入；400 / 413 / 422 也不计入——它们说的是这一条内容，不是端点）后该 destination 自动停用，不再发起网络请求（对齐 feed 侧的自动禁用机制）。修好端点后热重载（`POST /api/admin/reload` 或 SIGHUP）或重启即恢复——仍在文件里声明 = 你想让它工作；保留期内未投递的积压会随即补发。任一次成功也会清零失败计数 |

> **未知字段现在是硬错误**：`webhooks.yaml` / `sources.yaml` 里拼错的键（如 `secert:`）过去被静默忽略（HMAC 签名就这样无声消失过），现在直接中止启动并列出合法键。升级后若启动失败，按报错清理多余键即可；部署前可用 `make checkconfig` 离线预检（§7.6）。

### 4.8 扩展一个新 format

比如要加 "Microsoft Teams"（现在只能走 Power Automate Workflows 的 webhook，老的 Office 365 connector 已于 2026 年 5 月停用），在 `src/newsflow/adapters/webhook/formats.py` 加两个函数：

```python
def _to_teams(m: Message) -> WireRequest:
    return _json({
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": m.display_title,
                     "weight": "bolder", "wrap": True},
                    {"type": "TextBlock", "text": m.display_summary, "wrap": True},
                ],
            },
        }],
    })

def _to_teams_text(text: str) -> WireRequest:
    ...  # 同样的信封，body 里放一个 TextBlock
```

然后在文件尾的 `_ENTRY_CONVERTERS` 和 `_TEXT_CONVERTERS` 两个 dict 里各加一行 `"teams": _to_teams` / `"teams": _to_teams_text`。完事。不需要改 adapter 主体、sync 逻辑、model 或 migration —— 只是往 dispatch 表里加了一种选项，`SUPPORTED_FORMATS` 和 YAML 校验会自动跟上。

**但上线前一定要拿真端点打一遍。** 接收方的实际行为和它自己的文档常常对不上：Discord 的 429 响应里 `Retry-After` 头填的是毫秒（`1922`），而 HTTP 规范规定该头是秒 —— 只看文档写出来的重试逻辑会把每次限流都误判成「要等半小时」。Teams 更甚，payload 形状还取决于那条 flow 里「发布卡片」动作怎么配。

---

## 四B、非 RSS 信息源（sources.yaml）

§四 的 webhook 是**出站**（把条目推给别人）。本节是**入站**：把 RSS 之外的东西变成源。和 `webhooks.yaml` 一样**声明式**——`config/sources.yaml`（Docker 部署；裸机默认 `data/sources.yaml`）存在即启用，每次启动幂等同步进 DB（创建 / 更新 / 删除源与订阅）。完整示例见 [`samples/sources.example.yaml`](../samples/sources.example.yaml)。

> **安全边界**：同步只动「非 RSS 类型的 feed」和「`platform_user_id=source-yaml` 的订阅」。你用命令建的 RSS 订阅永不受影响。

### 4B.1 结构

```yaml
sources:
  <名字>:
    url: <地址或标识>
    type: json_api | email_imap | webhook_inbound
    config: { ... }            # 按 type 不同（见下）
    subscribers:               # 谁收这个源的条目
      - platform: discord | telegram | webhook
        channel: "<频道ID / webhook 目的地名>"
        translate: false       # 可选
        language: zh-CN        # 可选
        silent: false          # 可选
```

每个源就是一个 feed，条目走**和 RSS 完全一样**的过滤 / 翻译 / 静默 / 日报 / 投递链路。
新声明的源要等投递循环第一次抓取成功才有条目；那一次抓到的、发表于订阅建立之前的条目（以及没有日期的）算作存量，
不会推送，之后的才是新文章。把源注释掉再恢复也是如此，频道已经收过的不会再推一遍。

**这些订阅只能在 YAML 里改**（webhooks.yaml 的订阅同理）：每次启动和热重载都会把它们恢复成文件里写的样子，所以在聊天命令、`/manage` 面板或 REST API 里暂停、移除，或改它的语言、翻译、静默，会直接被拒绝并提示去哪份文件改；频道级的语言、翻译、静默设置会跳过它们。模板、提及、话题、过滤和显示设置文件里不管，照常可改。

### 4B.2 三种源类型

> 下面各源类型标注的 extra 仅**裸机运行**时需要手动装；Docker 镜像已内置全部 extra（`[all]`），开箱即用。

**`json_api`** — 轮询任意 REST/JSON 接口，用 JSONPath 抽条目。需装 `source-json` extra（`make install-all` 或 `pip install -e '.[source-json]'`，即 jsonpath-ng）。

```yaml
  my-api:
    url: https://api.example.com/articles   # 要轮询的接口
    type: json_api
    # fetch_interval_minutes: 240   # 可选：本源自己的抓取间隔（分钟，源级键
    #                               # 而非 config 键）。全局循环仍按
    #                               # FETCH_INTERVAL_MINUTES 走，本源在自己
    #                               # 间隔未满时跳过——给限速 API 用
    config:
      items: "$.data[*]"     # JSONPath 定位条目数组（必填）
      guid: id               # 每条的去重键字段，要指向稳定的 id。缺省时按整条
                             # 内容 hash，阅读数、更新时间之类一变就再推一次
      title: title
      link: url
      summary: summary
      published: published_at
      # content / image / author 也支持；字段是相对每条的 JSONPath，"author.name" 嵌套也行
      # headers:                                # 可选：附加请求头（Bearer 鉴权等）
      #   Authorization: "Bearer ${MY_TOKEN}"   # ${VAR} 在请求时从环境变量展开，
      #                                         # token 不进 yaml/DB/日志；变量
      #                                         # 未设置则该源响亮失败
```

条目数组按 RSS 的惯例当作**新→旧**排列入库。接口若是旧→新返回、又不带 `published`，无日期条目的投递顺序会反过来。

**`email_imap`** — 轮询 IMAP 邮箱，把每封邮件变成条目（适合**无 RSS 的 newsletter**）。需装 `source-email` extra（`make install-all` 或 `pip install -e '.[source-email]'`，即 imap-tools）。

```yaml
  my-newsletters:
    url: imap://me@example.com/INBOX   # 仅作标识；连接用下面的 config
    type: email_imap
    config:
      host: imap.example.com
      user: me@example.com
      password_env: NEWSFLOW_IMAP_PASS  # 存密码的【环境变量名】
      port: 993                         # 可选，IMAPS 端口，默认 993
      tls: verify                       # 可选，verify（默认）或 insecure
      mailbox: INBOX                    # 可选，默认 INBOX
      limit: 50                         # 可选，每轮取最新 N 封
```

> 🔒 **密码从不入库**：`config` 里只放 `password_env`（环境变量**名**），真正的密码放 `.env` / 环境。请用**应用专用密码**，不是主密码。guid = 邮件 Message-ID，跨轮精确去重。

> 🔒 **`tls: verify` 是默认，别随手改成 `insecure`**：连接虽然是 IMAPS，但不校验证书就等于把邮箱密码和全部邮件内容交给任何能中间人的一方。Gmail / Outlook / Fastmail 这类服务商证书有效，不用动这个键。只有**自建邮件服务器且用自签证书**时才需要 `tls: insecure`——更好的做法是给服务器换一张受信任的证书。默认校验失败时报错会直接点名这个键。

> **没有 `Date` 头的邮件按「无日期」处理**（`published_at` 为空），而不是记成 1900 年。这是有意的：只有空日期能豁免 `MAX_ENTRY_PUBLISH_AGE_DAYS` 的时效过滤，记成 1900 年会让这类邮件被静默过滤掉、永远投递不出去。

**`webhook_inbound`** — 入站推送：外部系统把条目 POST 进来，**不轮询**（无额外依赖）。

```yaml
  ci-events:
    url: ci-events           # POST /api/ingest/{url} 里的 slug
    type: webhook_inbound
    config: {}
    subscribers:
      - platform: discord
        channel: "123456789012345678"
```

外部这样推（需 REST API 启用 + `INGEST_API_KEY` 或 `API_KEY`，见 §2.7 / §6）。推送方在 bot 之外，给它单独的 `INGEST_API_KEY`：泄露了也只能推条目，碰不到删源、重载这些管理端点。

```bash
curl -X POST http://<host>:8000/api/ingest/ci-events \
  -H "Authorization: Bearer $INGEST_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"entries":[{"id":"build-42","title":"Build #42 passed","url":"https://ci/42"}]}'
```

每个 entry 字段：`id`（去重键，缺失则内容 hash）、`title`、`link`/`url`、`summary`、`content`、`author`、`image`、`published_at`。重复 `id` 自动幂等去重。一次推多条时按数组顺序入库，**请按旧→新排列**：没有 `published_at` 的条目只能靠这个顺序决定先发哪条。n8n / Zapier / CI / GitHub webhook 都能接。

### 4B.3 与 §四 出站 webhook 的区别

| | §四 webhook（出站） | 本节 `webhook_inbound`（入站） |
|---|---|---|
| 方向 | NewsFlow → POST 给外部 | 外部 → POST 给 NewsFlow |
| 角色 | 投递目的地 | 信息源 |
| 声明 | `webhooks.yaml` | `sources.yaml` |

### 4B.4 用作告警 / 通知的中转

把 NAS、软路由、CI、监控系统的消息接进来，统一过滤、排版，再扇出到 Discord / Telegram / ntfy / 任意 webhook。**这是一个转发与整合工具，不是告警系统**——下面两张清单请一并读完再决定要不要这么用。

**三条接入路径**

| 路径 | 怎么接 | 适合 |
|---|---|---|
| **邮件桥** | 设备发告警邮件到一个专用邮箱，用 `email_imap` 轮询（§4B.2） | 最通用——几乎所有 NAS / 路由器都能发邮件，设备侧零改造 |
| **入站 webhook** | 设备 POST 到 `/api/ingest/<slug>`（§4B.2） | 最快，入库即触发一轮分发，不等抓取周期；要求设备能发带自定义 header 的请求 |
| **轮询状态 API** | 设备暴露 JSON 端点时用 `json_api`，token 走 `${ENV}` | 定期巡检，不适合突发事件 |

**能做的**

- 一处配置扇出到所有渠道；按关键词 / 正则过滤，噪音事件直接丢掉（`/filter`，见 §一）
- 用消息模板把机器生成的内容排成人能读的样子（`/template`，见 §一）
- 低优先级事件交给 digest 攒成日报，不逐条打扰（§3.2）
- 出站 HMAC 签名、入站 `API_KEY` 鉴权，链路两端都可验证

**不能做的**

- **没有严重级别。** 载荷里没有 priority / severity 字段，硬盘故障和一条新闻走的是同一套排版；发到 ntfy 时用的也是同一组 header（`Tags: newspaper,rss`、默认优先级）。**绕法**：按严重级别分流到不同的 destination——critical 单开一个 ntfy 目的地，info 走 Discord。
- **去重语义对告警是反的。** 按 `id` 幂等对文章正确，对告警经常不是：设备用同一个 `id` 每小时重发「disk 2 failing」，你只会收到**一条**；`id` 每次都变则会刷屏。这里没有「重复通知直到确认」和静默窗口。
- **没有确认、升级、恢复配对。** 没有 ack，没有超时升级，也不会把「已恢复」和原告警关联起来——那是另一条独立消息。
- **没有存活检测。** 它不会因为「设备十分钟没上报」而告警。唯一沾边的是某个源连续 10 次抓取失败后自动停用并发通知（§4.7），那是很弱且很晚的信号。
- **轮询延迟有硬下限。** 源级的 `fetch_interval_minutes` **只能调慢不能调快**，想让邮箱一分钟轮询一次就得把全局 `FETCH_INTERVAL_MINUTES` 调到 1，所有 RSS 源会跟着被抓。要「低延迟告警 + 正常间隔新闻」，走入站 webhook（推送、不轮询），或者干脆跑两个实例。

> ⚠ **断电、宕机这类告警，跑在本机的 NewsFlow 发不出来**——故障域包含了通知器自己。这类信号必须由**故障域之外**的东西探测：另一台机器上的 Uptime Kuma、Healthchecks.io 的 dead-man switch、或路由器本身。NewsFlow 可以做它们的下游（探测方 POST 进 `/api/ingest`，再由这里统一扇出），但不能是探测方。

**推荐组合**：探测和心跳交给专门工具（Uptime Kuma / Healthchecks / ntfy / Alertmanager），它们把严重级别、静默、升级当核心功能做；NewsFlow 站在它们下游做统一的过滤、排版与扇出。如果你本来就在用它收 RSS，把告警也收口到同一处才是它的真实价值所在。

---

## 五、OPML 导入导出

**用途**：从 Feedly / Reeder / NetNewsWire 搬家；备份订阅列表；在多个频道 / 实例间迁移。

### 5.1 导出

- Discord：`/feed export` → bot 返回 `.opml` 附件
- Telegram：`/export` → bot 以 document 形式发送

### 5.2 导入

- Discord：`/feed import file:<上传文件>`
- Telegram：直接拖 `.opml` 文件到聊天（无需命令）；或 `/import <url>` 从 URL 抓取

频道里已暂停的源导入后仍保持暂停，计入「已订阅」。导出包含暂停的订阅，导回同一频道不会恢复它们；要恢复用 `/resume`（Discord `/feed resume`）。

### 5.3 预置源列表

仓库 `samples/curated-feeds.opml` 提供 22 个精选源（WSJ、FT、NYT、Bloomberg、Economist、Reuters、Atlantic、Foreign Affairs、Nautilus、Longreads、Cloudflare Blog、EFF 等）。另有三份按主题拆分的子集：`curated-feeds-general.opml`（11 源）、`curated-feeds-finance.opml`（7 源）、`curated-feeds-tech.opml`（4 源）。

**使用**：下载该文件到本地 → 用上面任一导入方式批量订阅。

> **批量导入不会刷屏** —— 不会一次推 22 条预览消息。导入走"安静流程"，文章在下一轮 dispatch 时正常发送。

---

## 六、REST API

`.env` 里设 `API_ENABLED=true` 启用。

> **鉴权**：未配 `API_KEY` 时——读端点开放、写端点一律 503（fail-closed）。**配了 `API_KEY` 后，除 `/health` `/ready` `/live` 探针外的所有端点（含 GET）都要求** `Authorization: Bearer <API_KEY>`。CORS 默认不发（`API_CORS_ORIGINS` 白名单开启）；默认监听回环地址（见 §2.7）。

### 6.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 服务状态 |
| `GET` | `/ready` | 就绪检查（含 DB 连接）；未就绪返回 **503**（编排系统按状态码摘流量） |
| `GET` | `/live` | 存活探针（K8s 友好） |
| `GET` | `/metrics` | **Prometheus 指标**：调度轮次/发送量/错误计数器（`newsflow_send_errors_total` 逐条计发送失败）、`newsflow_entries_undeliverable_total`（平台反复拒收而放弃的条目）、`newsflow_entries_dropped_unsent_total`（排队期间过期被清理的条目）+ feed/订阅/destination/digest 数量 gauge（文本格式，零依赖手写渲染） |
| `GET` | `/api/feeds` | 列全部 feed |
| `POST` | `/api/feeds` | 🔒 添加 feed；`url` 也收聊天命令里的简写（`gh:owner/repo`、`pypi:包名` 等） |
| `GET` | `/api/feeds/{id}` | 单 feed 详情 |
| `DELETE` | `/api/feeds/{id}` | 🔒 删 feed |
| `POST` | `/api/feeds/{id}/refresh` | 🔒 强制刷新 |
| `POST` | `/api/feeds/test` | 🔒 测试 URL，同样收简写 |
| `GET` | `/api/stats` | 总体统计 |
| `GET` | `/api/stats/feeds` | 每个 feed 的统计 |
| `GET` | `/api/subscriptions?platform=&channel=` | **列一个频道的全部订阅**（含暂停；返回 id 供下面的操作端点用） |
| `GET` | `/api/subscriptions/opml?platform=&channel=` | 频道订阅导出为 OPML |
| `POST` | `/api/subscriptions` | 🔒 订阅：`{platform, channel_id, feed_url}`，`platform` 只收 `discord` / `telegram` / `webhook`（其余 422）——缺失的 feed 走与 `/add` 相同的抓取校验路径 |
| `POST` | `/api/subscriptions/{id}/pause` · `/resume` | 🔒 暂停 / 恢复某订阅 |
| `DELETE` | `/api/subscriptions/{id}` | 🔒 退订（连带过滤器与去重历史，语义同 `/remove`） |
| `POST` | `/api/ingest/{source}` | 🔑 **入站推送**：条目 POST 进对应 `webhook_inbound` 源（见 §4B），**入库即触发一轮即时分发**——不再等下一个抓取周期 |
| `POST` | `/api/admin/reload` | 🔒 **热重载** `webhooks.yaml` + `sources.yaml`（等价于 SIGHUP，见 §7.6）；文件解析失败保持旧状态并返回 400 |

🔒 = 需 `Authorization: Bearer <API_KEY>`；未配 `API_KEY` 返回 503。🔑 = `INGEST_API_KEY` 或 `API_KEY` 均可；两个都没配返回 503。

`LOG_LEVEL=DEBUG` 时自动暴露 `/docs`（Swagger UI）。

---

## 七、高级部署与运维

### 7.1 Compose Profiles

```bash
# 加 Redis（想让翻译缓存跨重启时用；注意本项目定位单实例，Redis 不提供多副本调度安全）
docker compose -f docker/docker-compose.yml --profile with-redis up -d

# 加 Postgres（订阅超 10 万条再考虑；SQLite 之前都够用）
docker compose -f docker/docker-compose.yml --profile with-postgres up -d

# 全栈
docker compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres up -d
```

**切 Postgres 必须两步**：profile 只是把 Postgres 容器起起来，应用连不连它取决于 `DATABASE_URL`。在 `.env` 里加：

```bash
DATABASE_URL=postgresql+asyncpg://newsflow:newsflow@postgres/newsflow
# 主机名是 compose 服务名 `postgres`；用户/密码/库名默认都是 newsflow，
# 可用 POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB 环境变量改
```

然后 `up -d`。不设 `DATABASE_URL` 的话应用仍用 SQLite，Postgres 容器只是空转。
（Redis 同理：`CACHE_BACKEND=redis` + `REDIS_URL=redis://redis:6379/0`。）

### 7.2 systemd + venv（无 Docker）

需要 **Python 3.11–3.13**。先确认 `python3 --version` ——Ubuntu 22.04 自带的是 3.10，不够；
用 24.04+/Debian 12+，或先装 deadsnakes 等新版本并把下面的 `python3` 换成对应命令名。

```bash
sudo apt install -y git python3 python3-venv python3-pip libxml2-dev libxslt1-dev build-essential

cd /opt && sudo git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e ".[all]"
sudo cp .env.example .env && sudo nano .env

# 创建运行用户 + 数据目录（unit 里的 User=newsflow 和
# ReadWritePaths=…/data 都依赖这两步，缺一服务起不来）
sudo useradd --system --no-create-home --shell /usr/sbin/nologin newsflow
sudo mkdir -p /opt/NewsFlow-Bot/data
sudo chown -R newsflow: /opt/NewsFlow-Bot/data

# 注意：.env 里注释必须独占一行。systemd 的 EnvironmentFile 不剥行内
# 注释（Docker 会剥），`KEY=60  # 说明` 会让值变成 "60  # 说明" 直接
# 崩在配置校验。仓库自带的 .env.example 已按此要求整理。
# .env 里有 token，可以 `sudo chmod 600 .env`：systemd 以 root 读它、作为环境变量
# 交给进程，bot 自己读不到这个文件时跳过它，启动日志告警一次，照常运行。

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

### 7.3 升级到新版本

```bash
cd ~/NewsFlow-Bot
git pull                                            # 更新 compose / 配置 / 文档
docker compose -f docker/docker-compose.yml pull    # 拉取新的预构建镜像
docker compose -f docker/docker-compose.yml up -d   # 用新镜像重建容器
# alembic 迁移会在启动时自动跑，无需手工操作
```

> 想用自己改过的源码本地构建、而不是拉预构建镜像？用 `make docker-up-local`（它会 `docker build` 后用 `NEWSFLOW_IMAGE=newsflow-bot:latest` 起容器）。

### 7.4 备份

```bash
# SQLite 模式：在线生成一份一致的单文件快照，不用停机。
# 它连 WAL 里还没合并回主库的写入一起带上。
docker compose -f docker/docker-compose.yml exec newsflow python -c "import sqlite3; sqlite3.connect('/app/data/newsflow.db').execute(\"VACUUM INTO '/app/data/backup.db'\")"
docker cp newsflow-bot:/app/data/backup.db ./backup-$(date +%F).db
docker compose -f docker/docker-compose.yml exec newsflow rm /app/data/backup.db

# Postgres 模式（pg_dump 自带一致性，无需停机）
docker compose -f docker/docker-compose.yml exec postgres pg_dump -U newsflow > backup-$(date +%F).sql
```

> **只拷 `newsflow.db` 一个文件不是备份。** 数据库开着 WAL，最近提交的写入可能只在旁边的 `newsflow.db-wal` 里，停机也不会把它合并回主库。只拿主库文件恢复，最近的订阅与设置改动会丢失，已投递的条目会再推一遍。
> 想停机拷文件，就把整个数据目录连同 `-wal` / `-shm` 一起拷走：`stop` 之后 `docker cp newsflow-bot:/app/data ./backup-$(date +%F)`，恢复时整目录放回原处。Docker 部署的数据在 compose 声明的命名卷里（声明名 `newsflow-data`，实际名字带项目前缀，默认是 `docker_newsflow-data`，以 `docker volume ls` 为准），不在宿主的 `data/` 下。

### 7.5 健康检查详解

Docker 镜像的 HEALTHCHECK 每 60s 扫描 `/app/data/heartbeat/` 目录 —— **`dispatch` 文件不存在，或任一文件超过 120 分钟未更新，就算 unhealthy**。每个长期运行的任务各自负责刷新自己的文件：

| 文件 | 来源 | 刷新频率 |
|---|---|---|
| `dispatch` | `run_dispatch_loop` 开始第一轮之前，之后每轮 `dispatch_once` 末尾 | 每轮抓取完（`FETCH_INTERVAL_MINUTES`） |
| `cleanup` | `run_cleanup_loop` 的心跳节拍 | 每 5min（清理本身按 `CLEANUP_INTERVAL_HOURS` 跑） |
| `digest` | `run_digest_loop` 每轮末尾 | 每 `DIGEST_CHECK_INTERVAL_MINUTES`（默认 5min） |
| `discord` / `telegram` / `webhook` | `run_platform_monitor` 看到该 adapter 的 `is_connected()` 为真 | 每 30s |

平台文件只在真连着时刷新：Discord 从网关断开到重连成功之间不刷新（discord.py 的 `is_ready()` 在重连期间一直为真，所以看的是网关的连接 / 断开事件）；
Telegram 的轮询因 token 被撤销而停掉后不再刷新（PTB 这时 `updater.running` 仍为真，所以连轮询任务是否还活着一起看）。
`dispatch` 在第一轮之前先写一次：第一轮就卡死时，它照样在 120 分钟后变旧。

**120 分钟的阈值和抓取间隔绑在一起**：`dispatch` 每轮才刷新一次，`FETCH_INTERVAL_MINUTES` 调到 100 以上
（再加一轮本身的耗时），它就会周期性超过阈值，容器被判 unhealthy。要用长间隔，在 compose 里把阈值一起放宽：

```yaml
services:
  newsflow:
    healthcheck:
      # 240 = 阈值分钟数，要大于抓取间隔加一轮耗时；其余参数沿用镜像
      test: ["CMD-SHELL", "test -f /app/data/heartbeat/dispatch && ! find /app/data/heartbeat -type f -mmin +240 | grep -q ."]
```

查看各任务 heartbeat 状态：

```bash
docker exec newsflow-bot ls -la /app/data/heartbeat/
```

### 7.6 部署后在线改配置（`.env` 的正确姿势）

**短答**：改 `.env` 不是实时生效的，而且 `docker compose restart` **不会**重读 `.env`——必须用 `docker compose up -d`。

#### 为什么 restart 不够

`docker-compose.yml` 里的 `env_file: ../.env` 只在 **`up` 阶段**被读取一次，把每一行 `KEY=VALUE` 作为环境变量写进容器的配置快照。之后：

- `docker compose restart` = 对同一个容器做 stop + start，用的是早先存下的那份快照。磁盘上的 `.env` 改了也不会再读。
- `docker compose up -d` = 重新解析 compose 配置（包括 `env_file` 和 `${VAR:-default}` 替换）。如果发现值变了，compose 会 **recreate** 容器——实际是停旧容器 + 起新容器，新的带最新 env。

容器内部的 pydantic-settings `get_settings()` 用了 `@lru_cache`，Python 进程内只会初始化一次。即便挂载了 `.env` 文件也不会热 reload。所以想让新值生效，必须让 Python 进程**重新启动**，而让进程重启又必须让**容器 recreate**。

#### 标准工作流

```bash
cd ~/NewsFlow-Bot

nano .env           # 改配置

docker compose -f docker/docker-compose.yml up -d newsflow
#   注意是 up -d，不是 restart
#   compose 会打印 "Recreating newsflow-bot" 然后停旧起新，~10-30 秒停机
#   RSS 条目靠 GUID 去重，跨越这段停机不会漏推也不会重推

docker compose -f docker/docker-compose.yml logs -f newsflow    # 观察新值确实进了
```

#### 三种常见操作对照

| 目标 | 命令 | 重读 `.env`？ | 容器进程重启？ |
|---|---|---|---|
| 改 `.env` 的值 | `docker compose up -d newsflow` | ✅ | ✅（仅当值变了） |
| 排查时重启进程（不改配置） | `docker compose restart newsflow` | ❌ | ✅ |
| 升级到新版本（拉预构建镜像） | `docker compose pull newsflow && docker compose up -d newsflow` | ✅ | ✅（镜像变了就重建） |

#### 哪些配置可以随时在线改

**零风险，推荐在线调**：

- `FETCH_INTERVAL_MINUTES`（调到 100 以上要同时放宽 HEALTHCHECK，见 §7.5）/ `CLEANUP_INTERVAL_HOURS`
- `ENTRY_RETENTION_DAYS`：必须小于 `SENT_ENTRY_RETENTION_DAYS`，否则启动即报错退出；不大于 7 会告警（周报窗口里最早的文章会先被清掉）
- `LOG_LEVEL=DEBUG`（临时排错）/ `LOG_FORMAT=json`
- `TRANSLATION_SYSTEM_PROMPT` / `DIGEST_SYSTEM_PROMPT`（自定义 AI 提示词，见 §3.3）
- `DIGEST_MAX_INPUT_CHARS_PER_ARTICLE` / `DIGEST_MODEL` / `OPENAI_MODEL` / 两个 `*_REASONING_EFFORT`（换模型时一起看它接不接受当前的 effort）
- `CACHE_BACKEND` 在 memory ↔ redis 之间切换（需要先把 Redis 容器起起来：`--profile with-redis up -d`）

**需要注意副作用**：

- 新加 `TELEGRAM_TOKEN`（从单平台变双平台）：`up -d` 后 Telegram 也会启动，第一次要等 slash command 同步
- 新开 `TRANSLATION_ENABLED=true` + `OPENAI_API_KEY=xxx`：镜像里 `[all]` extras 已含 openai 包，直接 `up -d` 就行
- `API_ENABLED=false → true`：FastAPI 会启动；写端点需 `API_KEY`（未配则一律 503，fail-closed），读端点在配 `API_KEY` 之前开放；CORS 默认不发（`API_CORS_ORIGINS` 白名单开启），公网暴露建议再套 nginx 反代
- `API_PORT`：Docker 下容器内端口固定 8000（compose 钉死，避免映射错位），`.env` 里改它不生效——想换宿主机端口用 `API_PORT=9000 docker compose -f docker/docker-compose.yml up -d`（shell 变量控制端口映射）。裸机运行则 `.env` 的 `API_PORT` 正常生效。compose 默认把端口发布在 `127.0.0.1`（VPS 上 0.0.0.0 就是公网）；要远程访问请上反向代理（走 compose 网络，不需要宿主端口），或自行改 compose 里的绑定地址
- `config/webhooks.yaml` / `config/sources.yaml` 内容变化：**不用重启**——两个 YAML 支持热重载：`POST /api/admin/reload`（带 API_KEY；需 `API_ENABLED=true`），或对进程发 `SIGHUP`（`docker kill -s HUP newsflow-bot` / `systemctl kill -s HUP newsflow`；Unix 才有这个信号，Windows 上没有）。重载跑的就是启动时那套幂等 sync + 刷新 webhook 目的地缓存；**文件解析失败时保持旧状态**（不像启动时直接中止），错误从 API 响应 / 日志返回；数据库出错时响应只给错误类型，完整信息在日志里（SQL 报错会带出参数）。例外：**启动时还没有 `webhooks.yaml`、运行中才新建的**，热重载会把它入库，但 webhook 投递要重启后才开始，响应里会写明。当然 `up -d` 重启也照样生效

**需要换镜像**（仅改 `.env` 不够，得有新镜像才生效）：

- 升级到新发布版本 → 拉预构建镜像：`docker compose pull newsflow && docker compose up -d newsflow`
- 自己改了源码（`src/` 下的 `.py`）/ `pyproject.toml` 依赖 / Python 版本 → 本地构建：`make docker-up-local`
- 因为这些东西是 build 时烧进镜像的，`env_file` 不管它们

#### systemd 部署下的等价操作

如果你用 §7.2 的 systemd + venv 模式，对应动作是：

```bash
nano .env
sudo systemctl restart newsflow     # systemd 的 EnvironmentFile 指令会在每次 start 时重读
```

systemd 的 `EnvironmentFile=` 和 docker-compose 的 `env_file:` 语义不同——前者**每次 start 都重读**，所以 `systemctl restart` 在 systemd 下是 OK 的。Docker 的坑是 compose 层把 env 缓存到了容器 config 里。

#### 部署前离线校验：`checkconfig`

```bash
make checkconfig            # 或: python -m newsflow.checkconfig
```

Docker 部署要在容器里跑：宿主上跑读的是宿主的默认路径，看不到挂进容器的 `./config/` 与 compose 注入的环境变量，配置有错也会报 OK。

```bash
docker compose -f docker/docker-compose.yml run --rm --no-deps newsflow python -m newsflow.checkconfig
```

不碰网络、不碰数据库，验证三样东西：`.env` 能否通过 pydantic 校验（含行内注释污染、越界值）、两个 YAML 是否通过严格 schema（**未知字段现在是硬错误**——`secert:` 这类拼写错误从"静默失效"变成启动中止，所以改完配置先跑这个）、以及跨文件引用（sources.yaml 里 `platform: webhook` 的订阅者指向的 destination 必须真的在 webhooks.yaml 里声明过，启动与热重载同样拒绝）。两个 YAML 里会入库的值还按存储列宽校验，`language` 必须是语言码、会规范成命令里的写法（`zh-cn` → `zh-CN`）。另有几类只告警不报错的情况，启动日志里也会以 `Config:` 开头打出同样的内容：环境变量或 `.env` 里和某个配置项只差一个拼写的键（如 `DATABASE_URI`——它会被静默忽略，配置项保持默认值）、会让周报丢素材或让条目重推的保留期组合、开了翻译却没配 key 等。退出码 0 = 可部署；1 = 有错误，逐条列出。CI/部署脚本里当门禁用。

### 7.7 出网限制：URL 校验挡不住的那一类

bot 在抓取前会校验 feed URL（拒绝非 http(s) scheme、私网 / 回环 / 链路本地 / 云元数据段的 IP 字面量、超长 URL），重定向的每一跳也重新校验。它只看 URL 字面量，挡不住一个在**抓取时**才解析到私有 IP 的主机名（DNS rebinding 或恶意 DNS）——这一层有意不做，靠部署侧的出网限制来兜。**部署到能访问内网的环境时，这条是你要自己补的**：

- 容器 `--network` 限制到没有内网路由的网络命名空间
- 或者在 firewall / iptables 层拦截到私网段的出站

### 7.8 常见问题

**加了订阅但没收到第一条消息。** 先看订阅是怎么建的：

- `/add`、Discord `/feed add`：几秒内应收到最新一条作为预览。**没收到不是正常现象**——源可能还没有任何条目，或者发送失败了：查 Telegram `/info <url>` / Discord `/feed status <url>`，日志里搜这个源的标题或 URL。
- OPML 导入、`POST /api/subscriptions`：最新一条在下一轮抓取后送达。
- YAML 声明的订阅：只推订阅之后发表的文章，等源发新文章再加一轮抓取间隔才有第一条，这是预期行为。

想手动触发一轮：API 启用时调 `POST /api/feeds/<id>/refresh`，或者用 `FETCH_INTERVAL_MINUTES=1` 启动测试。

**删一个 feed 的所有数据。** 直接删 `feeds` 表里那一行：`feed_entries` 与 `subscriptions` 都级联自 `feeds.id`，`sent_entries` 级联自 `subscriptions`，一条 `DELETE FROM feeds WHERE id=?` 沿链清掉条目、订阅与已发记录。

**强制重新翻译。** 把 `feed_entries` 里的 `title_translated` / `summary_translated` / `translation_language` 清成 `NULL`，同时清服务缓存（Redis `FLUSHDB`，或重启进程清掉内存缓存），下轮 dispatch 会重翻。
