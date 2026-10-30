# 首次访谈与初始化

目标：用最少的提问，得到一份可执行的画像：关注什么、避开什么、多久看一次、每次多少、要不要简报、内容口味。

## 1. 先静默推断（不要问用户）

按顺序读取：

```bash
GET /api/assist/status        # 模式、是否已初始化、schedule/editorial、最近推送
GET /api/memory               # 画像段落、行为画像、最近反馈
GET /api/tags?limit=40        # 关注标签、热门标签
GET /api/stats                # 总量、今日、未读、近 30 天
GET /api/feedback?limit=20    # 负反馈原因
GET /api/my/favorites         # 收藏
GET /api/my/later             # 稍后再读
GET /api/news?page_size=20    # 最近标题，判断关注面与来源
```

可以推断的：

| 信息 | 来源 |
|------|------|
| 已有关注主题 | `tags.followed`、`memory.sections.interests`、收藏/已读标签 |
| 跟踪任务 | `memory.sections.watch` |
| 阅读偏好 | `memory.sections.preferences`、`profile.totals`、`profile.reading_hours` |
| 需要避免 | `memory.sections.avoid`、`feedback` 的负反馈原因 |
| 来源偏好 | `profile.sources`、最近新闻的 `source` |
| 时间窗口 | 时区、`server_time`、最近推送时间 |
| 是否已初始化 | `onboarded`、`onboarding.schedule/editorial` |

**不要问已经存在于记忆、关注标签或反馈里的信息。** 只问缺失项。

## 2. 访谈脚本

### 第一轮（最多 5 个问题，一条消息发完）

1. **节奏**：你通常几点看资讯？希望我多久采集一次、每次大概几条？需要每天一篇简报吗，大概几点？
2. **主题**：用一句话说你最想跟的领域/公司/人物/产品；有没有明确不想看的内容？
3. **来源**：哪些媒体、行业博客或官方公告你信得过？哪些来源要避开？社媒爆料可以接受但必须标注吗？
4. **口味**：更喜欢快讯、深度稿还是混合？正文希望多长（短 300 / 标准 600 / 深读 900 字）？要不要背景、数据、观点？
5. **简报**：每日简报按默认五段（导语 / 今日要闻 / 串联与影响 / 值得继续关注 / 编辑说明），还是更短、更投资视角、更技术视角？

### 第二轮（只在必要时问，最多 4 个）

6. **重复**：同一事件有多篇报道时，保留信息最完整的一篇，还是保留 1-2 个不同角度？
7. **质量底线**：低可信来源是否一律不收？未证实爆料要不要收（会明确标注）？
8. **语言**：只看中文，还是中英来源都看并由你翻译/改写？英文标题要保留原文吗？
9. **反馈节奏**：你希望我每周复盘一次行为数据并自动调整，还是每次调整前先问你？

### 确认（不要跳过）

用 5 行复述：

```
我会这样工作：
- 节奏：每天 08:00 / 20:00 各一轮，每次 10-15 条；21:00 写每日简报
- 关注：AI 大模型、半导体、地缘政治；避开娱乐八卦和营销软文
- 来源：优先路透/彭博/财新/官方公告；聚合平台只作线索
- 口味：偏深度，正文 600-900 字，带背景、数据与影响
- 封面：暂不生成，使用默认封面
哪里不对？
```

用户确认后再写入。用户说「随便 / 默认 / 你定」时，使用默认值并明确告诉用户。

## 3. 写入初始化

调用 `POST /api/assist/onboarding`。完整示例：

```json
{
  "version": 1,
  "agent": "OpenClaw",
  "nickname": "小明",
  "sections": {
    "profile": "- 科技行业从业者，长期关注 AI 与半导体。\n- 阅读时间集中在早晚通勤与睡前。",
    "interests": "- AI 大模型\n- 半导体/芯片\n- 地缘政治\n- 美股科技公司",
    "watch": "- 跟踪英伟达与 OpenAI 的产品发布\n- 跟踪国产 GPU 进展",
    "preferences": "- 每天 08:00 / 20:00 各一轮，每次 10-15 条\n- 偏深度，正文 600-900 字\n- 优先路透/彭博/财新/官方公告\n- 简报固定五段式，21:00 生成",
    "avoid": "- 娱乐八卦\n- 营销软文\n- 标题党\n- 纯榜单/盘点",
    "notes": "- [2026-10-30] 外部 Agent 完成首次访谈；采集节奏 08:00 / 20:00"
  },
  "tags": ["AI大模型", "半导体", "地缘政治", "美股科技"],
  "schedule": {
    "timezone": "Asia/Shanghai",
    "times": ["08:00", "20:00"],
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "items_per_run": 12,
    "brief_time": "21:00",
    "quiet_hours": ["23:00", "07:00"],
    "catch_up": true
  },
  "editorial": {
    "language": "zh-CN",
    "depth": "deep",
    "article_length": [600, 900],
    "summary_length": [60, 150],
    "tone": "objective",
    "sources_include": ["reuters.com", "bloomberg.com", "caixin.com", "gov.cn"],
    "sources_exclude": ["营销号", "低质聚合"],
    "cover": "off",
    "brief_style": "five-section",
    "translation": "zh-only",
    "duplicates": "best-only",
    "quality_floor": 55,
    "allow_unverified": false
  },
  "replace_sections": false
}
```

### 记忆段落写法

- 只使用固定段落键：`profile` / `interests` / `watch` / `preferences` / `avoid` / `notes`；拼错的键会返回 400，不会被静默忽略。
- 每行一条，以 `- ` 开头；`write_section` 会自动补 `- `。
- 单行会截到 300 字；长偏好拆成多行，不要写成一整段。
- 日期用 `[YYYY-MM-DD]`，便于后续清理。
- 一条只写一件事；不要把整段文章塞进记忆。
- 不要写 API Key、密码、身份证、住址等敏感信息。
- 不要写 `feedback` 段落（平台维护）。
- 不要动 `<!-- AUTO-PROFILE:START --> ... END -->` 自动画像块。
- 用户画像与兴趣可以写得具体：公司、产品、人物、地区、行业。
- 避免写成「喜欢科技」这种空话；写成「关注 AI 芯片、先进制程、HBM 供应链」。

### 标签选择

- 建议 3-8 个，最多 20 个（全部生效），具体、可检索；优先实体/公司/产品/技术/事件主题。
- 复用平台已有标签名（`/api/tags`），减少同义分裂。
- 平台会丢弃固定泛词：新闻 / 资讯 / 热点 / 最新 / 综合 / 头条 / 要闻 / 推荐 / 文章 / 消息 / 动态 / 其他；「科技 / 财经 / 国际 / 军事 / 社会 / 体育 / 娱乐」平台会保留，但建议少用，优先更具体的标签。
- 每个标签 ≤30 字；关注标签不受「单条新闻最多 8 个标签」的限制。
- 关注标签是采集的优先方向，不是硬过滤；泛热点查询仍要保留。

### schedule / editorial 字段

`schedule` 由外部 Agent 自己解释，平台只保存：

| 字段 | 说明 |
|------|------|
| `timezone` | 用户时区，如 `Asia/Shanghai` |
| `times` | 每日采集时刻，`HH:MM` 数组 |
| `days` | 工作日/周末，`mon`…`sun` |
| `items_per_run` | 每轮目标条数 |
| `brief_time` | 每日简报时刻 |
| `quiet_hours` | 免打扰时段 |
| `catch_up` | 启动/离线后是否补跑 |

`editorial`：

| 字段 | 取值 / 说明 |
|------|-------------|
| `language` | `zh-CN` / `en` / `bilingual` |
| `depth` | `brief` / `balanced` / `deep` |
| `article_length` | 正文字数区间，如 `[600, 900]` |
| `summary_length` | 摘要字数区间，如 `[60, 150]` |
| `tone` | `objective`（默认） |
| `sources_include` | 优先域名/媒体 |
| `sources_exclude` | 排除来源 |
| `cover` | `off` / `image` / `svg` |
| `brief_style` | `five-section` / `short` / `investment` / `technical` |
| `translation` | `zh-only` / `translate-en` |
| `duplicates` | `best-only` / `two-angles` |
| `quality_floor` | 可信度下限，建议 55 |
| `allow_unverified` | 是否允许未证实爆料（需标注） |

## 4. 初始化之后

1. `GET /api/assist/status` 确认 `onboarded: true`、`tags`、`onboarding.schedule/editorial`。
2. 立刻跑一轮小采集（8-12 条），让用户看到效果。
3. 告诉用户：更新节奏、下一次运行时间、如何反馈（收藏/已读/点踩）、如何修改偏好（直接对 Agent 说）。
4. 把 `schedule` / `editorial` 存入外部 Agent 自己的持久状态；下次会话先读 `/api/assist/status` 的 `onboarding`，不要重复访谈。

## 5. 修改偏好（不是首次）

- 用户说「以后多给我看 X / 少看 Y」：`PATCH /api/memory` 追加到 `interests` / `avoid` / `preferences`。
- 用户说「关注/取消关注 X」：`POST /api/tags/follow`。
- 用户改时间/条数/简报时间：重新 `POST /api/assist/onboarding`，只传要改的 `schedule` / `editorial`（未传的字段保留上次状态），并 `PATCH /api/memory` 的 `notes` 记一条变更。
- 用户要求「忘掉 X」：`PATCH /api/memory` 用 `replace` 重写对应段落，去掉 X；不要整篇删除。
- 用户要求重置外部 Agent 的初始化状态：`DELETE /api/assist/onboarding` 清除 `memory/ASSIST.json`，下次会重新访谈；记忆与关注标签保留。
- 用户要求重置全部记忆：先确认，再 `DELETE /api/memory`（重置 `PROFILE.md`；不会删除 `ASSIST.json` 与关注标签）。需要两者都重置时，两个接口都调用。

## 6. 模式冲突

- `work_mode == "autonomous"`：停止写入，告诉用户平台正在自主运行。**不要抢采集**。
- 用户同意切换：`PUT /api/config {"work_mode":"assist"}`，再读 `/api/assist/status` 确认。
- 用户切回自主：暂停你的定时推送；保留状态；等用户再次切换回辅助后，从 `latest_news.collected_at` 或你自己的 `last_successful_push_at` 续跑。
- 切换不会丢失数据；外部推送的新闻、记忆、图片、阅读记录都会保留。

## 7. 常见提问误区

| 不好 | 更好 |
|------|------|
| 「请填写：兴趣、来源、频率、条数、深度、语言、封面、简报风格、重复策略、质量底线……」 | 先问 5 个核心问题，其余用默认值，确认时再让用户改 |
| 「你要看什么新闻？」 | 「用一句话说你最想跟的领域/公司/人物；有没有明确不想看的？」 |
| 重复问记忆里已有的「关注 AI」 | 直接说「我看到你已关注 AI 大模型和半导体，还需要补充别的吗？」 |
| 让用户填 20 个标签 | 给 3-8 个候选标签，让用户增删 |
| 不确认就写入 | 先复述 5 行，问「哪里不对？」 |
| 用户说「随便」还继续追问 | 使用默认值，明确告诉用户默认是什么，之后可改 |
