# 定时资讯 · Cron News

一个 AI 新闻聚合展示平台。支持**自主模式**下由内置 Agent 自动联网采集、撰稿、生成封面与每日简报；也支持**辅助模式**下作为数据中枢，供外部 Agent（如 OpenClaw、Hermes）通过 API 写入与读取资讯。

---

## 主要功能

- **新闻聚合展示**：首页轮播、分类页、详情页（Markdown 渲染）、全文搜索、日期筛选。
- **AI 对话助手**：用自然语言问自己的新闻库，先检索再回答，回答带来源引用；支持多轮会话与会话历史。
- **流式输出与过程可见**：模型回答逐字输出，思考内容、工具调用与检索结果在对话中实时展示。
- **阅读管理**：未读 / 在读 / 已读三态追踪，收藏与稍后再读，个人阅读统计面板。
- **每日简报**：按分类汇总当日新闻自动生成简报，可在简报页编辑、写便签、导出 Markdown / Word。
- **智能推荐**：基于 AI 相关性分析，在详情页跨分类推荐真正相关的内容。
- **自动生成资讯封面**：两种方式可选——**大模型 SVG**（让大语言模型绘制矢量插画，成本约低一个数量级、可无损缩放）或**文生图模型**（写实效果更好、成本较高）；关闭时随机使用系统默认封面。
- **首次使用向导**：首次打开时用四步向导完成工作模式、模型服务、采集频率与封面方式配置，可随时跳过。
- **首页一体化**：首页左侧是新闻流，右侧四分之一是 AI 对话；对话就在该区域完成，不抢占整屏，可手动切换全屏或全屏新闻。
- **模型与服务统一管理**：大语言、文生图、搜索三类配置统一入库，仅需在界面维护各服务商 API Key。

## Agent 相关能力

| 能力 | 说明 |
|------|------|
| **工作模式切换** | 辅助 / 自主两种模式，随时可在设置中切换 |
| **自主模式** | 内置 Agent 按设定频率调用搜索与大模型 API，自动采集、撰写正文、生成推荐与简报 |
| **辅助模式** | 项目本身不调用模型，作为展示平台，通过 REST API 供外部 Agent 推送新闻、写入简报 |
| **Agent 配置管理** | 分类、大语言/文生图/搜索配置、定时任务、系统提示词、功能开关（智能推荐/每日简报/自动生成封面）均可在界面管理 |
| **对话问答（Phase 1）** | `/chat` 页面：工具循环检索本地新闻库、读取全文/简报/收藏，回答带来源引用，SSE 输出工具进度 |
| **向量 RAG（R1）** | 新闻自动向量化；FTS/关键词 + 语义检索 + RRF 融合；按数据库自动选择 BLOB(numpy) 或 pgvector 后端 |
| **检索 API** | `/api/retrieval/search`、`/probe`、`/status`、`/reindex`，供页面、脚本和外部 Agent 复用 |
| **数据库选择** | 内置 SQLite 零配置开箱即用，也可切换到自建的 MySQL / MariaDB / PostgreSQL（支持一键迁移） |

> 外部 Agent 对接文档见 `skill/SKILL.md`（推送新闻、写入简报、模型配置等接口说明）。

## AI 对话助手（Phase 1）

打开 `/chat` 即可用自然语言问自己的新闻库，例如「今天科技圈有什么大事？」「帮我梳理最近一周的 AI 芯片新闻」「我收藏的新闻有什么共同主题？」。

- **检索**：SQLite FTS5 `trigram` 索引（标题 + 摘要，≥3 字）+ LIKE 兜底（2 字短词/非 SQLite），无需额外检索服务。
- **工具**：`search_news`、`get_news`、`list_news`、`get_daily_brief`、`get_related_news`、`get_news_stats`、`list_categories`、`get_reading_list`、`search_web`（只读；写操作默认关闭）。
- **模型兼容**：沿用「模型与服务配置」中启用的大语言模型；原生 `tools` 不可用时自动降级为 JSON 协议，再降级为「预检索 + 单次回答」。
- **引用**：回答中的 `[[news:ID]]` 会被校验并重写为可点击的 `[1]` 来源，未在本轮工具结果中出现的引用会被剔除。
- **开关**：`AGENT_CHAT_ENABLED`、`AGENT_TOOL_PROTOCOL`、`AGENT_MAX_ITERATIONS`、`AGENT_DAILY_TOKEN_BUDGET` 等，见 `.env.example`。

> 当前新闻正文仍是平台基于「标题 + 摘要」的改写稿，引用会同时给出原文 `source_url`；关键事实请以原文为准。Phase 2 会改为先抓取原文再写稿。

## Agent 协同

项目里有四个各司其职的 Agent，对话助手可以在需要时把任务委派给它们：

| Agent | 职责 | 对话中的触发方式 |
|---|---|---|
| 对话助手 | 理解需求、检索、组织回答 | 直接聊天 |
| 采集 Agent | 搜索 → 撰稿 → 封面 → 向量索引 | “帮我采集新闻 / 更新新闻库” → `delegate_collect` |
| 简报 Agent | 按分类生成当日简报 | “给我一份简报 / 总结今天新闻” → `delegate_brief` |
| 推荐 Agent | 计算新闻相关性 | “更新相关推荐” → `delegate_recommend` |

- 委派任务会在对话里实时显示进度（如“采集 Agent 正在搜索…”），完成后由对话助手基于真实结果汇报。
- 采集/简报/推荐与定时任务共用一把任务锁：同一时间只跑一个，避免重复写库、重复调用模型。被占用时会如实告知，而不是排队等待。
- 可通过 `AGENT_ALLOW_DELEGATION=false` 关闭委派能力。

> 定时任务按服务器本地时区计算。项目启动时会把时区固定为 `Asia/Shanghai`（可用 `APP_TIMEZONE` 覆盖），避免 systemd / nohup 启动方式不同导致“到点不执行”。

## 向量检索（RAG）

新闻入库后会自动生成向量，检索时同时走向量与关键词，再用 RRF 融合排序。

### 启用方式

1. 在「设置 → 模型与服务配置 → 向量模型」新增一个支持 `/embeddings` 的服务（如阿里 `text-embedding-v3`、智谱 `embedding-3`、OpenAI、BGE-M3 等），填写 Base URL、模型 ID 和 API Key，并启用。
2. 保存后后台索引器（启动 5 秒后、之后每 60 秒）会自动补齐向量；也可以手动触发：
   ```bash
   curl -X POST 'http://localhost:8000/api/retrieval/reindex?limit=5000'
   ```
3. 未配置向量模型时，检索自动退化为纯关键词，不影响现有功能。

### 向量后端如何选择

| 数据库 | 后端 | 说明 |
|---|---|---|
| SQLite（默认） | `blob` | `news_embeddings.vector` BLOB + numpy 余弦，零额外服务 |
| MySQL / MariaDB | `blob` | 同上；不依赖 MySQL 9 / MariaDB 11.7 才有的 VECTOR 类型 |
| PostgreSQL | `pgvector` | BLOB 是事实来源，`news_embedding_vec` 是派生索引；扩展缺失时自动回退 BLOB |

- 向量只和**同一个 embedding 模型**比较；换模型后旧向量会被忽略并按需重建。
- PostgreSQL 下 `ensure_schema` 会自动创建扩展、派生表并从 BLOB 补齐；整库迁移（SQLite → PG）后无需手工处理。
- 规模提示：5 万条 × 512 维约 100MB，暴力余弦通常 < 20ms；超过这个量级再考虑 ANN（`POST /api/retrieval/vector/ann-index?dim=512`）。

### 检索调试

```bash
# 同时返回混合 / 词法 / 语义三组结果，便于对比召回
curl -X POST http://localhost:8000/api/retrieval/probe \
  -H 'Content-Type: application/json' \
  -d '{"q":"英伟达 芯片","limit":8}'

# 查看 FTS 能力、向量后端、已索引数量、待索引数量
curl http://localhost:8000/api/retrieval/status
```

维护脚本：

```bash
uv run python scripts/reindex.py --stats       # 查看状态
uv run python scripts/reindex.py --all         # 重建 FTS + 补向量 + 重建派生索引
```

---

## 部署方式一：Docker 部署（推荐）

**前置条件**：安装 Docker 与 Docker Compose。

### 方式 A：一键部署脚本（推荐）

1. 拉取代码并进入项目目录：

   ```bash
   git clone <你的仓库地址> cron_news
   cd cron_news
   ```

2. 运行部署脚本（自动检查环境 → 生成 `.env` → 准备数据目录 → 构建镜像 → 启动容器 → 健康检查 → 打印定时任务状态）：

   ```bash
   bash deploy.sh
   ```

   脚本会自动生成 `.env`（首次运行会提示你编辑后再次运行）。Windows 用户可改用：

   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy.ps1
   ```

3. 访问平台：<http://localhost:18080>。

> 脚本会挂载四个持久化目录：`database`（SQLite）、`logs`（运行日志，每天自动归档一份，保留 30 天）、`images`（封面/头像等资源）、`.env`（配置），容器重建不会丢失数据。宿主端口默认使用不常用的五位端口 `18080`（容器内为 8000）；若冲突，可用 `CRON_NEWS_PORT=18090 bash deploy.sh`（需同时修改 `docker-compose.yml` 的端口映射）。执行脚本前若检测到同名容器 `cron_news` 或镜像，会要求确认（可能是升级或你自己的同名项目）。

### 方式 B：手动执行 compose

1. 拉取代码并进入项目目录：

   ```bash
   git clone <你的仓库地址> cron_news
   cd cron_news
   ```

2. 创建配置文件（复制模板，按需填写）：

   ```bash
   cp .env.example .env
   ```

   > 大语言 / 文生图 / 搜索服务的 Base URL、模型 ID、启用状态等全部在**设置页面**里维护；`.env` 只需要填写用到的各服务商 `API_KEY_*`。首次启动后默认用户名为 `User` + 6 位随机数字。

3. 构建并启动：

   ```bash
   docker compose up -d --build
   ```

4. 访问平台：<http://localhost:18080>

**数据持久化**：`database`（SQLite）、`logs`（日志）、`images`（图片资源）与 `.env` 配置文件已通过 `docker-compose.yml` 挂载到宿主机，容器重建不会丢失数据。

**其他 Docker 用法**：

```bash
# 手动构建并运行
docker build -t cron_news .
docker run -d -p 18080:8000 \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/database:/app/database \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/images:/app/images \
  --name cron_news cron_news

# 查看日志 / 停止 / 移除
docker compose logs -f
docker compose down
```

---

## 部署方式二：拉取代码直接启动

**前置条件**：Python 3.10+（推荐 3.13）与 [uv](https://docs.astral.sh/uv/) 包管理器。

```bash
# 1. 拉取代码
git clone <你的仓库地址> cron_news
cd cron_news

# 2. 安装依赖
uv sync

# 3. 准备配置
cp .env.example .env

# 4. 启动服务
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 <http://localhost:8000>。首次启动会自动创建 SQLite 数据库、默认分类与默认用户名。

---

## 快速上手

1. 打开 <http://localhost:18080>（Docker 部署默认端口，直接启动为 8000），首次进入选择工作模式（辅助 / 自主）。
2. 在右上角 **设置** 中完成配置：
   - 首次打开会进入**快速开始向导**，按步骤填写工作模式、模型服务、采集频率与封面方式；也可以点「跳过向导」稍后在设置里配置。
   - **模型与服务配置**：新增并启用一个大语言模型、一个搜索服务（自主模式必需）；封面想要写实效果再启用一个文生图模型，否则用大模型 SVG 即可。
   - **分类管理**：维护导航栏分类。
   - **自动任务配置**：设定采集频率与目标分类。
3. 自主模式下点击「立即执行」或等待定时任务，Agent 会自动采集新闻、撰写正文并生成封面 / 简报。

---

## 常见问题

- **自主模式运行需要什么？** 至少启用一个大语言模型和一个搜索服务，并填写对应服务商 API Key。
- **如何启用每日简报 / 智能推荐 / 自动生成封面？** 进入 **设置 → Agent 管理**，打开对应开关。封面方式可选「大模型 SVG」（只需大语言模型）或「文生图」（需先启用文生图模型）。
- **向导关掉了怎么再打开？** 设置菜单 →「配置向导」，随时可以重新走一遍。
- **提示词在哪里调整？** **设置 → Agent 管理 → 提示词管理**，可分别定制定时资讯、智能推荐、AI 简报的系统提示词。
- **`sign.png`、`cover/default/*`、`avatar/default-avatar.png` 是什么？** 系统默认资源：站点 logo、默认新闻封面、默认头像，随代码仓库一起提供。

---

## 预设厂商参考

新增配置时在「模型与服务配置 → 新增」中直接填写服务商名称、Base URL、API Key 与模型 ID 即可。以下为常见预设厂商信息（大语言 / 文生图 / 搜索），API Key 需在各厂商后台自行申请，填写后在平台内**加密存储**。

| 厂商名称 | 支持类型 | Base URL | API 后台 / 申请地址 |
|---------|---------|----------|---------------------|
| DeepSeek | 大语言 | `https://api.deepseek.com/v1` | <https://platform.deepseek.com/> |
| Kimi（月之暗面） | 大语言 | `https://api.moonshot.cn/v1` | <https://platform.moonshot.cn/> |
| 智谱 AI | 大语言 | `https://open.bigmodel.cn/api/paas/v4` | <https://open.bigmodel.cn/> |
| MiniMax | 大语言 | `https://api.minimax.chat/v1` | <https://platform.minimaxi.com/> |
| MiMo（小米） | 大语言 / 搜索 | `https://api.mimo.xiaomi.com/v1` | <https://platform.mimo.xiaomi.com/> |
| 阿里云百炼 | 大语言 / 文生图 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | <https://bailian.console.aliyun.com/> |
| 字节方舟 | 大语言 / 文生图 | `https://ark.cn-beijing.volces.com/api/v3` | <https://console.volcengine.com/ark/> |
| 腾讯混元 | 大语言 | `https://api.hunyuan.cloud.tencent.com/v1` | <https://console.cloud.tencent.com/hunyuan> |
| Tavily | 搜索 | `https://api.tavily.com` | <https://app.tavily.com/> |

> 以上 Base URL 为 OpenAI 兼容接口地址；文生图模型请使用各厂商文档中提供的文生图模型 ID（如阿里云百炼 `wanx2.1-t2i-turbo`、字节方舟 `seedream-3-0-t2i` 等）。其他厂商可按相同格式自定义配置。

### 搜索服务厂商模板

不同搜索厂商的接口、鉴权方式与返回结构各不相同，平台为每个厂商提供**独立适配层**（`app/services/search_providers.py`），新增「搜索服务」配置时会提供厂商模板下拉，自动填充 Base URL，并在保存/测试时使用对应的调用方式。无法识别的厂商按「自定义」处理（兼容 `POST {base_url}/search` 的 Tavily 风格接口）。

| 模板 | Base URL | 鉴权 | 说明 |
|------|----------|------|------|
| Tavily | `https://api.tavily.com` | Bearer | 官方搜索 API，需在 <https://app.tavily.com/> 申请 Key |
| 博查 Bocha | `https://api.bochaai.com` | Bearer | 国内可直连，Key 在 <https://key.bochaai.com/> 获取，单次请求上限 10 条 |
| SearXNG | 自建实例地址 | 一般无需 | 自建元搜索（如 `http://host:8080`），返回 `format=json` |
| Exa | `https://api.exa.ai` | Bearer | 语义搜索，<https://exa.ai/> 申请 Key |
| 自定义 | 任意 | Bearer | 兼容 `POST /search`（请求体含 `query`/`max_results`，返回 `{"results":[...]}`） |

### 运行日志与定时任务

- 服务启动后会将运行日志同时输出到 **stdout**（`docker logs` 可见）与 **`logs/app.log`**（按大小轮转，可挂载到宿主机持久化）。
- 定时任务（自主模式下）每次执行、或因「搜索服务未配置 / 非自主模式」等原因**跳过**时，都会写入**运行日志**（界面「设置 → Agent 管理 → 运行日志」可见）并说明原因，不再静默失效。
- 启动时若距上次采集超过一个采集周期，会自动**补跑一次**，避免刚部署/重启后长时间没有新内容。
