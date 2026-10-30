# 故障排查

## 先做三件事

```bash
curl -s "$BASE/health"
curl -s "$BASE/api/assist/status" | jq '{work_mode, onboarded, stats, latest_news, feedback: .feedback.pending}'
curl -s "$BASE/api/news?page_size=5" | jq '{total, items: [.items[] | {id,title,collected_at,batch_id}]}'
```

大部分问题都能从这三步定位：模式不对、没有写入、时间窗不对、批次不对。

---

## 1. 初始化被拒绝（409）

**现象**：`POST /api/assist/onboarding` 返回 409，`detail` 提示当前不是辅助模式。

**原因**：`WORK_MODE` 是 `autonomous` 或空。

**处理**：

1. 读 `GET /api/assist/status` 确认 `work_mode`；
2. 请用户在「设置 → 工作模式」切到辅助；
3. 用户明确同意后，才可以 `PUT /api/config {"work_mode":"assist"}`；
4. 切换不会丢失数据；切回后重新 `POST /api/assist/onboarding`。

---

## 2. 资讯墙为空

**现象**：辅助模式首页没有新闻，或只有旧新闻。

**排查顺序**：

1. `GET /api/assist/status` 的 `latest_news` 是否为空？为空说明平台还没收到任何新闻。
2. `GET /api/news?page_size=5` 是否有数据？有数据但资讯墙为空 → 时间窗口太窄；点「近 30 天」或 `GET /api/assist/feed?days=30`。
3. `GET /api/assist/feed?days=7` 的 `items` 是否有数据？
4. 新闻的 `collected_at` 是否写成了原文发布时间（几个月前）？如果是，资讯墙按时间窗口就看不到。`collected_at` 应使用发现时间。
5. 浏览器缓存：刷新 / 检查 `style.css` 版本；资讯墙每 45 秒轮询一次。

**处理**：

- 没有新闻 → 跑一轮采集并推送；
- 时间窗太窄 → 用 `days=30` 或让 Agent 补推最近内容；
- `collected_at` 错误 → `PATCH /api/news/{id}` 更正（注意 `collected_at` 不在可更新字段里；必要时删除重推）；
- 首页显示旧数据 → 确认 `GET /api/assist/status` 的 `latest_news` 时间。

---

## 3. 推送成功但首页「上一批」看不到

**原因**：`/api/home` 按 `latest_batch_id` 取批次；外部推送没有 `batch_id`，或批次号比平台内置批次旧。

**处理**：

- 始终传 `batch_id`，每轮一个；
- 用 `GET /api/news?batch=<batch_id>&page_size=100` 核对；
- 辅助模式首页用 `/api/assist/feed`（时间窗口），不依赖批次；
- 如果平台里还有旧的自主批次，`/api/home?batch=latest` 可能仍指向它；这是预期行为，不要据此判断推送失败。

---

## 4. 封面没有生效

**现象**：推送返回的 `cover_url` 还是原值，或前端显示默认封面。

**原因**：

- 传了外链 URL（平台不下载外链）；
- 传了 `/images/...`，但文件不在平台挂载的 `images` 目录里；
- 文件名扩展名不在 `png/jpg/jpeg/webp/gif`；
- 远程 Agent 没有文件系统访问，却按「共享目录」方式传路径。

**处理**：

- 远程：先 `POST /api/media` 上传，拿返回的 `url` 再推送；
- 同机：确认文件在 `images/uploads/`（或 `images/cover/` 下唯一、非纯数字文件名），路径以 `/images/` 开头；
- `images/sign.png`、`images/avatar/`、`images/cover/default/`、`images/cover/{已有新闻id}.png` 会被拒绝；
- 推送后读回 `GET /api/news/{id}`，确认 `cover_url` 是 `/images/cover/{id}.<ext>`；
- 失败的封面不会阻塞新闻入库；可以之后 `PATCH /api/news/{id}` 更换。

---

## 5. 重复新闻

**原因**：没有推送前查重，或标题改写后相似度低于阈值，或同一事件不同角度。

**处理**：

- 每个候选先 `GET /api/news/check?url=...&title=...`；
- URL 规范化（去掉 utm 等跟踪参数）；
- 标题相似度 ≥0.82 视为重复；
- 同一事件如果是**实质进展**（新数字、新回应、新阶段），可以写新的一条，但标题要体现新进展，不要重复旧角度；
- 已重复：保留信息最完整的一条，其余 `DELETE /api/news/{id}`。

---

## 6. 记忆没保存 / `memory_saved: false`

**原因**：

- `AGENT_MEMORY_ENABLED=false`；
- 记忆目录不可写；
- 内容超过 60000 字上限；
- 初始化 `sections` 超过 30000 字。

**处理**：

- 检查 `GET /api/memory` 的 `enabled`；
- 缩短内容，分批 `PATCH /api/memory`；
- 记忆不可用时，外部 Agent 自己保存画像，并在回复中告诉用户；
- 不要在记忆里写超长文章或敏感信息。

---

## 7. 简报 404 / 简报页空白

**原因**：

- 还没写；
- `tag` 不是 `每日`；
- `date` 与平台日期不一致（时区）；
- 写到了旧标签（如 `AI芯片`），日报页不展示。

**处理**：

- `POST /api/brief` 时 `tag` 固定 `每日`，`source` 传 `外部`；
- 用 `GET /api/brief/daily?date=YYYY-MM-DD` 读回；
- 「今天」用平台服务器本地日期（默认 `Asia/Shanghai`）；跨时区时显式传 `date`；
- `GET /api/brief/daily/dates` 看已有日期。

---

## 8. 反馈状态一直是 `noted`

**原因**：辅助模式没有平台 LLM 反思；`noted` 表示已写入确定性记忆，等待外部 Agent 消化。

**处理**：

- 读取 `GET /api/feedback?status=noted`；
- 按原因调整采集/写作/来源；
- `PATCH /api/memory` 写入整改；
- `POST /api/feedback/{id}/ack` 标记 `processed`。

---

## 9. `POST /api/scheduler/trigger` 返回 409

**原因**：辅助模式不执行内置采集。

**处理**：不要调用；由外部 Agent 采集并推送。若用户想用平台内置采集，请切换到自主模式。

---

## 10. 400 / 422

| 错误 | 常见原因 | 处理 |
|------|----------|------|
| 400 空标签 | `tag` 归一化后为空 | 传具体标签，去掉 `#`、空格、泛词 |
| 400 工作模式非法 | `work_mode` 不是 `assist`/`autonomous` | 修正值 |
| 400 没有更新字段 | `PATCH /api/news/{id}` 空 body | 至少传一个字段 |
| 400 记忆段落不支持 | `PATCH /api/memory` 写了 `feedback` 或未知键 | 只用 `profile/interests/watch/preferences/avoid/notes` |
| 413 图片过大 | >8MB | 压缩或换图 |
| 422 字段校验失败 | 类型/长度/范围不对 | 检查 `title` 1-255、`credibility` 0-100、URL ≤500 等 |

---

## 11. 反向代理 / 子路径

如果平台部署在 `https://host/proxy/8000/`：

- 先设置 `APP_BASE_PATH=/proxy/8000`（或在平台 `.env` 中配置）；
- 外部 Agent 的 base URL 也要带前缀；
- Skill 里的相对路径（`/api/...`）由平台改写；外部 Agent 自己拼接时要用完整前缀；
- 如果返回的链接缺少前缀，检查代理是否保留原始 Host，或设置 `APP_BASE_PATH_ALWAYS=1`。

---

## 12. 时区

- 平台默认 `Asia/Shanghai`；`today_only`、简报日期、`server_time` 都用服务器本地时间。
- 外部 Agent 在别的时区时：
  - 采集时间可按用户时区安排；
  - **写简报时用平台本地日期**（`GET /api/assist/status` 的 `server_time` 日期），`today_only` 与 `POST /api/brief` 的 `date` 用同一个日期；
  - 不要用 UTC 的「今天」去筛选平台新闻，也不要用用户时区日期写平台简报，否则内容窗口与日期会错位。
- 需要改平台时区：`.env` 的 `APP_TIMEZONE`，重启生效。

---

## 13. 什么时候该问用户

- 模式不是辅助，需要用户决定是否切换；
- 用户偏好冲突（例如「只要中文」但又要求跟踪英文独家）；
- 连续多轮零收录，可能是主题太窄或来源不可达；
- 需要删除/重置记忆、批量撤回新闻；
- 需要把平台暴露到公网（先确认鉴权方案）。
