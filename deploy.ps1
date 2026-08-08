# 定时资讯 · Cron News 一键部署脚本（Windows PowerShell）
#
# 用法：git clone 项目后，在仓库根目录执行：
#     powershell -ExecutionPolicy Bypass -File deploy.ps1
#
# 流程：检查环境 -> 生成 .env -> 准备数据目录 -> 构建镜像 -> 启动容器 -> 健康检查 -> 状态报告
$ErrorActionPreference = 'Stop'

$Red = [ConsoleColor]::Red
$Green = [ConsoleColor]::Green
$Yellow = [ConsoleColor]::Yellow
$Cyan = [ConsoleColor]::Cyan

# ---------- 0. 目录定位 ----------
Set-Location -LiteralPath $PSScriptRoot
Write-Host "[deploy] 工作目录: $PWD" -ForegroundColor $Cyan
if (-not (Test-Path docker-compose.yml)) {
    Write-Host "[deploy] 错误: 未找到 docker-compose.yml，请在仓库根目录运行本脚本" -ForegroundColor $Red
    exit 1
}

# ---------- 1. 检查依赖 ----------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[deploy] 错误: 未检测到 docker，请先安装 Docker Desktop" -ForegroundColor $Red
    exit 1
}
Write-Host "[deploy] Docker 环境就绪" -ForegroundColor $Green

# ---------- 2. 准备 .env ----------
if (-not (Test-Path .env)) {
    if (-not (Test-Path .env.example)) {
        Write-Host "[deploy] 错误: 缺少 .env.example" -ForegroundColor $Red
        exit 1
    }
    Copy-Item .env.example .env
    Write-Host "[deploy] 已根据 .env.example 生成 .env，请编辑其中配置后重新运行本脚本。" -ForegroundColor $Yellow
    exit 0
}
Write-Host "[deploy] 配置文件 .env 已存在" -ForegroundColor $Green

# ---------- 3. 准备持久化目录 ----------
New-Item -ItemType Directory -Force -Path database, logs, images, "images\cover\default", "images\avatar" | Out-Null
Write-Host "[deploy] 数据目录已就绪: database/  logs/  images/" -ForegroundColor $Green

# 检查默认图片资源是否齐全（默认封面 / 头像 / logo）
$MissingDefaultImages = -not (Test-Path images\sign.png) -or -not (Test-Path images\avatar\default-avatar.png) -or @(Get-ChildItem images\cover\default\*.png -ErrorAction SilentlyContinue).Count -eq 0
if ($MissingDefaultImages) {
    Write-Host "[deploy] 警告: images 默认资源不完整（默认封面 / 头像 / logo），请确认已完整拉取代码（勿用稀疏克隆或 LFS 跳过）。" -ForegroundColor $Yellow
}

# ---------- 4. 构建并启动 ----------
Write-Host "[deploy] 构建镜像并启动容器（首次构建可能需要几分钟）..." -ForegroundColor $Cyan
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "[deploy] 错误: docker compose 启动失败" -ForegroundColor $Red
    exit 1
}
Write-Host "[deploy] 容器已启动" -ForegroundColor $Green

# ---------- 5. 等待健康检查 ----------
$Port = if ($env:CRON_NEWS_PORT) { $env:CRON_NEWS_PORT } else { 8000 }
Write-Host "[deploy] 等待服务就绪 (http://127.0.0.1:$Port/health)..."
$Ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $Resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
        if ($Resp.StatusCode -eq 200) { $Ready = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $Ready) {
    Write-Host "[deploy] 健康检查超时，最近日志:" -ForegroundColor $Yellow
    docker compose logs --tail=40 cron-news 2>$null
    Write-Host "[deploy] 错误: 服务未能正常启动" -ForegroundColor $Red
    exit 1
}
Write-Host "[deploy] 服务已就绪 ✔  http://localhost:$Port" -ForegroundColor $Green

# ---------- 6. 状态报告 ----------
Write-Host ""
Write-Host "[deploy] 容器状态:" -ForegroundColor $Cyan
docker inspect --format "状态={{.State.Status}}  健康={{if .State.Health}}{{.State.Health.Status}}{{else}}未启用{{end}}" cron-news 2>$null
Write-Host ""
Write-Host "[deploy] 定时任务状态 (/api/scheduler/status):" -ForegroundColor $Cyan
try {
    $S = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/scheduler/status" -TimeoutSec 5
    $S | ConvertTo-Json -Depth 5
} catch {
    Write-Host "(获取状态失败，可稍后在网页「自动任务配置」查看)"
}
Write-Host ""
Write-Host "[deploy] 部署完成。请在浏览器访问  http://localhost:$Port" -ForegroundColor $Green
Write-Host "[deploy] 查看运行日志: docker logs -f cron-news    停止: docker compose down    升级: 重新运行本脚本" -ForegroundColor $Cyan
