# 定时资讯 API 参考（辅助模式）

所有接口无鉴权，适合本地/内网。协议 HTTP + JSON，UTF-8；请求体带 `Content-Type: application/json`（上传接口除外）。

- Base URL：Docker 默认宿主 `http://localhost:18080`（容器内 `8000`）；直接启动默认 `http://localhost:8000`。
- 在线文档：`GET /docs`、`GET /openapi.json`。
- 健康检查：`GET /health` → `{status, app, version, work_mode, features}`。
- 反向代理子路径：平台支持 `APP_BASE_PATH`；如果部署在 `/proxy/8000/` 下，所有路径前缀相应用替换。

> 辅助模式下平台不运行内置 Agent：不会自动采集、不会调用 LLM、不会生成简报复盘。所有内容由外部 Agent 写入。

---

## 1. 辅助模式专用

### GET /api/assist/status

一站式状态恢复 / 健康检查。

响应字段：

| 字段 | 说明 |
|------|------|
| `work_mode` | `assist` / `autonomous` / 空 |
| `is_assist` | `work_mode == "assist"` |
| `skill_version` | 平台内置的 Skill 版本 |
| `server_time` | 服务器本地时间（ISO 8601） |
| `onboarded` | 是否已完成外部 Agent 初始化 |
| `onboarding` | `memory/ASSIST.json` 的内容：`onboarded_at`、`agent`、`schedule`、`editorial`、`tags`、`memory_saved`、`updated_at` |
| `nickname` | 平台昵称 |
| `base_url` | 本次请求的根地址 |
| `stats` | `total` / `today` / `read` / `unread` / `fav` / `later` / `tagged` / `untagged` |
| `latest_batch` | `{id, count, fallback}`；`fallback` 为 `last_24h` 表示没有批次号，回退最近 24 小时 |
| `latest_news` | `{id, title, source, collected_at, batch_id}` 或 `null` |
| `tags.followed` | 关注标签数组 |
| `tags.top` | 近 30 天热门标签 `{tag, count, color, last_collected}` |
| `brief` | `{exists, date, source, updated_at}` |
| `feedback` | `{pending, items}`，`items` 为最近 5 条反馈 |
| `memory` | `{enabled, exists, updated_at, path}` |
| `scheduler` | `{running, next_run, last_skip}`；辅助模式通常 `next_run: null` |
| `note` | 当前模式的提示 |

### POST /api/assist/onboarding

写入首次访谈结果。仅在 `work_mode == "assist"` 时可用；否则返回 `409`。

请求：

```json
{
  "version": 1,
  "agent": "OpenClaw",
  "nickname": "小明",
  "sections": {
    "profile": "- 关注 AI 大模型、芯片、国际局势。",
    "interests": "- AI 大模型\n- 半导体",
    "watch": "- 跟踪英伟达与 OpenAI",
    "preferences": "- 每天早晚各看一次；每次 10-15 条",
    "avoid": "- 娱乐八卦\n- 营销软文",
    "notes": "- 采集节奏：08:00 / 20:00；简报 21:00"
  },
  "tags": ["AI大模型", "半导体", "地缘政治"],
  "schedule": {
    "timezone": "Asia/Shanghai",
    "times": ["08:00", "20:00"],
    "items_per_run": 12,
    "brief_time": "21:00"
  },
  "editorial": {
    "language": "zh-CN",
    "depth": "deep",
    "article_length": [600, 900],
    "summary_length": [60, 150],
    "cover": "off",
    "brief_style": "five-section"
  },
  "replace_sections": false
}
```

| 字段 | 限制 / 行为 |
|------|-------------|
| `agent` | ≤80 字，仅记录 |
| `nickname` | ≤20 字；仅当平台当前昵称为空或形如 `User123456` 时写入，不覆盖自定义昵称 |
| `sections` | 只接受 `profile` / `interests` / `watch` / `preferences` / `avoid` / `notes`；未知键返回 400；总值 ≤30000 字 |
| `tags` | ≤20 个；经平台归一化（去 `#`、去固定泛词、30 字上限），增量关注，全部生效；不取消已有标签 |
| `schedule` / `editorial` | 原样存入 `memory/ASSIST.json`，平台不调度、不校验字段 |
| `replace_sections` | `false`（默认）追加并去重；`true` 覆盖对应段落 |

响应：

```json
{
  "ok": true,
  "work_mode": "assist",
  "onboarded_at": "2026-10-30T08:00:00",
  "tags": ["AI大模型", "半导体", "地缘政治"],
  "nickname": "小明",
  "memory_saved": true,
  "memory_error": "",
  "state": { "...": "memory/ASSIST.json 内容" },
  "base_url": "http://localhost:18080"
}
```

- `memory_saved: false` 表示记忆系统未启用或写入失败；外部 Agent 应继续用自己的存储保存画像，并告知用户。
- 该接口会把 `ONBOARDING_DONE=true` 写入 `.env`，抑制平台自己的首次向导。
- 省略 `tags` / `schedule` / `editorial` 时保留上一次的状态；`tags` 只增不删（取消关注用 `/api/tags/follow`）。
- 完全没有可保存内容（没有 sections/tags/schedule/editorial/nickname）时返回 `400`，不会把用户误标为已初始化。
- `DELETE /api/assist/onboarding` 清除 `memory/ASSIST.json`，不删除 `PROFILE.md` 与关注标签；下次可重新访谈。

### GET /api/assist/feed

辅助模式资讯墙数据：按时间窗口返回新闻 + 标签统计，**不依赖采集批次**。

| 参数 | 默认 | 限制 | 说明 |
|------|------|------|------|
| `days` | 7 | 1-30 | 回看天数；1 = 近 24 小时 |
| `limit` | 300 | 1-300 | 最多返回条数 |

响应：

```json
{
  "window_days": 7,
  "total": 42,
  "items": [ { "id": 1, "title": "...", "tags": ["AI芯片"], "cover_url": "...", "collected_at": "...", "is_read": 0, "credibility": 86, "match": "window" } ],
  "tags": [{ "tag": "AI芯片", "count": 12, "color": "#d4a853", "followed": true }],
  "followed": ["AI芯片"],
  "batch_id": "20261030T0800-a1b2",
  "batch_count": 12
}
```

`items` 使用检索条目形状，**不含** `content` / `analysis`；需要全文时逐条 `GET /api/news/{id}`。

---

## 2. 新闻

### POST /api/news — 推送新闻

请求（`NewsCreate`）：

| 字段 | 必填 | 限制 | 说明 |
|------|------|------|------|
| `title` | 是 | 1-255 字 | 标题 |
| `summary` | 否 | 入库清洗后 ≤300 字 | 概要；不传时平台会从正文提取短摘要 |
| `content` | 否 | 文本 | Markdown 正文 |
| `source_url` | 否 | ≤500 字 | 原文链接；只接受 `http://` / `https://` |
| `source` | 否 | ≤100 字 | 媒体名称 |
| `cover_url` | 否 | ≤500 字 | 封面；只接受 `http(s)://` 或 `/images/` 本地路径（推荐 `POST /api/media` 返回的 `/images/uploads/...`） |
| `collected_at` | 否 | ISO 8601 | **发现时间**；带 `Z`/偏移会按平台 `APP_TIMEZONE` 转成服务器本地时间，不传为当前时间。不要用原文发布时间 |
| `batch_id` | 否 | ≤40 字 | 本轮批次号；同轮所有条目共用。强烈建议传 |
| `tags` | 否 | 数组 | 平台归一化：最多 8 个、每个 ≤30 字、去泛词、去重 |
| `credibility` | 否 | 0-100 | 来源可信度 |
| `analysis` | 否 | 对象 | `key_points` / `topics` / `why_it_matters` / `entities`；原样存储，不做 schema 校验 |

响应 `201`：完整 `NewsResponse`，包含 `id`、`short_summary`、`tags[]`、`batch_id`、`cover_url`、`created_at`、`updated_at`、`is_read`、`is_fav`、`is_later` 等。

封面归档规则：

- `cover_url` 以 `/images/uploads/` 开头且文件真实存在 → **复制**到 `images/cover/{新闻id}.{ext}`，返回该路径；源文件保留；
- 也兼容 `/images/cover/` 下唯一文件名（纯数字文件名、`cover/default/`、`images/sign.png`、`images/avatar/` 会被拒绝）；
- 扩展名只保留 `png/jpg/jpeg/webp/gif`，其它后缀按 `.png` 处理（不会转换文件内容）；
- 文件不存在、路径不在允许范围、或外链 → 保留原 `cover_url`，前端兜底；
- 平台**不会**下载外链图片。远程 Agent 请用 `POST /api/media`。

> 平台在 `POST /api/news` 上**不**做去重、质量闸门、可信度评分或原文抓取。这些必须由外部 Agent 完成。

### GET /api/news — 列表

| 参数 | 默认 | 说明 |
|------|------|------|
| `page` | 1 | 页码 |
| `page_size` | 10 | 1-100 |
| `keyword` | — | 对 title / summary / content 做 AND 搜索；空格/逗号/分号/顿号分隔 |
| `tag` | — | 标签精确匹配（经归一化） |
| `batch` | — | `latest` 或具体批次号 |
| `today_only` | false | 服务器本地日期当天 |
| `date_filter` | — | `YYYY-MM-DD` |
| `is_read` | — | `0` / `1` |

响应：`{total, items:[NewsResponse]}`。

排序：未读且 `collected_at >= 昨天` → 0；已读且 `>= 昨天` → 1；其余 → 2；同组按 `collected_at desc`。带 `keyword` 时先按关键词命中数排序。

`batch=latest` 取最近的非空 `batch_id`。如果库里有旧批次而外部推送没有 `batch_id`，外部新闻不会出现在该过滤里——所以请始终传 `batch_id`。

### GET /api/news/check — 推送前查重

| 参数 | 说明 |
|------|------|
| `url` | 原文链接（可选） |
| `title` | 标题（可选） |
| `days` | 回看天数，默认 30，1-365 |

`url` 与 `title` 至少一个。响应：

```json
{"exists": true, "match": "url", "news_id": 12, "title": "…", "score": 1.0, "checked": 320, "truncated": false}
```

`match` 为 `url` / `title` / 空；标题阈值沿用 `AGENT_TITLE_DUP_THRESHOLD`（默认 0.82）。精确 URL 会查全库；标题在 `days` 窗口内最多扫描最近 2000 条，`truncated: true` 表示窗口内还有更早的条目未扫描。`exists: false` 且 `truncated: false` 时可推送。

### GET /api/news/{id}

返回完整新闻对象（含 `content`、`analysis`、`related_ids`、阅读/收藏状态）。不存在或已删除 → `404`。

### PATCH /api/news/{id} — 更正

只传需要改的字段；未传字段保持不变。可改：`title`、`summary`、`content`、`source_url`、`source`、`cover_url`、`tags`、`credibility`、`analysis`、`related_ids`。

- `tags` 变化会同步标签索引；
- `cover_url` 传本地 `/images/...` 时会重新归档；
- 返回更新后的完整 `NewsResponse`。
- 更正正文后，若平台配置了 embedding，可稍后调用 `POST /api/retrieval/reindex` 让向量检索跟上；辅助模式通常不配置向量模型，可忽略。

### DELETE /api/news/{id} — 逻辑删除

把 `is_deleted` 置 1；列表、检索、统计、首页不再展示。数据保留用于审计。返回 `{ok:true, id, deleted:true}`。

### 阅读状态

```
POST /api/news/{id}/read       # 已读；会清掉“稍后再读”
POST /api/news/{id}/reading    # 在读；仅未读时生效
POST /api/news/{id}/fav        # 切换收藏；不是 set
POST /api/news/{id}/later      # 切换稍后再读；不是 set
```

`fav` / `later` 是 **toggle**，重试会翻回去。需要确定状态时先 `GET /api/news/{id}`。

### 相关新闻

`GET /api/news/{id}/related?limit=10` → `{total, items}`。新闻不存在时返回 200 + 空列表。

### 个人列表

```
GET /api/my/favorites   # 全部收藏，按 fav_at desc，无分页
GET /api/my/later       # 全部稍后再读，按 later_at desc，无分页
GET /api/my/counts      # {fav_count, later_count}
```

---

## 3. 首页 / 标签 / 统计

### GET /api/home

| 参数 | 默认 | 说明 |
|------|------|------|
| `limit` | 60 | 1-200 |
| `include_recommendations` | true | 传 `false` 可跳过推荐计算 |

响应：`{batch_id, batch_count, items, tags, followed, recommendations, recommend_hint}`。

- `items` 是上一批新闻（无批次时回退最近 24 小时），形状同 `/api/assist/feed` 的条目。
- `recommendations` 是确定性打分（结合阅读/收藏/反馈/记忆），不是 LLM；辅助模式可关闭。
- 辅助模式首页推荐用 `/api/assist/feed`，不要依赖批次。

### GET /api/tags

| 参数 | 默认 | 说明 |
|------|------|------|
| `batch_id` | 最近批次 | 精确批次；不传时先取最近非空批次，没有批次才回退最近 30 天 |
| `limit` | 40 | 1-200 |

响应：`{batch_id, items:[{tag,count,color,last_collected?,followed}], followed:[...]}`。关注但不在统计里的标签会以 `count:0` 追加。

### POST /api/tags/follow

```json
{"tag": "AI芯片", "follow": true}
```

返回 `{ok, tag, followed}`。标签归一化后为空 → `400`。取消不存在的关注也返回成功。

### GET /api/stats

返回 `total`、`read_count`、`unread_count`、`today_count`、`tag_stats`、`daily_stats`（近 30 天）、`recent_reads`、`recent_reading` 等。辅助模式可用于反馈复盘。

---

## 4. 每日简报

平台每天只有一篇统一简报，固定存放在 `category_name = "每日"`。

### POST /api/brief

```json
{
  "tag": "每日",
  "date": "2026-10-30",
  "content": "# 每日简报 · 2026-10-30\n\n…",
  "note": "可选便签",
  "source": "外部"
}
```

- `tag` 必须传 `每日`；其它值会写入旧表结构但简报页不展示。
- 同一天已存在时更新 `content`，`note` 传了才更新，`source` 默认 `外部`。
- `source` 只接受 `自主` / `外部` / `用户`，其它值归一为 `外部`。
- 返回 `201` 及保存后的简报。

### 读取

```
GET /api/brief/daily?date=YYYY-MM-DD   # 不传 date 取最新一期；不存在 404
GET /api/brief/daily/dates              # {dates:[...]}
GET /api/brief/note?tag=每日&date=...   # 读取便签
PUT /api/brief/note                     # 写入便签 {tag,date,content}
```

「今天」用服务器本地日期。默认时区 `Asia/Shanghai`，可用 `APP_TIMEZONE` 调整。

---

## 5. 记忆与反馈

### GET /api/memory

返回 `{enabled, content, sections, profile, feedback, feedback_pending, meta}`。

`sections` 固定键：

| 键 | 标题 | 用途 |
|----|------|------|
| `profile` | 用户画像 | 身份、职业、长期兴趣 |
| `interests` | 兴趣与关注 | 主题、公司、人物、产品 |
| `watch` | 跟踪任务 | 明确要持续跟踪的事 |
| `preferences` | 阅读偏好 | 深度、篇幅、时段、来源、语言 |
| `avoid` | 需要避免 | 不想看的内容、低质来源 |
| `feedback` | 反馈与整改 | 平台反馈链路维护；外部 Agent 不要写 |
| `notes` | Agent 笔记 | 运行状态、约定、变更记录 |
| 自动区块 | 行为画像（自动） | 平台每 6 小时重建；不要手改 |

`profile` 字段包含 `totals`、`tags`、`sources`、`topics`、`recent_reads`、`daily_reads`、`reading_hours`、`feedback` 等（没有 `categories`）。

### PATCH /api/memory — 推荐

```json
{
  "mode": "append",
  "sections": {
    "watch": "- [2026-10-30] 开始跟踪国产 GPU 进展",
    "preferences": "- 正文控制在 600-900 字"
  }
}
```

- 可写：`profile` / `interests` / `watch` / `preferences` / `avoid` / `notes`；
- `append`（默认）追加并去重；`replace` 覆盖该段落；
- 每行会被规范成 `- ` 开头并截到 300 字；长偏好请拆成多行；
- 总内容 ≤30000 字；不会动 `feedback` 与自动画像块。

### PUT /api/memory

整篇替换，`{"content":"完整 Markdown"}`。会补回缺失段落与自动区块，但有覆盖用户手写内容的风险；外部 Agent 优先用 `PATCH`。

### POST /api/memory/refresh

强制重建行为画像，并在平台配置了 LLM 时反思未消化反馈。**辅助模式下返回 409**（平台不运行记忆反思 Agent）；外部 Agent 应读取反馈并用 `PATCH /api/memory` 自己更新画像。

### DELETE /api/memory

重置为模板。**危险操作**，除非用户明确要求，不要调用。只重置 `PROFILE.md`，不会删除 `memory/ASSIST.json` 与关注标签；需要重置外部 Agent 初始化时用 `DELETE /api/assist/onboarding`。

### 反馈

```
GET  /api/feedback?limit=20&status=new|noted|processed|failed
GET  /api/feedback/reasons
POST /api/feedback
POST /api/feedback/{id}/ack    # 标记 processed（外部 Agent 处理完调用）
```

`POST /api/feedback`：

```json
{
  "target_type": "news",
  "target_id": 12,
  "conversation_id": null,
  "rating": -1,
  "reason": "shallow",
  "comment": "希望更深入"
}
```

- `target_type`：`news` / `message` / `recommend` / `chat`；
- `reason` 不校验，但建议使用：`irrelevant` / `shallow` / `duplicate` / `outdated` / `inaccurate` / `source` / `too_long` / `too_hard` / `helpful` / `other`；
- 平台会把反馈写入记忆的「反馈与整改」，并影响推荐打分；
- `ack` 把状态改为 `processed`，避免重复消化。

---

## 6. 封面上传

### POST /api/media

`multipart/form-data`，字段名 `file`。

- 支持 PNG / JPEG / GIF / WebP，≤8MB；
- 校验文件头，拒绝伪装成图片的 HTML/脚本；
- 返回：

```json
{
  "ok": true,
  "url": "/images/uploads/5f3c....png",
  "filename": "5f3c....png",
  "size": 18234,
  "content_type": "image/png",
  "note": "把该 url 作为新闻的 cover_url 推送，平台会在入库时复制归档到 /images/cover/{新闻id}.<ext>（源文件保留）。"
}
```

上传后尽快推送；未使用的上传文件会留在 `images/uploads/`，可定期清理。

---

## 7. 检索（只读）

```
GET  /api/retrieval/search?q=英伟达 芯片&limit=8&tag=AI芯片&days=7&mode=hybrid
GET  /api/retrieval/timeline?q=AI&range=过去半个月&tag=AI芯片
GET  /api/retrieval/status
POST /api/retrieval/probe        {"q":"英伟达 芯片","limit":8}
POST /api/retrieval/reindex?limit=5000
```

- `mode`：`hybrid`（默认，关键词 + 向量 RRF）/ `lexical` / `semantic`；
- 未配置 embedding 时，`hybrid` 自动降级为 `lexical`；显式 `semantic` 不会自动降级，可能为空；
- `q` 建议 2-6 个关键词；3 字以上走 FTS5 trigram，只要有一个 2 字词整体回退 LIKE；
- 返回 `{query, mode, terms, sources, items:[{ref,id,title,summary,tags,source,collected_at,is_read,match,score}]}`。

辅助模式通常不需要检索；外部 Agent 可以用它读取平台已有新闻做二次处理。

---

## 8. 配置 / 模式 / Skill

### PUT /api/config

常用字段：

```json
{
  "work_mode": "assist",
  "nickname": "小明",
  "onboarding_done": true,
  "agent_interests_text": "…",
  "agent_schedule_text": "…"
}
```

- `work_mode` 只接受 `assist` / `autonomous` / 空串；其它值 `400`；
- 写入 `.env`，立即生效；内置调度器每 15 秒读取一次，最多 15 秒后停止/恢复；
- 模式切换不会迁移或删除数据。

`GET /api/config` 返回模型配置、搜索能力、任务设置、记忆偏好、`work_mode` 等。辅助模式不需要平台 LLM，不要为此配置模型。

### Skill 分发

```
GET /api/skill                     # 默认 SKILL.md（text/markdown）
GET /api/skill?path=references/api.md
GET /api/skill/manifest            # {name,version,description,entry,files:[{path,size,sha256,url}]}
```

只允许读取 `skill/` 目录内的 `.md/.json/.txt/.py/.yaml/.yml`，单文件 ≤512KB。

### 调度器

```
GET  /api/scheduler/status         # 含 work_mode、running、next_run、last_skip
POST /api/scheduler/trigger        # 辅助模式返回 409
GET  /api/scheduler/trigger/stream # 辅助模式返回 409（SSE）
```

辅助模式下调度器不会执行；`trigger` 也会被拒绝。

### 自主模式专用（辅助模式不要用）

- `/api/chat/*`、`/api/agent/*`、`/api/memory/refresh` 的 LLM 反思、`/api/config/preferences/analyze`、`/api/model-configs/*`、`/api/retrieval/reindex` 的向量化；辅助模式下聊天发送/流式端点会返回 `409`，记忆刷新、偏好分析也会返回 `409`；
- 这些能力依赖平台内的模型配置，辅助模式的设计是外部 Agent 负责。

---

## 9. 错误码

| 状态码 | 含义 | 处理 |
|--------|------|------|
| `400` | 参数错误（空标签、工作模式非法、没有更新字段等） | 读 `detail`，修正请求 |
| `404` | 资源不存在（新闻/简报/反馈） | 确认 ID；可能已删除 |
| `409` | 模式冲突（非辅助模式初始化、辅助模式触发采集） | 确认 `work_mode`，必要时请用户切换 |
| `413` | 上传文件过大 | 压缩到 8MB 以内 |
| `422` | Pydantic 字段校验失败 | 检查类型、长度、范围 |
| `500` | 服务端异常 | 记录响应体，稍后重试；持续失败请查平台日志 |

网络错误 / 5xx：指数退避重试（1s/3s/9s），最多 3 次。4xx 不要盲目重试。

---

## 10. 数据与限制速查

| 项 | 规则 |
|----|------|
| `title` | 1-255 字 |
| `summary` | 入库清洗后 ≤300 字 |
| `short_summary` | 派生字段，约 140 字 |
| `source_url` / `cover_url` | ≤500 字 |
| `source` | ≤100 字 |
| `tags` | 平台保留最多 8 个，每个 ≤30 字；固定泛词（新闻/资讯/热点/最新/综合/头条/要闻/推荐/文章/消息/动态/其他）会被丢弃，科技/财经等宽泛主题会保留 |
| `batch_id` | ≤40 字 |
| `credibility` | 0-100 |
| 上传图片 | PNG/JPEG/GIF/WebP，≤8MB |
| 记忆文件 | ≤60000 字（`AGENT_MEMORY_MAX_CHARS`） |
| 每日简报 | 一天一篇，`category_name="每日"` |
| 软删除 | `DELETE /api/news/{id}` 置 `is_deleted=1`，数据保留 |
| 时区 | 平台默认 `Asia/Shanghai`；`today_only` 与简报日期用服务器本地日期 |
| 鉴权 | 默认无；公网暴露前加反向代理认证 |
