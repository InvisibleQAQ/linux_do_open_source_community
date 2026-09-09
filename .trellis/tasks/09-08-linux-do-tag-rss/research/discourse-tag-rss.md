# Discourse tag RSS 实测

目标源 `https://linux.do/tag/2234-tag/2234.rss` 本机 **HTTP 403**（7,100 字节
Cloudflare 挑战页），与已知的 linux.do 挑战一致，无法直接取样。改用同款软件的
公开站点 `https://meta.discourse.org/tag/rss.rss` 取证（Discourse 官方 meta 站，
同一套 `list.rss.erb` 模板）。

## 实测结果（2026-09-08）

`HTTP 200 | application/rss+xml | 81,541 bytes`，30 个 `<item>`。

### item 结构

```xml
<item>
  <title>Add RFC 5005 support to Discourse RSS feeds</title>
  <dc:creator><![CDATA[matthewruzzi]]></dc:creator>
  <category>Feature</category>
  <description><![CDATA[
    <p>Currently <a href="...">Discourse RSS feeds</a> only include ...</p>
    <p><small>1 post - 1 participant</small></p>
    <p><a href="...">Read full topic</a></p>
  ]]></description>
  <link>https://meta.discourse.org/t/add-rfc-5005.../407218</link>
  <pubDate>Thu, 09 Jul 2026 21:00:11 +0000</pubDate>
  <discourse:topicPinned>No</discourse:topicPinned>
  <guid isPermaLink="false">meta.discourse.org-topic-407218</guid>
  <source url="https://meta.discourse.org/t/....rss">...</source>
</item>
```

### 关键事实

1. **`description` 是首帖的完整 cooked HTML，不是 excerpt。**
   长度分布 min=312 / median=1,162 / **max=11,765** 字节；无 `&hellip;`、
   无 `...` 结尾；全 feed 出现 81 处 `<pre>/<code>`，blockquote、mention、
   `discourse-local-date` span 均原样保留。

2. **只含首帖，不含回复。** 一条 26 帖的 topic，description 里仍只有第 1 帖，
   楼层数只以尾注 `<p><small>26 posts - 9 participants</small></p>` 出现。

3. **固定尾巴两段。** 每个 description 末尾恒为
   `<p><small>N post(s) - M participant(s)</small></p>` +
   `<p><a href="{topic_url}">Read full topic</a></p>`，需在清洗阶段剥离，
   否则会进 `cleaned_text` 污染 LLM 输入。

4. **`guid` 直接携带 topic id**，格式 `{host}-topic-{id}`。比现在从正文正则
   捞 `https://linux.do/t/topic/<id>` 可靠得多。

5. **30 条上限，无分页。** feed 内第 3 条 topic 恰是社区在请求给 Discourse RSS
   增加 RFC 5005 分页支持 —— 官方尚未支持。历史内容无法回填。

6. **channel 元数据**：`<title>Discourse Meta - Topics tagged rss</title>`、
   `<link>https://meta.discourse.org/tag/rss/76</link>`、`<lastBuildDate>`。
   规范 URL 形如 `/tag/<slug>/<tag_id>.rss`，与用户给的
   `/tag/2234-tag/2234.rss` 同形。

## 对本项目的映射

- 现有 `feeds/topic.py::TopicFeed` / `RetainedPost` 可直接承载解析结果，
  posts 恒为单元素且 `is_first_post=True`。下游 `save_topic_posts`、
  `_candidates_of`、`classifier.classify` 无需改动。
- `topic_posts` 表结构无需迁移（已有 `is_first_post`、`cleaned_text`）。
- `feeds/channel.py` 中防 `linux.do.evil.example` / `user@linux.do` / `../../`
  的 URL 校验（约 130 行注释+正则）随 guid 取 id 而作废。

## 未决 [UNKNOWN]

- linux.do 该 tag 的 `description` 是否与 meta 站一致（站点可改模板、可关
  RSS 全文）。**只能部署后验证。**
- 该 tag 每日新增量，决定 30 条窗口配 5 分钟 cron 是否足够。

## 追加实测：排序与时间语义（关键）

30 条 item 的 `topic_id` / `pubDate` 前 12 行：

```
topic_id  pubDate                    posts
407218    2026-07-09T21:00:11+00:00  1
407094    2026-07-08T17:58:04+00:00  4
264134    2023-05-07T11:04:55+00:00  26   <-- 老帖排第 3
392890    2026-01-07T15:45:46+00:00  8
380824    2025-08-28T21:57:53+00:00  2
```

* `pubDate` **单调递减 = False**，`topic_id` **单调递减 = False**
* `pubDate` 是 **topic 创建时间**（264134 → 2023-05-07，与 id 吻合）
* 排序是 Discourse 默认的 **`bumped_at`（最后活动时间）降序**
* 30 条时间跨度 **2017-01-12 → 2026-07-09，约九年**

### 排序不可控（已验证）

| 变体 | 结果 |
|---|---|
| `/tag/<slug>/l/latest.rss` | HTTP 404 |
| `/tag/<slug>/l/new.rss` | HTTP 404 |
| `/tag/<slug>.rss?order=created` | HTTP 200，**响应字节数与默认完全一致（81,541B），参数被忽略** |

结论：**只能拿到 bumped_at 降序的最新 30 条，排序无法改成创建时间。**

### 对本项目的两个后果

1. **老帖会重现**：任何一个老 topic 被回复就重新冒头。因为
   `upsert_discovered_topics` 按 `topic_id` 主键去重，它只在**首次**出现时
   被当作新发现并分类 —— 这是净收益（能捡到被顶起来的老资源帖），不是 bug。

2. **漏帖风险**：若该 tag 在一个 cron 周期（5 min）内有 **≥30 个不同 topic**
   发生活动，最新发布的帖会被顶上来的老帖挤出窗口，且**永远无法回补**
   （无分页、无按创建时间排序）。风险与 tag 活跃度直接相关。

3. **`topics.published_at` 语义正确**：存 `pubDate` 即创建时间，前端"最新"
   排序行为不变。
