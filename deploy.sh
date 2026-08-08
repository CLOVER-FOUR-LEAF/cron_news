#!/usr/bin/env bash
#
# 定时资讯 · Cron News 一键部署脚本（Linux/macOS + Docker）
#
# 用法：git clone 项目后，在仓库根目录执行：
#     bash deploy.sh
#
# 流程：检查环境 → 生成 .env → 检测同名容器/镜像 → 准备数据目录 → 构建/启动 → 健康检查 → 状态报告
# 可配置项：
#     CRON_NEWS_PORT=<宿主机端口> bash deploy.sh   # 默认 18080，需与 docker-compose.yml 的端口映射一致
#
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
die()  { echo -e "${RED}[deploy] 错误：$*${NC}" >&2; exit 1; }

# 交互式确认：返回 0=是 / 1=否
confirm() {
  local answer
  while true; do
    read -r -p "[deploy] $1 [y/N]: " answer
    case "${answer,,}" in
      y|yes) return 0 ;;
      ""|n|no) return 1 ;;
      *) echo "[deploy] 请输入 y 或 n" ;;
    esac
  done
}

CONTAINER=cron_news
IMAGE=cron_news:latest
PORT="${CRON_NEWS_PORT:-18080}"

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
  warn "已根据 .env.example 生成 .env。默认配置可直接部署："
  warn "  - SECRET_KEY 留空会自动生成；模型/搜索配置在网页「设置」中维护"
  warn "  - 如需连接独立数据库等，请先编辑 .env 后再重新运行本脚本"
fi
ok "配置文件 .env 已存在"

# ---------- 3. 检测同名容器 / 镜像 ----------
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  warn "检测到已存在的容器 $CONTAINER —— 可能是「升级」，也可能是你自己别的同名项目。"
  if confirm "是否继续？继续将替换重建该容器（数据保存在挂载目录 database/ logs/ images/ .env）"; then
    ok "确认继续，将替换重建容器 $CONTAINER"
  else
    die "已取消部署"
  fi
fi

REBUILD=0
if docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qx "$IMAGE"; then
  warn "检测到已存在的镜像 $IMAGE —— 可能是「升级」，也可能是你自己的同名项目。"
  if confirm "是否重新构建镜像？选择 n 将直接使用现有镜像启动"; then
    REBUILD=1
  else
    warn "使用现有镜像启动（不重新构建）"
  fi
else
  REBUILD=1
fi

# ---------- 4. 准备持久化目录（四个挂载目录） ----------
mkdir -p database logs images images/cover/default images/avatar
ok "数据目录已就绪：database/  logs/  images/"

# 检查默认图片资源是否齐全（默认封面 / 头像 / logo），缺失时警告
if [ ! -f images/sign.png ] || [ ! -f images/avatar/default-avatar.png ] || [ -z "$(ls images/cover/default/*.png 2>/dev/null)" ]; then
  warn "images 默认资源不完整（默认封面 / 头像 / logo），请确认已完整拉取代码（勿使用稀疏克隆或 LFS 跳过）。"
fi

# ---------- 5. 构建并启动 ----------
info "启动容器（端口 $PORT）..."
if [ "$REBUILD" = "1" ]; then
  info "构建镜像并启动容器（首次构建可能需要几分钟）..."
  "${COMPOSE[@]}" up -d --build
else
  "${COMPOSE[@]}" up -d
fi
ok "容器已启动"

# ---------- 6. 等待健康检查 ----------
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

# ---------- 7. 容器与调度状态报告 ----------
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
