# 切换到 linux.do 官方 tag RSS 单层采集

## Goal

把采集第一层从第三方 RSSHub Telegram 镜像
(`https://rsshub.rssforever.com/telegram/channel/linux_do_channel`)
换成 linux.do 官方 tag RSS (`https://linux.do/tag/2234-tag/2234.rss`)。

该 feed 的 `<description>` 已含首帖完整正文，因此原本的第二层
(`/t/topic/<id>.rss` 逐个抓取) 不再需要 —— 发现即取得正文，直接进第三层 LLM 分类。

## What I already know

* **tag RSS 的 description 是首帖完整 cooked HTML**，非摘要。实测最长 11,765 字节，
  含代码块/引用/mention，无截断标记。见 [`research/discourse-tag-rss.md`](research/discourse-tag-rss.md)。
* **`<guid>` 形如 `{host}-topic-{id}`**，topic id 可直接解析，无需正则扫正文。
* **只有首帖，无回复**；**只有最新 30 条，无分页**（RFC 5005 官方未支持）。
* 现有 `feeds/topic.py::TopicFeed` / `RetainedPost` 可原样承载解析结果，
  下游 `save_topic_posts` → `_candidates_of` → `classifier.classify` 无需改动。
* `topic_posts` 表已有 `is_first_post` / `cleaned_text`，**无需 schema 迁移**。
* `read_channel_feed` 目前返回 `list[int]`，丢弃了 feed 中已有的 title/author/
  published_at/正文 —— 这是本次改造的核心信息丢失点。
* claim/lease 状态机保护的对象从"抓取子请求"变为"LLM 调用"，**仍需保留**。
* 本机对 `linux.do` 任何路径均 403（挑战页），改造后本地 cron 第一步即失败。

## Assumptions (temporary)

* linux.do 未修改 Discourse 默认 RSS 模板，description 行为与 meta 站一致。[UNKNOWN]
* 该 tag 日增量 << 30 条，5 分钟 cron 配 30 条窗口不会漏。[UNKNOWN]

## Decisions (confirmed)

* **D1 只认首帖。** 回复层内容丢弃，`feeds/topic.py` 整个模块作废，不保留任何回补路径。
* **D2 接受漏帖风险，cron 保持 5 分钟。** 不加窗口穿透检测、不缩短间隔。
  触发条件（5 分钟内 ≥30 个不同 topic 活动）在垂直资源 tag 上视为不会发生。
* **D3 配置项 `CHANNEL_FEED_URL` → `TAG_FEED_URL`**，Glossary 术语
  `Channel Feed` → `Tag Feed`。项目未部署，无向后兼容负担。

* **D4 期望 host 从 `TAG_FEED_URL` 推导**，item 的 `guid`/`link` 必须匹配该 host，
  不符则跳过该 item；`canonical_url` 也用该 host 拼。信任锚定在配置上而非硬编码常量。
  副作用（净收益）：本地把 `TAG_FEED_URL` 指向 `meta.discourse.org` 即可端到端
  验证解析 → 入库 → LLM，无需任何 mock 开关。

* **D5 状态机从 6 态收敛到 4 态。** 这是 D1 的强制后果，不是可选项 ——
  `DUE_TOPICS_SQL` 只认 `'discovered'`/`'failed'`，而 `save_topic_posts` 会把行推到
  `'ready'`；正文改在发现阶段落库后，行一出生就是 `'ready'`，due 查询将永远选不中它，
  **一条都不会被分类**。因此：
  * `discovered` / `fetching` 两态删除（发现即持有正文，无抓取动作可言）
  * 新链路：`ready` → `classifying` → `published` / `not_relevant`，外加 `failed`
  * `DUE_TOPICS_SQL` 改看 `('ready', 'failed')`
  * `RECLAIM_EXPIRED_LEASES_SQL` 回收目标从 `('fetching','ready','classifying')`
    收窄为 `('classifying',)`，回退目标 `discovered` → `ready`
  * `_TOPIC_ROW` 字面量 `'discovered'` → `'ready'`

## Technical Approach

**本质：不是"删一层"，是换掉 `TopicFeed` 的生产者。**

现有 `feeds/topic.py::TopicFeed` / `RetainedPost` 原样保留为数据结构，下游
`save_topic_posts` → `_candidates_of` → `classifier.classify` 一行不改。
被删掉的只是 `TopicFeed` 原来的来源（一次 `/t/topic/<id>.rss` 子请求）。

**正文必须在发现阶段持久化，不能只在内存里传递。** 因为 `failed` 行会在后续轮次
重试，而那时它可能已滑出 30 条窗口 —— 内存里没有正文，就只能回退去抓取，
等于把刚删掉的第二层又请回来。落库是唯一自洽的选择。

**幂等性靠 `ON CONFLICT (topic_id) DO NOTHING` 保持。** 老帖被顶而重现时整行跳过，
不重写正文、不重置状态、不重复分类。

### 受影响文件

| 文件 | 改动 |
|---|---|
| `feeds/channel.py` (414 行) | 重写为 tag feed 解析器，改名 `feeds/tag_feed.py`；URL 正则校验整块删除（guid 取 id 替代） |
| `feeds/topic.py` (409 行) | 仅保留 `TopicFeed`/`RetainedPost` 数据结构并入新模块；fetch、post_number 推导、回复保留规则删除 |
| `sync.py` | `_process_topic` 去掉 fetch；发现阶段改为写正文 |
| `persistence/write_repository.py` | 状态字面量、`DUE_TOPICS_SQL`、`RECLAIM_EXPIRED_LEASES_SQL`、`TOPIC_URL_TEMPLATE` 参数化 |
| `config.py` | `channel_feed_url` → `tag_feed_url` |
| `wrangler.jsonc` / `.env` / `.env.example` | `CHANNEL_FEED_URL` → `TAG_FEED_URL` |
| `CONTEXT.md` | `Channel Feed` → `Tag Feed`；`Topic Post` 重定义；`Claim Lease` 中的 `discovered` 表述 |
| `CLAUDE.md` | 采集层描述、子请求预算数字 |
| `docs/adr/0007-*.md` | 新增，记录推翻两层采集 |

### 子请求预算

`1 + 20×(1 RSS + 1 LLM) = 41` → `1 + N×(1 LLM)`，N ≤ `SYNC_BATCH_SIZE`。

## Decision (ADR-lite)

**Context**：第一层依赖第三方 RSSHub Telegram 镜像（单点、不可信、只给 topic id），
第二层为拿正文对每个 topic 各发一次 RSS 子请求。而官方 tag RSS 的 `description`
已含首帖完整正文（实测 max 11,765 字节无截断）。

**Decision**：改用官方 tag RSS 作为唯一数据源，删除第二层抓取，状态机收敛到 4 态。

**Consequences**：
* 得：去掉第三方依赖；子请求预算大幅下降；删掉约 130 行 URL 防御性校验；
  状态机简化；本地经 `meta.discourse.org` 首次可端到端验证。
* 失：回复层内容永久不可见；历史只剩最近 30 条；tag 极端活跃时可能漏帖且不可回补。
* 不变：D1 schema、API 契约、前端。

## Requirements (evolving)

* 第一层改为读取单个 linux.do tag RSS，从 `<guid>`（`{host}-topic-{id}`）取 topic id，
  不再正则扫正文捞 URL。
* 解析 `<description>`，剥离固定尾巴（`<p><small>N posts - M participants</small></p>`
  与 `<p><a>Read full topic</a></p>`），产出与现有 `TopicFeed` 同形的结构，
  `posts` 恒为单元素且 `is_first_post=True`。
* `topics.published_at` 存 `<pubDate>`（= topic 创建时间），前端排序语义不变。
* 删除第二层 topic 抓取路径：`feeds/topic.py` 的 fetch、post_number 推导、
  回复保留规则全部移除；`sync.py::_process_topic` 不再发起 RSS 子请求。
* 保留 claim/lease 状态机 —— 它保护的对象从"抓取子请求"变为"LLM 调用"。
* 配置项更名 `CHANNEL_FEED_URL` → `TAG_FEED_URL`（`config.py`、`wrangler.jsonc`、
  `.env.example`、`.env`）。

## Acceptance Criteria (evolving)

* [x] 一轮 cron 内，发现的 topic 直接带正文落入 `topic_posts`，不再产生
      `/t/topic/<id>.rss` 子请求。
* [x] `cleaned_text` 不含 `Read full topic` / `N posts - M participants` 尾巴。
* [x] 每个 topic 在 `topic_posts` 中恰有一行，`is_first_post = 1`。
* [x] 同一 topic 在后续轮次重复出现（被顶）时不重复分类，不产生重复
      `project_mentions`。
* [x] 畸形 item（缺 guid / guid host 不符 / description 为空）被跳过且不使整轮失败。
* [x] 代码中不再存在指向 `rsshub` 或 Telegram 的引用。

## Verification (2026-09-08)

* 后端纯层 **722 passed, 1 skipped**，ruff check / format 全绿；前端 `pnpm check` 全绿。
* **真实数据端到端验证**（本次改动带来的新能力）：把 `meta.discourse.org/tag/rss.rss`
  的真实 81,541 字节响应喂进 `parse_tag_feed` —— 30 个 topic 全部解析成功，
  canonical_url 全部属于配置 host，每 topic 恰好一个首帖，尾巴零残留，
  并从真实正文提取到 4 个真实仓库（`discourse/discourse`、`miniflux/v2`、
  `discourse/discourse-rss-polling`、`xrav3nz/discourse-wellfed`）。
* 顺带修复 backend/CLAUDE.md 记录的 4 个长期已知失败（fixture 侧三个缺陷），
  并删除 `rss.py::item_text_fields` 死代码（其唯一调用者是已删除的 channel reader）。
* `test_schema.py` 原本自带一份 CLAIM SQL 副本，在状态机改名后仍自洽通过 ——
  已改为 import 生产常量。

## Review findings (trellis-check, 2026-09-08)

独立复查发现 **1 个真实数据丢失缺陷**，已修 + 回归测试：

* **`tag_feed.py::_link_trailer_re` 会吞掉整篇正文。** 标签用 `.*?` 且开启
  `re.DOTALL`，而 `\Z` 要求匹配落在字符串末尾 —— 于是正文里任何**以自身 topic
  链接开头的 `<p>`**（"见我原帖"这类段落）会成为匹配起点，`.*?` 为够到末尾的真尾巴
  把中间所有段落一并吞掉。实测输入四段、输出空字符串，GitHub 链接随之消失，
  topic 再被当作"空 description"跳过，只留一行 WARNING。
  修复：标签收紧为 `(?:(?!</p>).)*?`，禁止跨段落边界。
  原有测试没覆盖是因为它的自链接前有文字（`<p>See <a...`），锚点不在 `<p>` 首位。

其余为文档与死代码，一并处理：

* `write_repository.reclaim_expired_leases` 的 docstring 说退回 `discovered`，
  而 SQL 写的是 `ready` —— 方向相反，照 docstring 改代码会让每行永久停摆。已修。
* `html_text.py` 模块 docstring 仍在陈述已删除的回复保留规则。已重写。
* **`html_text.py::extract_links` 删除**（连同 `_record_link` / `_links` / `links()`）。
  零生产调用者，其唯一消费者是被删的回复保留谓词。子代理建议保留，理由是"它解决的
  转义 href 问题是 Discourse 标记的性质"——**该理由已不成立**：实测
  `html_to_text('<a href="https://github.com&#x2F;owner&#x2F;repo">')` 输出
  `https://github.com/owner/repo` 并被 `extract_repository_candidates` 成功提取，
  该能力由 `_close_anchor` 提供，与 `extract_links` 无关。
* `CLAUDE.md` 子请求预算原写"约 60 次"，实际 D1 往返才是大头。已改为公式 + 区间。

**对复查一处判断的修正**：`migrations/0001` 的 `status ... DEFAULT 'discovered'`
是个陷阱（省略 status 的 INSERT 落进 `DUE_TOPICS_SQL` 永远选不中的状态，不报错）。
复查以"迁移前向、写过就不改"为由不动它。该规范保护的是**已在生产库跑过**的迁移，
而本项目 `database_id` 仍是 `REPLACE_WITH_D1_DATABASE_ID`，0001 从未 apply 到任何
远程环境，不存在需要保护的 schema 漂移。已直接改为 `DEFAULT 'ready'` 并把注释
收成四态 —— 留陷阱等未来踩，代价远大于现在改一行。所有现存 INSERT 均显式给
status，改动不影响任何调用点。

## Definition of Done

* 单测覆盖新解析器（含固定尾巴剥离、guid 取 id、畸形 item）
* `pnpm check` / 后端 lint+type+test 全绿
* CLAUDE.md / CONTEXT.md 同步术语与架构描述
* 新增 ADR 记录"两层采集 → 单层"的推翻理由

## Out of Scope (explicit)

* 历史数据回填（feed 无分页，官方未支持 RFC 5005）
* D1 schema 迁移（`topic_posts` 已有 `is_first_post` / `cleaned_text`）
* 首帖被编辑后的重新分类（`upsert_discovered_topics` 按 topic_id 去重，
  老帖被顶只在首次出现时分类）
* 窗口穿透检测 / 缩短 cron 间隔（见 D2）

## Technical Notes

* 受影响文件：`feeds/channel.py`(重写)、`feeds/topic.py`(大部分作废)、
  `sync.py::_process_topic`、`config.py`、`wrangler.jsonc`
* 子请求预算：从 `1 + 20×(1 RSS + 1 LLM)` 降为 `1 + N×(1 LLM)`
* 本地不可验证，必须部署后验证 —— 见 CLAUDE.md 未解决发布前置项
