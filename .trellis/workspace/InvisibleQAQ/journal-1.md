# Journal - InvisibleQAQ (Part 1)

> AI development session journal
> Started: 2026-08-31

---



## Session 1: 初始提交：单 Worker 骨架落库

**Date**: 2026-08-31
**Task**: 初始提交：单 Worker 骨架落库
**Branch**: `main`

### Summary

把仓库从零提交历史推到 4 个 scoped commit：仓库配置与 4 条 ADR、Python Worker 采集管线与 D1 持久层、React SPA 与 MUI 主题移植、出网验证 spike。提交前跑了质量门：前端 pnpm check（tsc + eslint + vitest）全绿，后端 ruff check / format 全绿，pytest 589 passed / 4 failed。4 个失败全在 backend/tests/test_write_repository.py 的 fixture 侧，两个根因：(1) seed_topic() 对已结算主题无条件断言 claim_topic 成功，但 upsert_discovered_topics 是 ON CONFLICT DO NOTHING、已 published 的主题不回 discovered，这是 write_repository.py:200 写死的设计且对应 PRD 需求 16；test_publishing_clears_a_previous_failure 则是第二次 seed 传 now=NOW 而 retry_after=LATER，退避未到。(2) make_post() 的 guid 硬编码 guid-{n} 未按 topic 隔离，撞上全表 UNIQUE 的 idx_topic_posts_guid。生产代码正确，用户选择带红提交、留待下轮修 fixture。同时归档 00-bootstrap-guidelines（.trellis/spec/ 已填 17 个真实规范文件）。遗留：根级 pylock.toml 缺失（pywrangler sync 未在根目录跑过），.trellis/ 被全局 gitignore 覆盖导致 Trellis 自动提交空转。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `3305668` | (see git log) |
| `325ac81` | (see git log) |
| `facf6e5` | (see git log) |
| `6d4e80e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: LLM 三协议支持：wire 适配器拆包与显式 schema 降级

**Date**: 2026-09-08
**Task**: LLM 三协议支持：wire 适配器拆包与显式 schema 降级
**Branch**: `main`

### Summary

参考 00_favbase 的 sdkType 分派与 supportsJsonSchema 能力位，把 classifier 从硬编码的 OpenAI Responses 单协议扩成三协议（LLM_PROTOCOL: responses/chat_completions/anthropic），wire 层拆成 backend/src/linuxdo_oss/llm/ 一协议一模块（stdlib-only、惰性分派、不得反向 import classifier），classifier.py 只留分类业务且公开 API 零删除。新增 LLM_SCHEMA_MODE 三档显式降级（strict/json_object/none），把 research note 的禁止回退精确化为禁止静默回退——降级必须写在配置里并每轮 WARNING，绝不运行时换档重试；之所以安全是因为防幻觉靠 _reject_unknown_and_duplicate() 而非 schema，九个协议×档位组合各有断言。config.py 把 base URL 形状错误全部提前到启动时拒绝，401/403/404 从 transport 拆出为 endpoint_config 并带上 status 码。不搬 favbase 的 provider registry（无前端配置面则无消费者），不引任何 SDK（Pyodide 未验证 + 1s 启动快照成本）。决策记录 docs/adr/0005，父 PRD 九处矛盾一并修订。ruff 干净，纯测试 779 passed，新增 179 个。三处自查发现的问题：写测试逼出带 query string 的 base URL 漏洞、自己引入的 _settings_protocol/_settings_schema_mode copy-paste 已合并、classifier 行数预估从 600 修正为实际 845。trellis-check 另修四处：anthropic strict 的 wire 声明不实（Anthropic 的语法约束挂在单独的 strict 工具标志上，本次未发，已如实记录并留 UNKNOWN）、未处理的 model_context_window_exceeded、config.py 既有的 ValueError 逃逸 run_sync、logging-guidelines 未同步。已知遗留：test_write_repository.py 4 个既有失败（fixture 侧，根因见 325ac81）；.trellis/ 被全局 gitignore 屏蔽，规范文件未随代码进版本控制。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `67b4bbd` | (see git log) |
| `c62616d` | (see git log) |
| `23b6b19` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
