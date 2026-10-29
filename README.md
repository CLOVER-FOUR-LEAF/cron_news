# 定时资讯 · Cron News

一个 AI 新闻聚合展示平台。支持**自主模式**下由内置 Agent 自动联网采集、撰稿、生成封面与每日简报；也支持**辅助模式**下作为数据中枢，供外部 Agent（如 OpenClaw、Hermes）通过 API 写入与读取资讯。

---

## 主要功能

- **新闻聚合展示**：首页工作台（推荐 / 助手 / 采集 / 简报统一入口）、标签页、详情页（Markdown 渲染）、全文搜索、日期筛选。
- **AI 对话助手**：用自然语言问自己的新闻库，先检索再回答，回答带来源引用；支持多轮会话与会话历史。
- **长期记忆与个性化**：一份所有 Agent 共用的 `memory/PROFILE.md`，记录用户画像、兴趣、跟踪任务、阅读偏好、需要避免的内容与反馈整改；阅读/收藏/停留/反馈会持续刷新画像。
- **反馈闭环**：对话回答和每篇新闻都可以反馈（不相关、太浅、重复、过时、有误等）。反馈即时进入记忆与推荐惩罚，后台 Agent 会反思并重写记忆段落。
- **采集质量**：搜索前按记忆规划指向具体事件的查询；质量闸门过滤门户首页/登录页/栏目页/百科/视频页/营销软文；抓取原文；URL/标题/语义三层去重；来源可信度评分；模型二次判定是否为真实新闻事件，并重写标题、摘要与正文。
- **流式输出与过程可见**：模型回答逐字输出，思考内容、工具调用与检索结果在对话中实时展示。
- **阅读管理**：未读 / 在读 / 已读三态追踪，收藏与稍后再读，个人阅读统计面板。
- **每日简报**：把当日全部新闻汇总成**一篇统一简报**（不再按标签拆分），可在简报页编辑、写便签、导出 Markdown / Word。
- **个性化推荐**：结合阅读标签、收藏、停留时长、常看来源、关注话题与正负反馈打分，从上一批采集中挑出最值得看的 10 篇，并附相关新闻与「为什么推荐」；详情页仍保留跨标签相关性推荐。
- **自动生成资讯封面**：两种方式可选——**大模型 SVG**（让大语言模型绘制矢量插画，成本约低一个数量级、可无损缩放）或**文生图模型**（写实效果更好、成本较高）；关闭时随机使用系统默认封面。
- **首次使用向导**：首次打开时用四步向导完成工作模式、模型服务、采集频率与封面方式配置，可随时跳过。
- **标签体系**：彻底移除分类；每条新闻由采集 Agent 自由打多个标签，用户可关注标签，搜索 Agent 围绕关注标签采集，首页展示上一批新闻的全部标签。
- **全局悬浮助手**：首页内置助手卡片与可拖动悬浮球；完整对话页 `/chat` 保留并扩展出记忆面板、快捷任务与会话管理。
- **模型与服务统一管理**：大语言、文生图、搜索三类配置统一入库，仅需在界面维护各服务商 API Key。

## Agent 相关能力

| 能力 | 说明 |
|------|------|
| **工作模式切换** | 辅助 / 自主两种模式，随时可在设置中切换 |
| **自主模式** | 内置 Agent 按设定频率调用搜索与大模型 API，自动采集、撰写正文、生成推荐与简报 |
| **辅助模式** | 项目本身不调用模型，作为展示平台，通过 REST API 供外部 Agent 推送新闻、写入简报 |
| **Agent 配置管理** | 关注标签、大语言/文生图/搜索配置、定时任务、系统提示词、功能开关（智能推荐/每日简报/自动生成封面）均可在界面管理 |
| **对话问答（Phase 1）** | `/chat` 页面：工具循环检索本地新闻库、读取全文/简报/收藏，回答带来源引用，SSE 输出工具进度 |
| **长期记忆（Phase 2）** | 五个 Agent 共用 Markdown 记忆；对话工具可读写，阅读行为自动生成画像，反馈触发记忆管家反思 |
| **个性化推荐（Phase 2）** | 行为统计 + 记忆 + 正负反馈打分，带推荐理由；与详情页的新闻间关联互补 |
| **采集质量（Phase 2）** | 原文抓取、语义去重、可信度、结构化分析；搜索查询由关注标签与记忆驱动细分 |
| **向量 RAG（R1）** | 新闻自动向量化；FTS/关键词 + 语义检索 + RRF 融合；按数据库自动选择 BLOB(numpy) 或 pgvector 后端 |
| **标签与时间线（Phase 3）** | 新闻多标签、标签关注、`/api/tags`、`/api/home`；`/api/retrieval/timeline` 按时间桶召回过去半个月/一个月 |
| **检索 API** | `/api/retrieval/search`、`/timeline`、`/probe`、`/status`、`/reindex`，供页面、脚本和外部 Agent 复用 |
| **数据库选择** | 内置 SQLite 零配置开箱即用，也可切换到自建的 MySQL / MariaDB / PostgreSQL（支持一键迁移） |

> 外部 Agent 对接文档见 `skill/SKILL.md`（推送新闻、写入简报、模型配置等接口说明）。

## AI 对话助手（Phase 1 + Phase 2）

打开 `/chat` 即可用自然语言问自己的新闻库，例如「今天科技圈有什么大事？」「帮我梳理最近一周的 AI 芯片新闻」「我收藏的新闻有什么共同主题？」。首页、标签页、文章页右下角也都有可拖动的悬浮球，可以随时唤出同一个助手。

- **检索**：SQLite FTS5 `trigram` 索引（标题 + 摘要，≥3 字）+ LIKE 兜底（2 字短词/非 SQLite），无需额外检索服务；配置 embedding 后自动叠加语义检索。
- **工具**：`search_news`、`get_news`、`list_news`、`get_daily_brief`、`get_related_news`、`get_news_stats`、`list_tags`、`timeline_news`、`get_reading_list`、`search_web`、`read_memory`、`remember`、`forget_memory`、`get_user_profile`、`get_recommendations`、`submit_feedback`，以及委派工具 `delegate_collect` / `delegate_brief` / `delegate_recommend`。
- **模型兼容**：沿用「模型与服务配置」中启用的大语言模型；原生 `tools` 不可用时自动降级为 JSON 协议，再降级为「预检索 + 单次回答」。
- **引用**：回答中的 `[[news:ID]]` 会被校验并重写为可点击的 `[1]` 来源，未在本轮工具结果中出现的引用会被剔除。
- **记忆**：系统提示会自动注入用户画像、兴趣、跟踪任务、偏好、避免项与反馈整改；用户说「记住 / 跟踪 / 以后多给我」时助手调用 `remember` 写入，说「忘掉」时调用 `forget_memory`。
- **反馈**：回答下方有「有帮助 / 不够好」，文章底部有原因标签与补充说明。反馈写入 `feedback` 表与 `memory/PROFILE.md`，推荐打分即时降权，后台反思会归纳可执行整改。
- **开关**：`AGENT_CHAT_ENABLED`、`AGENT_TOOL_PROTOCOL`、`AGENT_MAX_ITERATIONS`、`AGENT_DAILY_TOKEN_BUDGET` 等，见 `.env.example`。

> 采集 Agent 现在会尽量抓取原文；正文中的关键事实仍建议核对 `source_url`。抓取失败时会在正文顶部标注「基于搜索摘要」。

## 记忆、反馈与个性化

### 记忆文件

长期记忆是一份可读、可手改的 Markdown（默认 `memory/PROFILE.md`，已加入 `.gitignore`）：

- **手写/Agent 归纳**：`用户画像`、`兴趣与关注`、`跟踪任务`、`阅读偏好`、`需要避免`、`反馈与整改`、`Agent 笔记`。
- **自动画像**：`行为画像（自动）` 由真实统计数据每 6 小时重建一次，位于 `<!-- AUTO-PROFILE:START -->` 标记内，不会被 LLM 反思覆盖。
- **共享**：对话、采集、推荐、简报、记忆管家五个 Agent 都读取同一份记忆，只是注入的段落与预算不同。例如用户说「跟踪 AI 新闻」，采集 Agent 的搜索规划会优先覆盖 AI 细分方向。
- **安全**：记忆写入使用原子替换与进程内锁；用户可以在 `/chat` 右侧面板查看、编辑、重置。记忆内容被当作数据，不会被当成系统指令。

### 反馈闭环

- 支持对象：对话回答（`message`）、新闻（`news`）、推荐（`recommend`）与泛化反馈（`chat`）。
- 原因：`irrelevant` / `shallow` / `duplicate` / `outdated` / `inaccurate` / `source` / `too_long` / `too_hard` / `helpful`。
- 即时动作：写入确定性记忆（含整改建议）+ 推荐打分惩罚（来源/标签）。
- 后台反思：若配置了大语言模型，`AGENT_MEMORY_REFLECT=true` 时会在反馈后异步归纳并重写画像/兴趣/偏好/避免/笔记段落；失败会自动重试，3 次后标记失败。

### 个性化推荐

`/my/recommend` 与首页「为你推荐」从**上一批采集**中挑出最值得看的 10 篇，并综合：

1. 标签偏好（阅读 ×1、收藏 ×2.4、稍后 ×1.2）；
2. 常看来源、关注话题（来自结构化标签）；
3. 与最近阅读的关联新闻、共享标签；
4. 时效衰减、可信度；
5. 负反馈惩罚（不相关/来源/标签）与正反馈加成；
6. 主标签/来源多样性约束，避免信息茧房；
7. 每篇推荐附 2-3 条相关新闻（来自推荐 Agent 计算的关联，缺失时回退共享标签）。

每条推荐都会返回 `reason`，并在页面上展示。

### 采集质量

每轮采集的流水线：

1. **搜索规划**：读取关注标签、记忆与近 14 天已覆盖标题，由模型为每个标签生成 1-2 条差异化查询，并保留一条泛热点查询；无模型时退化为「标签 + 记忆关注词」。
2. **原文抓取**：并发抓取 HTML，抽取标题、正文与发布时间；带 SSRF 防护与大小限制。
3. **去重**：URL 规范化、标题指纹（Jaccard/序列相似度）、可选语义向量近邻。
4. **可信度**：域名等级、HTTPS、标题党/低价值词、摘要长度、发布时间、原文抓取结果、跨来源交叉出现等信号，0-100 分；低于 `AGENT_MIN_CREDIBILITY` 不收录。
5. **结构化分析**：一次模型调用输出摘要、要点、主题标签、实体、为什么值得关注与 Markdown 正文；原文抓取失败时明确标注。
6. **入库**：标签、可信度、结构化分析写入 `news.tags` / `news.credibility` / `news.analysis`，详情页展示可信度徽章、主题标签、要点与「为什么值得关注」。

### 记忆 / 反馈 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memory` | 读取记忆、分段、画像、最近反馈 |
| PUT | `/api/memory` | 保存整份记忆（用户手动编辑） |
| POST | `/api/memory/refresh` | 强制刷新画像，并反思未消化反馈 |
| DELETE | `/api/memory` | 重置记忆为模板 |
| GET | `/api/feedback` | 最近反馈列表（可按 `status` 过滤） |
| GET | `/api/feedback/reasons` | 反馈原因与整改说明 |
| POST | `/api/feedback` | 新增反馈（`target_type` / `target_id` / `rating` / `reason` / `comment`） |

> 外部 Agent 也可以调用这些接口读写同一份记忆，实现「用户对对话助手说跟踪 AI，搜索助手同步专注 AI」。

### RAG 评估（暂不引入）

当前记忆规模（个人使用通常 < 30KB）整段注入比 RAG 更可靠、更便宜，也更容易让用户审阅和纠正；新闻检索本身已经有 FTS + 向量 + RRF。等到以下情况再引入记忆 RAG：多用户/多画像、记忆超过 5 万 token、需要按时间线检索历史对话与反馈，或要做实体图谱。届时可复用现有 embedding 基础设施，为 `feedback` 与历史对话建向量索引，采用「结构化画像 + 相关记忆检索 + 重排」的混合方案，而不是把整份记忆塞进上下文。


## Agent 协同

项目里有五个各司其职的 Agent，对话助手可以在需要时把任务委派给它们：

| Agent | 职责 | 对话中的触发方式 |
|---|---|---|
| 对话助手 | 理解需求、检索、组织回答 | 直接聊天 |
| 采集 Agent | 搜索 → 撰稿 → 封面 → 向量索引 | “帮我采集新闻 / 更新新闻库” → `delegate_collect` |
| 简报 Agent | 把当日全部新闻汇总成一篇统一简报 | “给我一份简报 / 总结今天新闻” → `delegate_brief` |
| 推荐 Agent | 计算新闻相关性 | “更新相关推荐” → `delegate_recommend` |
| 记忆管家 | 用户画像 / 兴趣 / 跟踪任务 / 反馈整改 | 对话 `remember` / `forget_memory`，阅读自动刷新，反馈后台反思 |

- 五个 Agent 共用同一个 `memory/PROFILE.md`：对话注入对话相关段落，采集注入关注标签/跟踪任务/避免项，推荐注入画像与反馈，简报注入兴趣偏好，记忆管家负责反思重写。
- 委派任务会在对话里实时显示进度（如“采集 Agent 正在搜索…”），完成后由对话助手基于真实结果汇报。
- 采集/简报/推荐与定时任务共用一把任务锁：同一时间只跑一个，避免重复写库、重复调用模型。被占用时会如实告知，而不是排队等待。
- 可通过 `AGENT_ALLOW_DELEGATION=false` 关闭委派能力。

> **为什么新增「记忆管家」而不是让对话助手当主 Agent？** 对话助手是前台交互，反馈反思是后台异步、可能耗时数十秒的任务。拆成独立 Agent 后：反馈不会阻塞对话；记忆反思可以有自己的提示词、开关与失败重试；采集/推荐/简报也能复用同一套记忆而不依赖对话是否在线。对话助手仍然负责理解需求与委派，是用户唯一的主要入口。

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
- **本地向量库怎么选**：默认的 BLOB + numpy 就是「像 SQLite 一样零配置」的本地向量库，向量和新闻在同一个数据库文件里，不需要额外服务。只有在单进程内存或延迟成为瓶颈时，才建议引入 `sqlite-vec`（同一 SQLite 文件内的 vec0 表，约 0.1–1MB 扩展）作为可选加速后端；LanceDB / Chroma 会引入第二个数据目录与数百 MB 依赖，FAISS 只是索引库、没有元数据与持久化，当前规模都不划算。
- **过滤精度**：BLOB 后端会先取全部允许 ID，再做精确掩码和 `argpartition`，避免「先取全局 top-N 再按时间/标签过滤」导致的时间窗口召回塌陷。

### 时间线检索

「过去半个月 AI 发展历程」这类问题不再从 12 条全局相关结果里硬答：

- `GET /api/retrieval/timeline?q=AI&range=过去半个月&tag=AI芯片` 按天/周分桶召回，每桶内做 FTS/LIKE + 向量 + RRF；
- 跨桶按 URL/标题去重，返回 `timeline`、`buckets` 与 `coverage`（有新闻的天数、空桶、语义后端）；
- 对话助手新增 `timeline_news` 工具，遇到「发展历程 / 演变 / 脉络 / 最近 X 天」时优先调用，并按日期引用每条事件。

### 标签与首页 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tags` | 标签总览（数量、颜色、是否关注） |
| POST | `/api/tags/follow` | 关注 / 取消关注 `{tag, follow}` |
| GET | `/api/home` | 最新采集批次新闻 + 批次标签 + 个性化推荐 |
| GET | `/api/brief/daily` | 统一每日简报（`date` 可选，默认最新一期） |
| GET | `/api/brief/daily/dates` | 有简报的日期列表 |
| GET | `/api/news?tag=AI芯片` | 按标签筛选新闻（分页、日期、已读状态） |
| GET | `/api/news?batch=latest` | 最新采集批次 |

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

> 脚本会挂载五个持久化目录：`database`（SQLite）、`logs`（运行日志，每天自动归档一份，保留 30 天）、`images`（封面/头像等资源）、`memory`（长期记忆与画像）、`.env`（配置），容器重建不会丢失数据。宿主端口默认使用不常用的五位端口 `18080`（容器内为 8000）；若冲突，可用 `CRON_NEWS_PORT=18090 bash deploy.sh`（需同时修改 `docker-compose.yml` 的端口映射）。执行脚本前若检测到同名容器 `cron_news` 或镜像，会要求确认（可能是升级或你自己的同名项目）。

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
  -v $(pwd)/memory:/app/memory \
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

访问 <http://localhost:8000>。首次启动会自动创建 SQLite 数据库、标签索引表与默认用户名；标签会在首次采集后由 Agent 自动生成。

---

## 快速上手

1. 打开 <http://localhost:18080>（Docker 部署默认端口，直接启动为 8000），首次进入选择工作模式（辅助 / 自主）。
2. 在右上角 **设置** 中完成配置：
   - 首次打开会进入**快速开始向导**，按步骤填写工作模式、模型服务、采集频率与封面方式；也可以点「跳过向导」稍后在设置里配置。
   - **模型与服务配置**：新增并启用一个大语言模型、一个搜索服务（自主模式必需）；封面想要写实效果再启用一个文生图模型，否则用大模型 SVG 即可。
   - **关注标签**：在首页标签栏或「设置 → 关注标签」中关注主题；采集 Agent 会优先围绕这些标签搜索。
   - **自动任务配置**：设定采集频率与关注标签。
3. 自主模式下点击「立即执行」或等待定时任务，Agent 会自动采集新闻、撰写正文并生成封面 / 简报。

---

## 常见问题

- **自主模式运行需要什么？** 至少启用一个大语言模型和一个搜索服务，并填写对应服务商 API Key。
- **如何启用每日简报 / 智能推荐 / 自动生成封面？** 进入 **设置 → Agent 管理**，打开对应开关。封面方式可选「大模型 SVG」（只需大语言模型）或「文生图」（需先启用文生图模型）。
- **向导关掉了怎么再打开？** 设置菜单 →「配置向导」，随时可以重新走一遍。
- **提示词在哪里调整？** **设置 → Agent 管理 → 提示词管理**，可分别定制定时资讯、智能推荐、AI 简报、对话助手、记忆管家的系统提示词。
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
