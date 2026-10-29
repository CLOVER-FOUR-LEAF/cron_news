---
name: cron-news
description: 与「定时资讯」新闻聚合平台对接。当需要向该平台推送新闻、查询新闻、关注标签、写入或读取每日简报、读取统计数据或维护模型配置时使用本技能。适用场景：外部 Agent（如 OpenClaw、Hermes）作为「资讯助手」为平台采集新闻、生成封面图、撰写每日简报，或从平台读取已有资讯做二次处理。平台处于「辅助模式」时，采集、封面、简报等职责全部交给外部 Agent 完成。
---

# 定时资讯 · 对接技能

「定时资讯」是一个 AI 新闻聚合展示平台，提供「辅助」与「自主」两种工作模式：

- **自主模式**：平台内置 Agent 自动联网采集、撰稿、生成封面与每日简报，外部 Agent 无需介入。
- **辅助模式**：平台作为展示与数据中枢，**采集新闻、生成封面图、撰写每日简报等任务全部由外部 Agent 完成**。本技能即服务于该模式，教你如何通过 REST API 与平台完美协作。

## 基础信息

- **协议**：HTTP + JSON，所有接口无需鉴权（本地/内网部署）。
- **Base URL**：默认 `http://localhost:18080`（Docker 部署宿主端口，容器内为 8000），以实际部署地址为准。
- **字符编码**：UTF-8，请求体需带 `Content-Type: application/json`。
- **在线文档**：`{Base URL}/docs`（Swagger UI）、`{Base URL}/openapi.json`（OpenAPI 规范）。
- **健康检查**：`GET /health` → `{"status":"ok", ...}`，可用于探活。

## 核心概念

- **标签（Tag）**：新闻唯一的组织维度。一条新闻可以有多个标签，由采集 Agent 在结构化分析时自由生成；用户可关注标签，搜索 Agent 会优先围绕关注标签采集。分类已彻底移除。
- **关注标签（TagFollow）**：用户主动关注的标签，采集与推荐会优先覆盖，但不是硬性过滤。
- **采集批次（batch_id）**：每轮采集生成一个批次号；首页展示「上一批」新闻及其全部标签，「为你推荐」也从上一批中挑选。
- **新闻（News）**：一条资讯，含标题、短摘要、Markdown 正文、来源、封面、标签、可信度、结构化分析、收录时间与阅读状态。
- **每日简报（Brief）**：按日期的一条统一简报记录（`tag` 固定为「每日」），含正文 `content`、配套便签 `note` 与来源 `source`（自主/外部/用户）。
- **来源（source）**：简报来源标记；外部 Agent 写入简报时传 `外部`。

---

## 标签接口

### 标签总览

```
GET /api/tags?batch_id=20261029...
```

响应：

```json
{
  "batch_id": "20261029160714123456",
  "items": [
    { "tag": "AI芯片", "count": 12, "color": "#d4a853", "followed": true },
    { "tag": "地缘政治", "count": 8, "color": "#4a8fd4", "followed": false }
  ],
  "followed": ["AI芯片"]
}
```

- `batch_id` 省略时返回最近 30 天标签；带批次号时只统计该批新闻。
- 一条新闻可计入多个标签，`count` 是包含该标签的不同新闻数。

### 关注 / 取消关注

```
POST /api/tags/follow
Content-Type: application/json

{ "tag": "AI芯片", "follow": true }
```

返回 `{ "ok": true, "tag": "AI芯片", "followed": true }`。关注后采集 Agent 会优先围绕该标签搜索。

### 首页数据

```
GET /api/home?limit=80
```

返回上一批新闻、批次标签、关注标签与个性化推荐：

```json
{
  "batch_id": "20261029...",
  "batch_count": 24,
  "items": [ ...新闻对象... ],
  "tags": [{"tag":"AI芯片","count":12,"color":"#d4a853","followed":true}],
  "followed": ["AI芯片"],
  "recommendations": [ {"id":1,"title":"...","tags":["AI芯片"],"reason":"你常看「AI芯片」","related":[...]} ],
  "recommend_hint": "从上一批 24 条未读中……"
}
```

---

## 新闻接口

### 推送新闻（外部 Agent 最常用）

```
POST /api/news
Content-Type: application/json

{
  "title": "OpenAI 发布新一代模型",
  "summary": "一句话概要，用于列表展示",
  "content": "# 标题\n\n这里是 Markdown 正文……",
  "source_url": "https://example.com/original",
  "source": "来源名称",
  "cover_url": "/images/ext_cover_abc.png",
  "collected_at": "2026-08-07T10:00:00+08:00",
  "tags": ["大模型", "OpenAI"],
  "credibility": 86,
  "analysis": {
    "key_points": ["关键事实 1", "关键事实 2"],
    "topics": ["大模型", "OpenAI"],
    "why_it_matters": "一句话说明为什么值得关注",
    "entities": {"companies": ["OpenAI"], "people": [], "places": []}
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `title` | 是 | 标题，1–255 字 |
| `summary` | 否 | 概要（建议约 100 字），列表页展示 |
| `content` | 否 | Markdown 正文，详情页渲染（建议篇幅充实） |
| `source_url` | 否 | 原文链接 |
| `source` | 否 | 来源媒体名称 |
| `cover_url` | 否 | 封面图 URL |
| `collected_at` | 否 | 收录时间（ISO 8601），不传则为当前时间 |
| `tags` | 否 | 主题标签数组；用于个性化推荐与话题聚合 |
| `credibility` | 否 | 来源可信度 0-100；详情页展示，推荐打分参考 |
| `analysis` | 否 | 结构化分析：`key_points` / `topics` / `why_it_matters` / `entities`；详情页展示要点与「为什么值得关注」 |

成功返回 `201` 及完整新闻对象（含自增 `id`、`tags`、`short_summary`、`cover_url`）。

> **封面图约定（辅助模式封面落盘 → 平台改名）**：
>
> 1. 外部 Agent 先把封面图**写入平台挂载的 `images` 目录**：宿主机部署为项目根 `images/`（容器内为 `/app/images/`），两者同步；最常用路径为 `images/cover/`（如 `/images/cover/外部生成_时间戳.png`），也可放在 `images/` 任意子目录。
> 2. 推送新闻时在 `cover_url` 传对应的本地链接，如 `/images/cover/xxx.png`。
> 3. 平台收到后会把该文件**改名**为 `/images/cover/{新闻id}.{原扩展名}`（如 `/images/cover/508.png`）并回写 `cover_url` 字段——**最终封面名称以新闻 id 命名**，返回的新闻对象里即为最终链接。
> 4. 文件名请确保**唯一**（建议时间戳/随机串），避免与其它新闻的待处理文件冲突。
> 5. **不传** `cover_url` 时：自主模式按开关自动生成/选取默认封面；辅助模式下无封面由前端兜底展示。请勿传外链 URL（平台不会拉取外链图片）。

### 查询新闻列表

```
GET /api/news?page=1&page_size=10&tag=AI芯片&keyword=模型&is_read=0&batch=latest&today_only=false&date_filter=2026-08-07
```

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页，`page_size` 上限 100 |
| `tag` | 按标签筛选 |
| `batch` | `latest` 或具体批次号 |
| `keyword` | 按标题关键词搜索（支持多关键词空格分隔） |
| `is_read` | `0` 未读 / `1` 已读 |
| `today_only` | `true` 仅今日 |
| `date_filter` | 指定日期 `YYYY-MM-DD` |

响应：`{ "total": N, "items": [ ...新闻对象 ] }`。每条新闻对象含 `id`、`title`、`summary`、`short_summary`、`content`、`source_url`、`source`、`cover_url`、`collected_at`、`batch_id`、`is_read`、`is_reading`、`is_fav`、`is_later`、`related_ids`、`tags`、`credibility`、`analysis` 等。

### 获取新闻详情

```
GET /api/news/{id}
```

返回单条新闻对象；不存在返回 `404`。

### 阅读状态与收藏

```
POST /api/news/{id}/read       # 标记已读 → {"message":"ok"}
POST /api/news/{id}/reading    # 标记在读 → {"message":"ok"}
POST /api/news/{id}/fav        # 切换收藏 → {"is_fav": 0|1}
POST /api/news/{id}/later      # 切换稍后再读 → {"is_later": 0|1}
```

### 个人收藏 / 稍后再读 / 计数

```
GET /api/my/favorites     # → { total, items }
GET /api/my/later         # → { total, items }
GET /api/my/counts        # → { "fav_count": N, "later_count": N }
```

### 相关推荐

```
GET /api/news/{id}/related?limit=10
```

返回相关新闻列表（平台开启智能推荐时为 AI 计算的跨标签结果，否则回退为共享标签最新新闻）。

---

## 每日简报接口

> 简报数据在 `briefs` 表，一条记录含 `content`（正文）、`note`（便签）、`source`（来源）。
> **平台每天只有一篇统一简报，固定使用 `tag: "每日"`**：把当日全部新闻汇总成一篇，不再按标签拆分。
> **辅助模式下请由外部 Agent 撰写简报并通过 `POST /api/brief` 写入**（`source` 默认 `外部`），平台负责展示、编辑与便签。

### 新增 / 更新当日简报（外部 Agent 常用）

```
POST /api/brief
Content-Type: application/json

{
  "tag": "每日",
  "date": "2026-08-07",
  "content": "# 每日简报 · 2026-08-07\n\n今日全球市场与科技行业……",
  "note": "可选的配套便签",
  "source": "外部"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `tag` | 是 | 固定传 `每日`（传入其它标签只会写入旧数据，简报页不展示） |
| `date` | 是 | 简报日期 `YYYY-MM-DD` |
| `content` | 否 | Markdown 简报正文 |
| `note` | 否 | 配套便签 |
| `source` | 否 | 默认 `外部` |

- 幂等：同一天已存在时更新 `content`（并保留/更新 `note`、`source`）。
- 成功返回 `201` 及保存后的简报对象。

### 读取简报

```
GET /api/brief/daily?date=2026-08-07
```

- `date` 省略时返回最新一期每日简报。
- 返回：`{ "date", "content", "source", "note" }`；不存在返回 `404`。
- 兼容旧接口：`GET /api/brief/每日?date=...`。

### 有简报的日期（日历圆点）

```
GET /api/brief/daily/dates
```

返回：`{ "dates": ["2026-08-07", ...] }`。

### 便签读写

```
GET /api/brief/note?tag=AI芯片&date=2026-08-07
PUT /api/brief/note
Content-Type: application/json

{ "tag": "AI芯片", "date": "2026-08-07", "content": "便签内容" }
```

---

## 统计接口

```
GET /api/stats
```

返回总数、已读/未读数、今日新增、标签分布、近 30 日趋势、最近阅读/在读等，可用于生成报表。

---

## 模型与服务配置接口

平台的大语言 / 文生图 / 搜索 / 向量服务配置统一入库（`model_configs` 表），API Key 使用 `SECRET_KEY` 加密存储。

```
GET  /api/model-configs?config_type=llm|image|search|embedding   # 列表（含 enabled / has_api_key）
POST /api/model-configs                                          # 新增
PUT  /api/model-configs/{id}                                     # 编辑（api_key 留空表示不修改）
DELETE /api/model-configs/{id}                                   # 删除（已启用配置不可删除）
POST /api/model-configs/{id}/enable                              # 启用（同类型仅一个启用）
POST /api/model-configs/{id}/disable                             # 取消启用
POST /api/model-configs/test                                     # 测试连接（body 同新增字段，不落库）
```

新增 / 测试连接时传入字段：`provider`（服务商名称）、`base_url`、`model_id`、`config_type`（llm/image/search/embedding）、`api_key`。
`embedding` 类型要求服务商提供 OpenAI 兼容的 `POST {base_url}/embeddings`，测试连接会返回向量维度。

---

## 对话与检索接口（Phase 1 + Phase 2）

平台内置对话助手会把「检索本地新闻库 → 读取全文/简报/收藏 → 组织回答」串成工具循环，并在回答中标注来源。外部 Agent 可以直接复用检索能力，或通过非流式接口调用对话。

### 只读检索

```
GET /api/retrieval/search?q=英伟达 芯片&limit=8&tag=AI芯片&days=7&mode=hybrid
GET /api/retrieval/timeline?q=AI&range=过去半个月&tag=AI芯片
GET /api/retrieval/status
POST /api/retrieval/probe        {"q": "英伟达 芯片", "limit": 8}
```

- `mode`：`hybrid`（默认，关键词 + 向量 RRF 融合）/ `lexical` / `semantic`。
- `q` 建议传 2–6 个关键词；3 字以上的词走 FTS5 trigram，2 字短词自动回退 LIKE。
- 返回 `{query, mode, terms, sources:{lexical,semantic}, items:[{ref,id,title,summary,short_summary,tags,source,source_url,collected_at,is_read,match,score}]}`。
- 未配置向量模型时 `mode` 自动降级为 `lexical`，调用方无需处理差异。
- `probe` 同时返回三组结果，适合评估召回差异；`status` 返回 FTS 能力、向量后端、索引条数与待索引数量。

### 索引维护

```
POST /api/retrieval/reindex?limit=5000          # 补齐缺失/过期的新闻向量
POST /api/retrieval/vector/rebuild              # 重建向量派生索引（PostgreSQL）
POST /api/retrieval/vector/ann-index?dim=512    # 为固定维度创建 pgvector HNSW 索引
```

### 非流式对话（脚本/外部 Agent 推荐）

```
POST /api/chat/conversations
Content-Type: application/json
{"title": "可选标题"}

POST /api/chat/conversations/{id}/messages
Content-Type: application/json
{"message": "最近 AI 芯片领域有什么进展？"}
```

返回：

```json
{
  "conversation_id": 1,
  "assistant_message_id": 2,
  "content": "…… [1] ……",
  "citations": [
    {"n": 1, "kind": "news", "news_id": 12, "title": "……", "source": "……", "source_url": "https://……"}
  ],
  "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
  "tool_trace": [{"name": "search_news", "arguments": {"query": "AI 芯片"}, "ok": true, "count": 8}]
}
```

### 流式对话（页面使用）

```
POST /api/chat/stream
Content-Type: application/json
{"conversation_id": 1, "message": "今天科技圈有什么大事？"}
```

SSE 事件：`meta` → `status` → `tool_call` / `tool_result` → `token` → `citation` → `usage` → `done`；异常时返回 `error`。

### 会话管理

```
GET    /api/chat/conversations
GET    /api/chat/conversations/{id}
PATCH  /api/chat/conversations/{id}   {"title": "新标题"}
DELETE /api/chat/conversations/{id}
GET    /api/chat/conversations/{id}/messages?limit=50
```

> 对话接口与平台其它接口采用相同的信任模型（默认无鉴权，适合本地/内网）。暴露到公网前请先加反向代理鉴权或完成 Phase 4 的 Token 鉴权。

## 长期记忆与反馈接口（Phase 2）

五个 Agent（对话/采集/推荐/简报/记忆管家）共用一份 Markdown 记忆（默认 `memory/PROFILE.md`）。外部 Agent 可以读取画像、写入跟踪任务，也可以提交反馈；写入后对话/采集/推荐/简报都会在下一轮读取。

### 读取记忆与画像

```
GET /api/memory
```

返回：

```json
{
  "enabled": true,
  "content": "# 资讯助手记忆\n……",
  "sections": {
    "profile": "- （暂无）",
    "interests": "- [2026-10-28] AI 芯片、大模型（科技）",
    "watch": "- [2026-10-28] 跟踪 AI 新闻（科技）",
    "preferences": "……",
    "avoid": "……",
    "feedback": "……",
    "notes": "……",
    "auto": "- 自动生成于 2026-10-28T12:00:00……"
  },
  "profile": {
    "totals": {"news": 120, "read": 30, "fav": 5, "read_week": 12, "read_rate": 0.25, "avg_dwell_seconds": 48},
    "categories": [{"name": "科技", "read": 18, "fav": 4, "later": 2, "affinity": 0.9}],
    "topics": [{"tag": "AI芯片", "count": 9}],
    "recent_reads": [{"id": 12, "title": "……", "tags": ["AI芯片"]}],
    "feedback": {"negative_reasons": {"shallow": 2}}
  },
  "feedback": [{"id": 3, "target_type": "news", "target_id": 12, "rating": -1, "reason": "shallow", "status": "noted"}],
  "meta": {"enabled": true, "path": "/app/memory/PROFILE.md", "updated_at": "……"}
}
```

### 写入 / 重置记忆

```
PUT    /api/memory          {"content": "完整 Markdown"}
POST   /api/memory/refresh  强制重建行为画像，并让 Agent 反思未消化反馈
DELETE /api/memory          重置为模板
```

外部 Agent 建议只追加/合并，不要覆盖用户手写内容。`POST /api/memory/refresh` 需要大语言模型才能完成反思，否则只刷新自动画像。

### 反馈

```
POST /api/feedback
Content-Type: application/json

{
  "target_type": "news",     // news | message | recommend | chat
  "target_id": 12,           // 新闻 ID 或助手消息 ID；chat 可留空
  "conversation_id": 1,      // message 反馈时建议传
  "rating": -1,              // 1=满意 / 0=中性 / -1=不满意
  "reason": "shallow",       // irrelevant/shallow/duplicate/outdated/inaccurate/source/too_long/too_hard/helpful/other
  "comment": "希望看原文深度报道"
}
```

```
GET /api/feedback?limit=20&status=noted
GET /api/feedback/reasons
```

反馈会：

1. 立即写入 `feedback` 表；
2. 立即追加到记忆的「反馈与整改」，并给出确定性整改建议；
3. 推荐打分对该新闻/来源/标签降权；
4. 后台 Agent 异步反思，归纳后重写画像/偏好/避免段落。

### 外部 Agent 协作建议

- 用户对对话助手说「跟踪 AI 新闻」时，外部 Agent 也可以 `PUT /api/memory` 或通过 `POST /api/feedback` 的上下文同步更新，保证采集侧专注 AI。
- 外部采集 Agent 推送新闻时建议带 `tags`、`credibility`、`analysis`，平台的个性化推荐会直接使用这些信号。
- 反馈写入后如果希望立刻看到反思结果，调用 `POST /api/memory/refresh`。

---

## 辅助模式工作流（外部 Agent 的核心职责）

外部 Agent 在辅助模式下扮演「资讯助手」，典型工作流如下：

### 工作流 A：采集并推送新闻（心跳任务）

每次心跳到来时，检索**上一次心跳之后**的最新资讯，覆盖用户关注的标签（以及一条泛热点查询）：

1. **搜索**：按关注标签检索该时间段内的新闻，确保不遗漏、不重复；入库时自由打多个标签。
2. **整理**：对每条新闻提炼字段：
   - `title`：原文标题
   - `summary`：约 100 字概要
   - `content`：Markdown 正文（篇幅充实，独立可读）
   - `source_url`：原文链接
   - `source`：来源媒体名称
   - `tags`：3-6 个主题标签
   - `cover_url`：封面图。调用文生图模型生成图片后，**先把图片文件写入平台挂载的 `images` 目录**（容器内 `/app/images/`，宿主为部署根目录 `images/`），建议直接放 `images/cover/` 并取唯一文件名（如 `cover_<时间戳>.png`），再传 `/images/cover/cover_<时间戳>.png`；平台会把该文件改名到 `/images/cover/{新闻id}.png`（保留原扩展名）并改写 `cover_url` 字段
   - `collected_at`：发现时间（ISO 8601）
3. **写入**：逐条 `POST /api/news` 入库，记录返回的 `id` 以备追踪。
4. **去重**：推送前用 `GET /api/news?keyword=标题` 检查是否已存在。

### 工作流 B：从平台读取资讯做二次处理

1. `GET /api/news?page_size=50&is_read=0` 拉取未读新闻。
2. 处理完成后 `POST /api/news/{id}/read` 标记已读，避免重复处理。

### 工作流 C：为平台生成每日简报

1. `GET /api/news?today_only=true&page_size=100` 拉取当日全部新闻（可按标签补充筛选）。
2. 按重要性取舍，写成**一篇**覆盖全天要闻的统一简报（导语 → 今日要闻 → 串联与影响 → 值得关注 → 编辑说明）。
3. `POST /api/brief` 写入，`tag` 固定传 `每日`（`source` 默认 `外部`）。
4. 平台简报页自动展示，用户可补充便签。

### 工作流 D：按主题聚合 / 报表

- `GET /api/news?tag=AI芯片&page_size=100` 按标签拉取；`GET /api/home` 获取上一批。
- `GET /api/news?keyword=关键词` 全文检索。
- `GET /api/stats` 生成阅读统计报表。

---

## 错误处理

- `400`：参数错误（如标签为空、标签名过长、反馈原因非法）。响应体 `detail` 字段说明原因。
- `404`：资源不存在（新闻/标签/简报 ID 无效）。
- `422`：请求体字段校验失败（FastAPI 标准格式）。
- 建议：对 `4xx` 读取 `detail` 并向用户/上游反馈；对网络错误做重试。

## 协作约定

- **幂等推送**：平台按标题展示，重复标题会并存；如需去重，推送前先用 `GET /api/news?keyword=标题` 检查。
- **正文格式**：`content` 使用 Markdown，平台详情页会自动渲染。
- **时间**：`collected_at` 建议使用 ISO 8601；列表默认按收录时间倒序、未读优先展示。
- **批量**：逐条 `POST /api/news`；大批量时注意控制并发，避免压垮本地服务。
- **封面**：图片需先落盘到平台挂载的 `images` 目录（宿主 `images/` 与容器 `/app/images/` 同步），再传 `/images/xxx.png`；平台会改名到 `/images/cover/{新闻id}.{扩展名}`。请勿传外链 URL（平台不会拉取外链图片）。
