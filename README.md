# 定时资讯 · Cron News

一个 AI 新闻聚合展示平台。支持**自主模式**下由内置 Agent 自动联网采集、撰稿、生成封面与每日简报；也支持**辅助模式**下作为数据中枢，供外部 Agent（如 OpenClaw、Hermes）通过 API 写入与读取资讯。

---

## 主要功能

- **新闻聚合展示**：首页轮播、分类页、详情页（Markdown 渲染）、全文搜索、日期筛选。
- **阅读管理**：未读 / 在读 / 已读三态追踪，收藏与稍后再读，个人阅读统计面板。
- **每日简报**：按分类汇总当日新闻自动生成简报，可在简报页编辑、写便签、导出 Markdown / Word。
- **智能推荐**：基于 AI 相关性分析，在详情页跨分类推荐真正相关的内容。
- **自动生成资讯封面**：资讯助手调用文生图模型为新增新闻生成封面；关闭时随机使用系统默认封面。
- **模型与服务统一管理**：大语言、文生图、搜索三类配置统一入库，仅需在界面维护各服务商 API Key。

## Agent 相关能力

| 能力 | 说明 |
|------|------|
| **工作模式切换** | 辅助 / 自主两种模式，随时可在设置中切换 |
| **自主模式** | 内置 Agent 按设定频率调用搜索与大模型 API，自动采集、撰写正文、生成推荐与简报 |
| **辅助模式** | 项目本身不调用模型，作为展示平台，通过 REST API 供外部 Agent 推送新闻、写入简报 |
| **Agent 配置管理** | 分类、大语言/文生图/搜索配置、定时任务、系统提示词、功能开关（智能推荐/每日简报/自动生成封面）均可在界面管理 |
| **数据库选择** | 内置 SQLite 零配置开箱即用，也可切换到自建的 MySQL / MariaDB / PostgreSQL（支持一键迁移） |

> 外部 Agent 对接文档见 `skill/SKILL.md`（推送新闻、写入简报、模型配置等接口说明）。

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

3. 访问平台：<http://localhost:8000>。

> 脚本会挂载四个持久化目录：`database`（SQLite）、`logs`（运行日志，每天自动归档一份，保留 30 天）、`images`（封面/头像等资源）、`.env`（配置），容器重建不会丢失数据。若宿主机端口冲突，可用 `CRON_NEWS_PORT=8080 bash deploy.sh`（需同时修改 `docker-compose.yml` 的端口映射）。

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

4. 访问平台：<http://localhost:8000>

**数据持久化**：`database`（SQLite）、`logs`（日志）、`images`（图片资源）与 `.env` 配置文件已通过 `docker-compose.yml` 挂载到宿主机，容器重建不会丢失数据。

**其他 Docker 用法**：

```bash
# 手动构建并运行
docker build -t cron-news .
docker run -d -p 8000:8000 \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/database:/app/database \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/images:/app/images \
  --name cron-news cron-news

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

1. 打开 <http://localhost:8000>，首次进入选择工作模式（辅助 / 自主）。
2. 在右上角 **设置** 中完成配置：
   - **模型与服务配置**：新增并启用一个大语言模型、一个搜索服务（自主模式必需）；如需自动生成封面，再启用一个文生图模型。
   - **分类管理**：维护导航栏分类。
   - **自动任务配置**：设定采集频率与目标分类。
3. 自主模式下点击「立即执行」或等待定时任务，Agent 会自动采集新闻、撰写正文并生成封面 / 简报。

---

## 常见问题

- **自主模式运行需要什么？** 至少启用一个大语言模型和一个搜索服务，并填写对应服务商 API Key。
- **如何启用每日简报 / 智能推荐 / 自动生成封面？** 进入 **设置 → Agent 管理**，打开对应开关（自动生成封面需要先启用一个文生图模型）。
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
