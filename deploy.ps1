# 定时资讯 · Cron News 一键部署脚本（Windows PowerShell）
#
# 用法：git clone 项目后，在仓库根目录执行：
#     powershell -ExecutionPolicy Bypass -File deploy.ps1
#
# 流程：检查环境 -> 生成 .env -> 检测同名容器/镜像 -> 准备数据目录 -> 构建/启动 -> 健康检查 -> 状态报告
$ErrorActionPreference = 'Stop'

$Red = [ConsoleColor]::Red
$Green = [ConsoleColor]::Green
$Yellow = [ConsoleColor]::Yellow
$Cyan = [ConsoleColor]::Cyan

$Container = 'cron_news'
$Image = 'cron_news:latest'
$Port = if ($env:CRON_NEWS_PORT) { $env:CRON_NEWS_PORT } else { 18080 }

function Confirm-YesNo([string]$Prompt) {
    while ($true) {
        $Answer = Read-Host "[deploy] $Prompt [y/N]"
        if ($Answer -match '^(y|yes)$') { return $true }
        if ($Answer -match '^(n|no)$' -or [string]::IsNullOrWhiteSpace($Answer)) { return $false }
        Write-Host "[deploy] 请输入 y 或 n" -ForegroundColor $Yellow
    }
}

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
    Write-Host "[deploy] 已根据 .env.example 生成 .env。默认配置可直接部署：" -ForegroundColor $Yellow
    Write-Host "[deploy]   - SECRET_KEY 留空会自动生成；模型/搜索配置在网页「设置」中维护" -ForegroundColor $Yellow
    Write-Host "[deploy]   - 如需连接独立数据库等，请先编辑 .env 后再重新运行本脚本" -ForegroundColor $Yellow
}
Write-Host "[deploy] 配置文件 .env 已存在" -ForegroundColor $Green

# ---------- 3. 检测同名容器 / 镜像 ----------
$ExistingContainers = docker ps -a --format "{{.Names}}" 2>$null
if ($ExistingContainers -contains $Container) {
    Write-Host "[deploy] 警告: 检测到已存在的容器 $Container —— 可能是「升级」，也可能是你自己别的同名项目。" -ForegroundColor $Yellow
    if (-not (Confirm-YesNo "是否继续？继续将替换重建该容器（数据保存在挂载目录 database/ logs/ images/ .env）")) {
        Write-Host "[deploy] 已取消部署" -ForegroundColor $Red
        exit 1
    }
    Write-Host "[deploy] 确认继续，将替换重建容器 $Container" -ForegroundColor $Green
}

$Rebuild = $false
$ExistingImages = docker images --format "{{.Repository}}:{{.Tag}}" 2>$null
if ($ExistingImages -contains $Image) {
    Write-Host "[deploy] 警告: 检测到已存在的镜像 $Image —— 可能是「升级」，也可能是你自己的同名项目。" -ForegroundColor $Yellow
    if (Confirm-YesNo "是否重新构建镜像？选择 n 将直接使用现有镜像启动") {
        $Rebuild = $true
    } else {
        Write-Host "[deploy] 使用现有镜像启动（不重新构建）" -ForegroundColor $Yellow
    }
} else {
    $Rebuild = $true
}

# ---------- 4. 准备持久化目录 ----------
New-Item -ItemType Directory -Force -Path database, logs, images, "images\cover\default", "images\avatar" | Out-Null
Write-Host "[deploy] 数据目录已就绪: database/  logs/  images/" -ForegroundColor $Green

# 检查默认图片资源是否齐全（默认封面 / 头像 / logo）
$MissingDefaultImages = -not (Test-Path images\sign.png) -or -not (Test-Path images\avatar\default-avatar.png) -or @(Get-ChildItem images\cover\default\*.png -ErrorAction SilentlyContinue).Count -eq 0
if ($MissingDefaultImages) {
    Write-Host "[deploy] 警告: images 默认资源不完整（默认封面 / 头像 / logo），请确认已完整拉取代码（勿用稀疏克隆或 LFS 跳过）。" -ForegroundColor $Yellow
}

# ---------- 5. 构建并启动 ----------
Write-Host "[deploy] 启动容器（端口 $Port）..." -ForegroundColor $Cyan
if ($Rebuild) {
    Write-Host "[deploy] 构建镜像并启动容器（首次构建可能需要几分钟）..." -ForegroundColor $Cyan
    docker compose up -d --build
} else {
    docker compose up -d
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "[deploy] 错误: docker compose 启动失败" -ForegroundColor $Red
    exit 1
}
Write-Host "[deploy] 容器已启动" -ForegroundColor $Green

# ---------- 6. 等待健康检查 ----------
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
    docker compose logs --tail=40 $Container 2>$null
    Write-Host "[deploy] 错误: 服务未能正常启动" -ForegroundColor $Red
    exit 1
}
Write-Host "[deploy] 服务已就绪 ✔  http://localhost:$Port" -ForegroundColor $Green

# ---------- 7. 状态报告 ----------
Write-Host ""
Write-Host "[deploy] 容器状态:" -ForegroundColor $Cyan
docker inspect --format "状态={{.State.Status}}  健康={{if .State.Health}}{{.State.Health.Status}}{{else}}未启用{{end}}" $Container 2>$null
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
Write-Host "[deploy] 查看运行日志: docker logs -f $Container    停止: docker compose down    升级: 重新运行本脚本" -ForegroundColor $Cyan
