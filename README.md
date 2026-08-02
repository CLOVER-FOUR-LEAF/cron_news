# 定时资讯 - AI新闻聚合平台

AI辅助新闻展示平台，用于收集和展示AI搜集的新闻信息。

## 技术栈

- **后端**: Python 3.13 + FastAPI 0.138 + Uvicorn
- **数据库**: SQLite (aiosqlite)
- **前端**: Jinja2模板 + 原生CSS/JavaScript
- **包管理**: uv

## 快速启动

```bash
# 安装依赖
uv sync

# 启动服务
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> 首次启动会自动创建数据库和默认分类

## 访问地址

| 地址 | 说明 |
|------|------|
| http://localhost:8000 | 首页 |
| http://localhost:8000/category/{分类名} | 分类页 |
| http://localhost:8000/news/{id} | 新闻详情页 |
| http://localhost:8000/search?q=关键词 | 搜索结果页 |
| http://localhost:8000/stats | 阅读统计面板 |
| http://localhost:8000/docs | Swagger API文档 |
| http://localhost:8000/redoc | ReDoc API文档 |

## 功能说明

### 新闻分类
支持动态分类管理，默认初始化：科技 | 军事 | 政治 | 经济 | 文化 | 热点

### 核心功能
- 首页横向轮播（仅展示当日新闻）
- 分类页网格布局 + 日期筛选
- 新闻详情 Markdown 渲染 + 相关推荐
- 已读/未读状态追踪（未读优先展示）
- 全文搜索
- 阅读统计面板（饼图、柱状图、分页列表）

## 数据库

数据库文件位于 `database/cron_news.db`

### categories表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键，自增 |
| name | VARCHAR(50) | 分类名称，唯一 |
| sort_order | INTEGER | 排序权重 |
| created_at | DATETIME | 创建时间 |

### news表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键，自增 |
| title | VARCHAR(255) | 标题 |
| summary | TEXT | 概要 |
| content | TEXT | Markdown正文 |
| source_url | VARCHAR(500) | 原文链接 |
| source | VARCHAR(100) | 来源 |
| category_id | INTEGER | 分类ID，外键 |
| cover_url | VARCHAR(500) | 封面图路径 |
| collected_at | DATETIME | 收录时间 |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 更新时间 |
| is_deleted | SMALLINT | 逻辑删除 |
| is_read | SMALLINT | 已读状态 |

## API接口

> 完整文档访问 http://localhost:8000/docs

---

### 分类管理

#### GET /api/categories

获取所有分类。

**响应 200:**
```json
{
  "total": 6,
  "items": [
    {
      "id": 1,
      "name": "科技",
      "sort_order": 0,
      "created_at": "2024-01-01T00:00:00"
    }
  ]
}
```

#### GET /api/categories/{category_id}

获取单个分类。

**响应 200:**
```json
{
  "id": 1,
  "name": "科技",
  "sort_order": 0,
  "created_at": "2024-01-01T00:00:00"
}
```

**响应 404:**
```json
{ "detail": "分类不存在" }
```

#### POST /api/categories

创建分类。

**请求体:**
```json
{
  "name": "分类名称",
  "sort_order": 0
}
```

**响应 201:** 返回创建的分类对象

**响应 400:**
```json
{ "detail": "分类名称已存在" }
```

#### PUT /api/categories/{category_id}

更新分类。

**请求体:**
```json
{
  "name": "新名称",
  "sort_order": 1
}
```

**响应 200:** 返回更新的分类对象

**响应 404:**
```json
{ "detail": "分类不存在" }
```

**响应 400:**
```json
{ "detail": "分类名称已存在" }
```

#### DELETE /api/categories/{category_id}

删除分类。

**响应 200:**
```json
{ "message": "ok" }
```

**响应 404:**
```json
{ "detail": "分类不存在" }
```

---

### 新闻管理

#### GET /api/news

获取新闻列表。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| page | int | 否 | 1 | 页码 |
| page_size | int | 否 | 10 | 每页数量(1-100) |
| keyword | string | 否 | - | 搜索关键词 |
| category | string | 否 | - | 分类名称筛选 |
| today_only | bool | 否 | false | 仅今日新闻 |
| date_filter | date | 否 | - | 日期筛选(YYYY-MM-DD) |
| is_read | int | 否 | - | 已读状态(0/1) |

**响应 200:**
```json
{
  "total": 49,
  "items": [
    {
      "id": 1,
      "title": "新闻标题",
      "summary": "概要",
      "content": "# Markdown正文",
      "source_url": "https://example.com",
      "source": "新华社",
      "category_id": 1,
      "category_name": "科技",
      "cover_url": "/images/1.png",
      "collected_at": "2024-01-01T00:00:00",
      "is_read": 0,
      "created_at": "2024-01-01T00:00:00",
      "updated_at": "2024-01-01T00:00:00"
    }
  ]
}
```

#### GET /api/news/{news_id}

获取新闻详情。

**响应 200:** 同上单条新闻对象

**响应 404:**
```json
{ "detail": "新闻不存在" }
```

#### POST /api/news

创建新闻。

**请求体:**
```json
{
  "title": "标题（必填）",
  "summary": "概要",
  "content": "# Markdown正文",
  "source_url": "https://example.com",
  "source": "来源",
  "category_id": 1,
  "cover_url": "images/temp.png",
  "collected_at": "2024-01-01T00:00:00"
}
```

> `cover_url` 传入本地路径时，系统自动重命名为 `{id}.{ext}`

**响应 201:** 返回创建的新闻对象

#### POST /api/news/{news_id}/read

标记新闻为已读。

**响应 200:**
```json
{ "message": "ok" }
```

**响应 404:**
```json
{ "detail": "新闻不存在" }
```

---

### 统计数据

#### GET /api/stats

获取阅读统计数据。

**响应 200:**
```json
{
  "total": 49,
  "read_count": 10,
  "unread_count": 39,
  "today_count": 8,
  "category_stats": {
    "科技": 8,
    "军事": 8,
    "政治": 8,
    "经济": 8,
    "文化": 8,
    "热点": 9
  },
  "daily_stats": [
    { "date": "06-22", "count": 5 },
    { "date": "06-23", "count": 3 }
  ],
  "recent_reads": [
    {
      "id": 1,
      "title": "标题",
      "category": "科技",
      "source": "新华社",
      "collected_at": "2024-01-01T00:00:00",
      "is_read": 1
    }
  ]
}
```

## 项目结构

```
cron_news/
├── app/
│   ├── main.py              # 入口 + 页面路由
│   ├── config.py             # 配置
│   ├── database.py           # 数据库连接
│   ├── models/
│   │   ├── __init__.py
│   │   ├── news.py           # News模型
│   │   └── category.py       # Category模型
│   ├── schemas/
│   │   ├── news.py           # News Pydantic模型
│   │   └── category.py       # Category Pydantic模型
│   ├── routers/
│   │   ├── news.py           # News API路由
│   │   └── category.py       # Category API路由
│   └── crud/
│       ├── news.py           # News数据操作
│       └── category.py       # Category数据操作
├── templates/
│   ├── home.html             # 首页
│   ├── category.html         # 分类页
│   ├── news_detail.html      # 详情页
│   ├── search.html           # 搜索页
│   └── stats.html            # 统计页
├── static/css/style.css      # 样式
├── images/                   # 封面图
├── database/                 # SQLite数据库目录
│   └── cron_news.db          # 数据库文件
├── pyproject.toml            # 依赖
├── uv.lock                   # 锁文件
├── .env                      # 环境变量
└── README.md                 # 本文档
```
