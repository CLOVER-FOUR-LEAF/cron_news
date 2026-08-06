---
name: cron-news
description: 与「定时资讯」新闻聚合平台对接。当需要向该平台推送新闻、查询新闻、管理分类、写入或读取每日简报或读取统计数据时使用本技能。适用场景：外部 Agent（如 OpenClaw、Hermes）把采集到的新闻写入平台展示、为平台生成每日简报，或从平台读取已有资讯。平台处于「辅助模式」时专为外部 Agent 提供数据接口。
---

# 定时资讯 · 对接技能

「定时资讯」是一个新闻聚合展示平台。在**辅助模式**下，它本身不主动采集，而是作为展示与数据中枢，等待外部 Agent 推送新闻、读取分类与资讯。本技能教你如何通过其 REST API 与它完美协作。

## 基础信息

- **协议**：HTTP + JSON，所有接口无需鉴权（本地/内网部署）。
- **Base URL**：默认 `http://localhost:8000`，以实际部署地址为准。
- **字符编码**：UTF-8，请求体需带 `Content-Type: application/json`。
- **在线文档**：`{Base URL}/docs`（Swagger UI）、`{Base URL}/openapi.json`（OpenAPI 规范）。
- **健康检查**：`GET /health` → `{"status":"ok", ...}`，可用于探活。

## 核心概念

- **分类（Category）**：新闻的归属维度，每个分类有名称、主题色、排序权重。系统内置一个不可删除的「默认」分类（`is_default=true`），作为未指定分类新闻的兜底。
- **新闻（News）**：一条资讯，含标题、概要、Markdown 正文、来源、封面、收录时间、已读状态等。
- **分类用 ID 关联**：新闻通过 `category_id` 关联分类；修改分类名无需改动新闻。

---

## 分类接口

### 获取所有分类

```
GET /api/categories
```

响应：

```json
{
  "total": 3,
  "items": [
    { "id": 2, "name": "科技", "sort_order": 2, "color": "#d4a853", "is_default": false, "created_at": "2024-01-01T00:00:00" },
    { "id": 1, "name": "默认", "sort_order": 0, "color": "#8a8690", "is_default": true, "created_at": "2024-01-01T00:00:00" }
  ]
}
```

> 推送新闻前先调用此接口，拿到目标分类的 `id`。列表按 `sort_order` 降序排列，「默认」恒在最后。

### 新增分类

```
POST /api/categories
Content-Type: application/json

{ "name": "人工智能" }
```

- `name` 必填且唯一；`color`、`sort_order` 可选（不传则自动分配唯一主题色并置顶）。
- 分类上限 10 个，超出返回 `400`。
- 成功返回 `201` 及新建分类对象。

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
  "category_id": 2,
  "cover_url": "/images/cover.png",
  "collected_at": "2024-01-01T12:00:00"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `title` | 是 | 标题，1–255 字 |
| `summary` | 否 | 概要，列表页展示 |
| `content` | 否 | Markdown 正文，详情页渲染 |
| `source_url` | 否 | 原文链接 |
| `source` | 否 | 来源名称 |
| `category_id` | 否 | 分类 ID；不传或无效时归入「默认」分类 |
| `cover_url` | 否 | 封面图 URL |
| `collected_at` | 否 | 收录时间（ISO 格式），不传则为当前时间 |

成功返回 `201` 及完整新闻对象（含自增 `id`）。

### 查询新闻列表

```
GET /api/news?page=1&page_size=10&category=科技&keyword=模型&is_read=0&today_only=false&date_filter=2024-01-01
```

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页，`page_size` 上限 100 |
| `category` | 按分类**名称**筛选 |
| `keyword` | 按标题关键词搜索 |
| `is_read` | `0` 未读 / `1` 已读 |
| `today_only` | `true` 仅今日 |
| `date_filter` | 指定日期 `YYYY-MM-DD` |

响应：`{ "total": N, "items": [ ...新闻对象 ] }`。每条新闻对象含 `id`、`category_name`、`category_color`、`related_ids` 等。

### 获取新闻详情

```
GET /api/news/{id}
```

返回单条新闻对象；不存在返回 `404`。

### 标记已读

```
POST /api/news/{id}/read
```

成功返回 `{"message":"ok"}`。

### 获取相关推荐

```
GET /api/news/{id}/related?limit=10
```

返回与指定新闻相关的内容列表（平台开启智能推荐时为 AI 计算的跨分类结果，否则回退为同分类最新新闻）。

---

## 统计接口

```
GET /api/stats
```

返回总数、已读/未读数、今日新增、各分类数量、近 7 日趋势、最近阅读等，可用于生成报表。

---

## 每日简报接口

> 简报、便签、分类等数据统一存放在 `briefs` 表（每日简报），一条记录包含 `content`（简报正文）、`note`（便签）与 `source`（来源：**自主** / **外部**）。
> - 自主模式下由平台内置 Agent 在每次采集完成后自动生成（`source=自主`）。
> - **辅助模式下请由外部 Agent 生成并通过 `POST /api/brief` 写入**（`source=外部`），平台负责展示与便签。

### 新增简报（外部 Agent 最常用）

```
POST /api/brief
Content-Type: application/json

{
  "category": "科技",
  "date": "2026-08-05",
  "content": "## 科技 · 每日简报\n\n今日科技领域……",
  "note": "可选的配套便签",
  "source": "外部"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `category` | 是 | 分类名称（需与平台分类一致，可先 `GET /api/categories` 获取） |
| `date` | 是 | 简报日期 `YYYY-MM-DD` |
| `content` | 否 | Markdown 简报正文 |
| `note` | 否 | 配套便签 |
| `source` | 否 | 默认 `外部`；传 `自主`/`外部` 之外的任意值按 `外部` 处理 |

- 幂等：同一分类 + 日期已存在时更新 `content`（并保留/更新 `note`、`source`）。
- 成功返回 `201` 及保存后的简报对象。

### 读取简报

```
GET /api/brief/{category}?date=2026-08-05
```

- `date` 省略时返回该分类最近一期简报。
- 返回：`{ "category", "date", "content", "source", "note" }`；不存在返回 `404`。

### 获取有简报的日期（日历圆点）

```
GET /api/brief/dates/{category}
```

返回：`{ "dates": ["2026-08-05", ...] }`。

### 便签读写

```
GET /api/brief/note?category=科技&date=2026-08-05
PUT /api/brief/note
Content-Type: application/json

{ "category": "科技", "date": "2026-08-05", "content": "便签内容" }
```

便签与会话内自动保存的笔记共用，同一分类 + 日期唯一。

---

## 常见工作流

### 工作流 A：把采集到的新闻写入平台

1. `GET /api/categories` 获取分类，匹配目标分类 `id`（无匹配可用「默认」或先 `POST /api/categories` 新建）。
2. 对每条新闻 `POST /api/news`，带上 `title`、`content`、`category_id`、`source` 等。
3. 记录返回的 `id` 以备后续更新/追踪。

### 工作流 B：从平台读取资讯做二次处理

1. `GET /api/news?page_size=50&is_read=0` 拉取未读新闻。
2. 处理完成后 `POST /api/news/{id}/read` 标记已读，避免重复处理。

### 工作流 C：按主题聚合

1. `GET /api/news?category=科技&page_size=100` 按分类拉取。
2. 或 `GET /api/news?keyword=关键词` 全文检索。

### 工作流 D：辅助模式下为平台生成每日简报

1. `GET /api/categories` 获取分类列表。
2. 对每个分类 `GET /api/news?category={分类}&today_only=true&page_size=50` 拉取当日新闻。
3. 汇总整理后 `POST /api/brief` 写入简报（`source` 默认 `外部`）。
4. 平台简报页将自动展示这些简报，用户可在页面上补充便签。

---

## 错误处理

- `400`：参数错误（如分类名重复、分类数达上限）。响应体 `detail` 字段说明原因。
- `404`：资源不存在（新闻/分类 ID 无效）。
- `422`：请求体字段校验失败（FastAPI 标准格式）。
- 建议：对 `4xx` 读取 `detail` 并向用户/上游反馈；对网络错误做重试。

## 协作约定

- **幂等推送**：平台按标题展示，重复标题会并存；如需去重，推送前先用 `GET /api/news?keyword=标题` 检查。
- **正文格式**：`content` 使用 Markdown，平台详情页会自动渲染。
- **时间**：`collected_at` 建议使用 ISO 8601；列表默认按收录时间倒序、未读优先展示。
- **批量**：逐条 `POST /api/news`；大批量时注意控制并发，避免压垮本地服务。
