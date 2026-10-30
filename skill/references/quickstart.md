# 快速开始 · 15 分钟接入

目标：让外部 Agent 在辅助模式下完成首次访谈、推送第一批新闻、写一篇简报，并能持续运行。

## 0. 确认地址与模式

```bash
BASE=http://localhost:18080   # Docker 默认宿主端口；直接启动通常是 8000
curl -s "$BASE/health" | jq
curl -s "$BASE/api/assist/status" | jq '{work_mode, onboarded, stats, latest_batch}'
```

- `work_mode` 必须是 `assist`。如果是 `autonomous`，请用户在「设置 → 工作模式」切换；不要擅自切换。
- 切换不会丢失新闻、记忆、图片或阅读记录。

## 1. 读取现有信息（不要重复问用户）

```bash
curl -s "$BASE/api/memory" | jq '.sections'
curl -s "$BASE/api/tags?limit=40" | jq '{followed, items: [.items[] | {tag, count}]}'
curl -s "$BASE/api/stats" | jq '{total, today_count, unread_count}'
curl -s "$BASE/api/feedback?limit=20" | jq '.items'
```

能从这些数据推断的偏好，不要再问。

## 2. 访谈

按 `references/onboarding.md` 的脚本，一轮最多 5 个问题。至少确认：

1. 关注领域 / 想避开的内容；
2. 可信来源 / 语言；
3. 更新频率、时段、每次条数；
4. 是否需要每日简报、大概几点；
5. 内容深度与封面偏好。

用户回答「默认」时使用默认值：早 8 点 / 晚 8 点、每次 12 条、正文 600-900 字、每日简报 21:00、封面关闭。

## 3. 写入初始化

```bash
curl -s -X POST "$BASE/api/assist/onboarding" \
  -H 'Content-Type: application/json' -d @onboarding.json | jq
```

`onboarding.json` 的完整示例见 `assets/onboarding.example.json`；访谈与字段说明见 `references/onboarding.md`。确认：

```bash
curl -s "$BASE/api/assist/status" | jq '{onboarded, onboarding, tags}'
```

## 4. 推送第一条新闻

先查重：

```bash
curl -s "$BASE/api/news/check?url=https%3A%2F%2Fexample.com%2Fnews&title=示例标题" | jq
```

再推送（完整字段见 `assets/news-payload.example.json`）：

```bash
curl -s -X POST "$BASE/api/news" \
  -H 'Content-Type: application/json' -d @news.json | jq '{id, title, batch_id, cover_url, tags}'
```

用返回的 `id` 读回：

```bash
curl -s "$BASE/api/news/123" | jq '{title, tags, credibility, analysis, cover_url}'
```

## 5. 生成封面（可选）

远程 Agent 用上传接口：

```bash
curl -s -X POST "$BASE/api/media" -F 'file=@cover.png' | jq
# → {"url": "/images/uploads/xxxx.png", ...}
```

把 `url` 填进新闻的 `cover_url` 再推送；平台会复制归档到 `/images/cover/{新闻id}.<ext>`。

## 6. 写每日简报

```bash
curl -s -X POST "$BASE/api/brief" -H 'Content-Type: application/json' -d '{
  "tag": "每日",
  "date": "2026-10-30",
  "source": "外部",
  "content": "# 每日简报 · 2026-10-30\n\n导语……\n\n## 今日要闻\n\n### …\n\n…"
}' | jq

curl -s "$BASE/api/brief/daily?date=2026-10-30" | jq '{date, source, content}'
```

## 7. 建立自己的定时

平台在辅助模式下不调度。用宿主 Agent 的 cron/心跳，或系统 cron 调用：

```bash
python3 skill/scripts/cron_news.py --base "$BASE" status
python3 skill/scripts/cron_news.py --base "$BASE" push --file news.json
```

每轮开始前读 `GET /api/assist/status`；用户切到自主模式时暂停写入，切回辅助后从 `latest_news.collected_at` 续跑。

## 8. 接入完成检查表

- [ ] `work_mode == assist`
- [ ] `onboarded == true`，记忆段落与关注标签正确
- [ ] 已推送至少 8-12 条，`GET /api/news?batch=<batch_id>` 能查到
- [ ] 封面返回 `/images/cover/{id}.ext`（如果生成了封面）
- [ ] 当日简报可读回，`source == 外部`
- [ ] 外部 Agent 有自己的定时与 `last_successful_push_at`
- [ ] 已约定反馈复盘频率（建议每周一次）
