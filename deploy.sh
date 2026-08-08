#!/usr/bin/env bash
#
# 定时资讯 · Cron News 一键部署脚本（Linux/macOS + Docker）
#
# 用法：git clone 项目后，在仓库根目录执行：
#     bash deploy.sh
#
# 流程：检查环境 → 生成 .env → 准备数据目录 → 构建镜像 → 启动容器 → 健康检查 → 状态报告
# 可配置项：
#     CRON_NEWS_PORT=<宿主机端口> bash deploy.sh   # 默认 8000，需与 docker-compose.yml 的端口映射一致
#
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
die()  { echo -e "${RED}[deploy] 错误：$*${NC}" >&2; exit 1; }

# ---------- 0. 目录定位：必须在仓库根目录运行 ----------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
info "工作目录：$ROOT"
[ -f docker-compose.yml ] || die "未找到 docker-compose.yml，请在仓库根目录运行本脚本"

# ---------- 1. 检查依赖 ----------
command -v docker >/dev/null 2>&1 || die "未检测到 docker，请先安装 Docker（https://docs.docker.com/get-docker/）"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
  warn "未检测到 docker compose 插件，将使用旧版 docker-compose，建议升级。"
else
  die "未检测到 docker compose，请安装 Docker Compose。"
fi
ok "Docker 环境就绪"

# ---------- 2. 准备 .env ----------
if [ ! -f .env ]; then
  [ -f .env.example ] || die "缺少 .env.example，无法生成配置"
  cp .env.example .env
  warn "已根据 .env.example 生成 .env，请编辑其中配置后再次运行本脚本。"
  warn "（SECRET_KEY 留空会自动生成；其余按需填写，之后在网页设置里维护模型配置即可）"
  exit 0
fi
ok "配置文件 .env 已存在"

# ---------- 3. 准备持久化目录（四个挂载目录） ----------
mkdir -p database logs images images/cover/default images/avatar
ok "数据目录已就绪：database/  logs/  images/"

# 检查默认图片资源是否齐全（默认封面 / 头像 / logo），缺失时警告
if [ ! -f images/sign.png ] || [ ! -f images/avatar/default-avatar.png ] || [ -z "$(ls images/cover/default/*.png 2>/dev/null)" ]; then
  warn "images 默认资源不完整（默认封面 / 头像 / logo），请确认已完整拉取代码（勿使用稀疏克隆或 LFS 跳过）。"
fi

# ---------- 4. 构建并启动 ----------
info "构建镜像并启动容器（首次构建可能需要几分钟）..."
"${COMPOSE[@]}" up -d --build
ok "容器已启动"

# ---------- 5. 等待健康检查 ----------
CONTAINER=cron-news
PORT="${CRON_NEWS_PORT:-8000}"
info "等待服务就绪（http://127.0.0.1:$PORT/health）..."
READY=0
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" != "1" ]; then
  warn "健康检查超时，最近日志："
  "${COMPOSE[@]}" logs --tail=40 "$CONTAINER" 2>&1 | tail -40 || true
  die "服务未能正常启动，请检查上方日志。"
fi
ok "服务已就绪 ✔  http://localhost:$PORT"

# ---------- 6. 容器与调度状态报告 ----------
echo
info "容器状态："
docker inspect -f '状态={{.State.Status}}  健康={{if .State.Health}}{{.State.Health.Status}}{{else}}未启用{{end}}' "$CONTAINER" 2>/dev/null \
  || "${COMPOSE[@]}" ps "$CONTAINER" 2>/dev/null || true
echo
info "定时任务状态（/api/scheduler/status）："
STATUS_JSON="$(curl -fsS "http://127.0.0.1:$PORT/api/scheduler/status" 2>/dev/null || true)"
if [ -n "$STATUS_JSON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    echo "$STATUS_JSON" | python3 -m json.tool 2>/dev/null || echo "$STATUS_JSON"
  else
    echo "$STATUS_JSON"
  fi
else
  echo "（获取状态失败，可稍后在网页「自动任务配置」查看）"
fi
echo
echo -e "${GREEN}部署完成。请在浏览器访问  http://<服务器IP>:$PORT${NC}"
info "查看运行日志：docker logs -f $CONTAINER   或   tail -f logs/app.log"
info "停止服务：${COMPOSE[*]} down      升级：重新执行 bash deploy.sh"
