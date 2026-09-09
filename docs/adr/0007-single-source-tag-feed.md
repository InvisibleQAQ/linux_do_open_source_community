# ADR 0007：采集收敛为单一 Discourse tag feed，删除逐主题抓取

- 日期：2026-09-08
- 状态：已接受
- 关系：**推翻两层采集设计**。原设计（第三方 RSSHub Telegram 镜像发现 topic id
  → 逐个抓 `/t/topic/<id>.rss` 取正文）不再存在，`feeds/channel.py` 与
  `feeds/topic.py` 一并删除。
- 相关：`.trellis/tasks/09-08-linux-do-tag-rss/prd.md`、
  `.trellis/tasks/09-08-linux-do-tag-rss/research/discourse-tag-rss.md`

## 背景

原设计把「发现哪些帖子」和「取得帖子正文」拆成两次网络请求，因为假定
发现源只能给出链接。这个假定来自发现源的形态 —— 一个 Telegram 频道的
RSSHub 镜像，正文里只有 URL。

两个代价随之而来。一是每轮 cron 的子请求数是 `1 + N×(1 RSS + 1 LLM)`，
20 主题一轮就是 41 次。二是发现源是第三方公共 RSSHub 实例，它一旦限流或
下线，整条管线 `discovered = 0` 且不报错 —— 没有数据和没有新数据长得一样。

而 Discourse 自己的列表 feed 根本不需要第二次请求。

## 实测依据

目标源 `https://linux.do/tag/2234-tag/2234.rss` 从开发机取不到
（HTTP 403，7,100 字节 Cloudflare 挑战页，与 linux.do 其余路径一致）。
改用同一套 `list.rss.erb` 模板的 `https://meta.discourse.org/tag/rss.rss`
取证，`HTTP 200 | application/rss+xml | 81,541 bytes`，30 个 item：

1. **`<description>` 是首帖的完整 cooked HTML，不是摘要。**
   长度 min=312 / median=1,162 / **max=11,765** 字节，无 `&hellip;` 截断标记，
   81 处 `<pre>/<code>`、blockquote、mention 全部原样保留。
2. **`<guid isPermaLink="false">` 形如 `{host}-topic-{id}`**，topic id 直接可读。
3. **只含首帖**。一条 26 帖的 topic，description 里仍只有第 1 帖。
4. **排序是 `bumped_at` 降序且不可改。** `?order=created` 返回 200 但响应字节数
   与默认完全一致（参数被忽略），`/l/latest.rss` 是 404。30 条跨越 2017–2026。
5. **`<pubDate>` 是 topic 创建时间**，不是最后活动时间。
6. **30 条无分页。** feed 内第 3 条 topic 恰是社区在请求 RFC 5005 分页支持。

## 决定

### 1. 单一数据源，单次请求

`TAG_FEED_URL` 是管线唯一输入。一次请求同时得到主题列表与首帖正文，
子请求预算从 `1 + N×(1 RSS + 1 LLM)` 降为 `1 + N×(1 LLM)`。

### 2. 正文在发现阶段落库，不在内存传递

`save_discovered_topics` 一次 `batch()` 写入 `topics` 行与 `topic_posts` 行。

理由是重试路径：`failed` 行会在后续轮次重试，而那时它可能早已滑出 30 条窗口。
内存传递能服务新主题、却会让重试主题永远拿不到正文 —— 两条代码路径，其中
一条静默失效。落库后 `_process_topic` 统一从 D1 读回，一条路径。

### 3. 状态机从 6 态收敛到 4 态

`ready` → `classifying` → `published` | `not_relevant`，外加 `failed`。

`discovered` 和 `fetching` 描述的是「已知主题但尚无正文」的间隙。该间隙不再
存在，两个状态也就没有含义了。二者仍是 schema 里的合法值，只是永不写入，
因此**不需要迁移**。

这一条不是可选项。`DUE_TOPICS_SQL` 原本只选 `discovered`/`failed`，而
`save_topic_posts` 会把行推到 `ready`；若只改写入时机而不改选择条件，每一行
出生即 `ready`，due 查询永远选不中，**一条都不会被分类**，且没有任何错误。

### 4. 期望 host 从 `TAG_FEED_URL` 推导，不硬编码

item 的 `guid` 必须匹配该 host，否则跳过；`canonical_url` 也用该 host 拼。

信任锚定在配置上，强于硬编码常量：feed 无法拓宽自己的权限。原
`channel.py` 中防 `linux.do.evil.example`、防 `user@linux.do`、防 `../../`
的约 130 行 URL 校验随之作废 —— 身份来自 guid，不再从正文里捞 URL。

副作用是净收益：本地把 `TAG_FEED_URL` 指向任一可达的 Discourse 实例
（如 `meta.discourse.org`），解析、入库、分类全链路照常运行，**首次**
使得本地端到端验证成为可能。

### 5. 生成尾巴按结构剥离，不按文案

每个 `<description>` 末尾恒有两段生成内容：`<p><small>N posts - M
participants</small></p>` 与 `<p><a href="{topic_url}">Read full topic</a></p>`。

剥离规则匹配**元素形状**与**锚点 href 是否等于 item 自己的 `<link>`**，
不匹配文字。Discourse 会本地化这两行，而 linux.do 是中文站；写死英文文案
会在 `meta.discourse.org` 上一直通过测试，在生产上静默失效，把样板文字
灌进每一条送给模型的 `cleaned_text`。

剥离失败时保留原文，不抛异常：代价是两行噪音，且那个锚点指向论坛而非
GitHub，不可能制造出假的仓库候选。

### 6. 已知主题重现时整行跳过

feed 按最后活动排序，任何老帖被回复都会重新进入窗口。
`save_discovered_topics` 先查一次已存在的 id，只写新主题。

不仅靠 `ON CONFLICT DO NOTHING`：那只能挡住 `topics` 行，挡不住随行的
`topic_posts` 写入。若放任其 `DO UPDATE`，每次被顶都会重写 `cleaned_text`,
等于在分类器已结算的结果下面不断更换输入。冲突子句因此退居为并发保护 ——
挡住成员查询与 batch 之间另一轮的插入。

## 代价

明确接受，不再讨论：

- **回复层内容永久不可见。** 楼主在 3 楼补充的仓库地址采集不到。
- **历史只剩最近 30 条。** 无分页、无法按创建时间排序，滑出窗口即永久不可达。
- **极端活跃时可能漏帖。** 若单个 tag 在 5 分钟内有 ≥30 个不同 topic 发生活动，
  新帖会被顶上来的老帖挤出窗口且无法回补。不加检测、不缩短 cron 间隔。
- **首帖被编辑后不重新分类。**

## 不变

D1 schema、API 契约、前端。`topic_posts` 本就有 `is_first_post` 与
`cleaned_text`，只是从此每个 topic 恰好一行。
