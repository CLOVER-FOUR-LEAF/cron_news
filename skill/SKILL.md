---
name: cron-news
description: 与「定时资讯」新闻聚合平台对接。当需要向该平台推送新闻、查询新闻、管理分类、写入或读取每日简报、读取统计数据或维护模型配置时使用本技能。适用场景：外部 Agent（如 OpenClaw、Hermes）作为「资讯助手」为平台采集新闻、生成封面图、撰写每日简报，或从平台读取已有资讯做二次处理。平台处于「辅助模式」时，采集、封面、简报等职责全部交给外部 Agent 完成。
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

- **分类（Category）**：新闻的归属维度，系统内置默认分类（`is_default=true`）兜底，预置科技/军事/政治/经济/文化/热点。分类可用名称直接引用。
- **新闻（News）**：一条资讯，含标题、概要、Markdown 正文、来源、封面、收录时间与阅读状态。
- **每日简报（Brief）**：按分类+日期的一条简报记录，含正文 `content`、配套便签 `note` 与来源 `source`（自主/外部/用户）。
- **来源（source）**：简报来源标记；外部 Agent 写入简报时传 `外部`。

---

## 分类接口

### 获取所有分类

```
GET /api/categories
```

响应：

```json
{
  "total": 7,
  "items": [
    { "id": 2, "name": "科技", "sort_order": 6, "color": "#d4a853", "is_default": false },
    { "id": 1, "name": "默认", "sort_order": 0, "color": "#8a8690", "is_default": true }
  ]
}
```

> 推送新闻前先调用此接口确认分类名称；列表按 `sort_order` 降序排列，「默认」恒在最后。

### 新增分类

```
POST /api/categories
Content-Type: application/json

{ "name": "人工智能" }
```

- `name` 必填且唯一；`color`、`sort_order` 可选。
- 分类上限 10 个，超出返回 `400`；成功返回 `201` 及新建分类对象。

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
  "category": "科技",
  "cover_url": "/images/ext_cover_abc.png",
  "collected_at": "2026-08-07T10:00:00+08:00"
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
| `category` | 否 | 分类**名称**（推荐，与 `category_id` 二选一，优先使用名称） |
| `category_id` | 否 | 分类 ID（不传且不传名称时归入「默认」分类） |
| `cover_url` | 否 | 封面图 URL |
| `collected_at` | 否 | 收录时间（ISO 8601），不传则为当前时间 |

成功返回 `201` 及完整新闻对象（含自增 `id`、最终 `category_name`、`cover_url`）。

> **封面图约定（辅助模式封面落盘 → 平台改名）**：
>
> 1. 外部 Agent 先把封面图**写入平台挂载的 `images` 目录**：宿主机部署为项目根 `images/`（容器内为 `/app/images/`），两者同步；最常用路径为 `images/cover/`（如 `/images/cover/外部生成_时间戳.png`），也可放在 `images/` 任意子目录。
> 2. 推送新闻时在 `cover_url` 传对应的本地链接，如 `/images/cover/xxx.png`。
> 3. 平台收到后会把该文件**改名**为 `/images/cover/{新闻id}.{原扩展名}`（如 `/images/cover/508.png`）并回写 `cover_url` 字段——**最终封面名称以新闻 id 命名**，返回的新闻对象里即为最终链接。
> 4. 文件名请确保**唯一**（建议时间戳/随机串），避免与其它新闻的待处理文件冲突。
> 5. **不传** `cover_url` 时：自主模式按开关自动生成/选取默认封面；辅助模式下无封面由前端兜底展示。请勿传外链 URL（平台不会拉取外链图片）。

### 查询新闻列表

```
GET /api/news?page=1&page_size=10&category=科技&keyword=模型&is_read=0&today_only=false&date_filter=2026-08-07
```

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页，`page_size` 上限 100 |
| `category` | 按分类名称筛选 |
| `keyword` | 按标题关键词搜索（支持多关键词空格分隔） |
| `is_read` | `0` 未读 / `1` 已读 |
| `today_only` | `true` 仅今日 |
| `date_filter` | 指定日期 `YYYY-MM-DD` |

响应：`{ "total": N, "items": [ ...新闻对象 ] }`。每条新闻对象含 `id`、`title`、`summary`、`content`、`category_name`、`category_color`、`source_url`、`source`、`cover_url`、`collected_at`、`is_read`、`is_reading`、`is_fav`、`is_later`、`related_ids` 等。

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

返回相关新闻列表（平台开启智能推荐时为 AI 计算的跨分类结果，否则回退为同分类最新新闻）。

---

## 每日简报接口

> 简报数据在 `briefs` 表，一条记录含 `content`（正文）、`note`（便签）、`source`（来源）。
> **辅助模式下请由外部 Agent 撰写简报并通过 `POST /api/brief` 写入**（`source` 默认 `外部`），平台负责展示、编辑与便签。

### 新增简报（外部 Agent 常用）

```
POST /api/brief
Content-Type: application/json

{
  "category": "科技",
  "date": "2026-08-07",
  "content": "## 科技 · 每日简报\n\n今日科技领域……",
  "note": "可选的配套便签",
  "source": "外部"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `category` | 是 | 分类名称（需与平台分类一致） |
| `date` | 是 | 简报日期 `YYYY-MM-DD` |
| `content` | 否 | Markdown 简报正文 |
| `note` | 否 | 配套便签 |
| `source` | 否 | 默认 `外部` |

- 幂等：同一分类 + 日期已存在时更新 `content`（并保留/更新 `note`、`source`）。
- 成功返回 `201` 及保存后的简报对象。

### 读取简报

```
GET /api/brief/{category}?date=2026-08-07
```

- `date` 省略时返回该分类最近一期简报。
- 返回：`{ "category", "date", "content", "source", "note" }`；不存在返回 `404`。

### 有简报的日期（日历圆点）

```
GET /api/brief/dates/{category}
```

返回：`{ "dates": ["2026-08-07", ...] }`。

### 便签读写

```
GET /api/brief/note?category=科技&date=2026-08-07
PUT /api/brief/note
Content-Type: application/json

{ "category": "科技", "date": "2026-08-07", "content": "便签内容" }
```

---

## 统计接口

```
GET /api/stats
```

返回总数、已读/未读数、今日新增、各分类数量、近 30 日趋势、最近阅读/在读等，可用于生成报表。

---

## 模型与服务配置接口

平台的大语言 / 文生图 / 搜索服务配置统一入库（`model_configs` 表），API Key 使用 `SECRET_KEY` 加密存储。

```
GET  /api/model-configs?config_type=llm|image|search    # 列表（含 enabled / has_api_key）
POST /api/model-configs                                  # 新增
PUT  /api/model-configs/{id}                             # 编辑（api_key 留空表示不修改）
DELETE /api/model-configs/{id}                           # 删除（已启用配置不可删除）
POST /api/model-configs/{id}/enable                      # 启用（同类型仅一个启用）
POST /api/model-configs/{id}/disable                     # 取消启用
POST /api/model-configs/test                             # 测试连接（body 同新增字段，不落库）
```

新增 / 测试连接时传入字段：`provider`（服务商名称）、`base_url`、`model_id`、`config_type`（llm/image/search）、`api_key`。

---

## 辅助模式工作流（外部 Agent 的核心职责）

外部 Agent 在辅助模式下扮演「资讯助手」，典型工作流如下：

### 工作流 A：采集并推送新闻（心跳任务）

每次心跳到来时，检索**上一次心跳之后**的最新资讯，覆盖科技/军事/政治/经济/文化/热点等分类：

1. **搜索**：按分类检索该时间段内的新闻，确保不遗漏、不重复。
2. **整理**：对每条新闻提炼字段：
   - `title`：原文标题
   - `summary`：约 100 字概要
   - `content`：Markdown 正文（篇幅充实，独立可读）
   - `source_url`：原文链接
   - `source`：来源媒体名称
   - `category`：所属分类名称
   - `cover_url`：封面图。调用文生图模型生成图片后，**先把图片文件写入平台挂载的 `images` 目录**（容器内 `/app/images/`，宿主为部署根目录 `images/`），建议直接放 `images/cover/` 并取唯一文件名（如 `cover_<时间戳>.png`），再传 `/images/cover/cover_<时间戳>.png`；平台会把该文件改名到 `/images/cover/{新闻id}.png`（保留原扩展名）并改写 `cover_url` 字段
   - `collected_at`：发现时间（ISO 8601）
3. **写入**：逐条 `POST /api/news` 入库，记录返回的 `id` 以备追踪。
4. **去重**：推送前用 `GET /api/news?keyword=标题` 检查是否已存在。

### 工作流 B：从平台读取资讯做二次处理

1. `GET /api/news?page_size=50&is_read=0` 拉取未读新闻。
2. 处理完成后 `POST /api/news/{id}/read` 标记已读，避免重复处理。

### 工作流 C：为平台生成每日简报

1. `GET /api/categories` 获取分类列表。
2. 对每个分类 `GET /api/news?category={分类}&today_only=true&page_size=50` 拉取当日新闻。
3. 汇总整理后 `POST /api/brief` 写入简报（`source` 默认 `外部`）。
4. 平台简报页自动展示，用户可补充便签。

### 工作流 D：按主题聚合 / 报表

- `GET /api/news?category=科技&page_size=100` 按分类拉取。
- `GET /api/news?keyword=关键词` 全文检索。
- `GET /api/stats` 生成阅读统计报表。

---

## 错误处理

- `400`：参数错误（如分类名重复、分类数达上限）。响应体 `detail` 字段说明原因。
- `404`：资源不存在（新闻/分类/简报 ID 无效）。
- `422`：请求体字段校验失败（FastAPI 标准格式）。
- 建议：对 `4xx` 读取 `detail` 并向用户/上游反馈；对网络错误做重试。

## 协作约定

- **幂等推送**：平台按标题展示，重复标题会并存；如需去重，推送前先用 `GET /api/news?keyword=标题` 检查。
- **正文格式**：`content` 使用 Markdown，平台详情页会自动渲染。
- **时间**：`collected_at` 建议使用 ISO 8601；列表默认按收录时间倒序、未读优先展示。
- **批量**：逐条 `POST /api/news`；大批量时注意控制并发，避免压垮本地服务。
- **封面**：图片需先落盘到平台挂载的 `images` 目录（宿主 `images/` 与容器 `/app/images/` 同步），再传 `/images/xxx.png`；平台会改名到 `/images/cover/{新闻id}.{扩展名}`。请勿传外链 URL（平台不会拉取外链图片）。
