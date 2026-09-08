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


## Session 3: 让 .trellis 进入版本控制 + anthropic 发送工具级 strict

**Date**: 2026-09-08
**Task**: 让 .trellis 进入版本控制 + anthropic 发送工具级 strict
**Branch**: `main`

### Summary

两件收尾。(1) .trellis 进版本控制：原因是机器级全局 gitignore 带一条笼统的 .trellis/，导致 spec 与它管的代码不在同一份历史里，上一轮 23b6b19 丢了 4 份 spec、一份任务 PRD 和父 PRD 的 9 处修订，archive 与 journal 脚本也因检测到被忽略而跳过 git。用项目级 !.trellis/ 否定规则解决，不改全局配置——仓库内 .gitignore 优先级高于 core.excludesFile，实测有效，且队友克隆后行为不依赖各人机器状态。内层 .trellis/.gitignore 本来就写对了未改动，79 个文件里不含 .developer / .current-task / .runtime / 缓存 / 备份。副作用已记入 CLAUDE.md：脚本从此会自己产生 commit（本轮已观察到生效）。(2) anthropic 适配器发送工具级 strict：LLM_SCHEMA_MODE=strict 对 anthropic 一直名不副实，Anthropic 把语法约束挂在工具定义顶层的 strict 字段上，没有它强制工具调用只绑定字段名、对类型与必填是 best effort，canonical_url 的 enum 不成硬约束。核实依据：strict 是工具定义顶层字段而非 tool_choice 字段（放错会被静默忽略，故测试直接断言 set(tool) 与 strict not in tool_choice）、GA 无需 beta header、前置要求 additionalProperties:false + 全部 required 已由 build_json_schema 满足（本轮机器验证 schema 每层对象都满足）。依据 ADR 0005 第 5 条能力由配置声明：LLM_SCHEMA_MODE=strict 就是运维在声明端点支持严格结构化输出，不发等于对三协议之一静默打折。同时改正上一轮我写错的 8 处表述（原 docstring 断言 Anthropic 没有 strict 标志），每处保留反事实说明以防日后有人把标志当冗余删掉；ADR 把该 UNKNOWN 移出未关闭项。ruff 干净，纯测试 780 passed。遗留：test_write_repository.py 4 个既有失败（fixture 侧，根因见 325ac81）；Anthropic strict 的关键字限制清单是否与 OpenAI 相同仍 UNKNOWN（本项目 schema 已避开清单全部关键字，两边无影响）。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `1d6ae45` | (see git log) |
| `ae66eca` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: LLM_BASE_URL 后缀容忍：按归属剥离或拒绝

**Date**: 2026-09-08
**Task**: LLM_BASE_URL 后缀容忍：按归属剥离或拒绝
**Branch**: `main`

### Summary

LLM_BASE_URL 末尾多写端点路径时按后缀归属分两路：属于当前协议自己 ENDPOINT_SUFFIX 路径前缀的剥回 API 根并每 Sync Run 告警一次，属于别协议的继续启动拒绝并在信息里指出协议归属。理由是后者配错的是 LLM_PROTOCOL 而非 URL，剥掉会把可诊断的启动失败变成 cron 里的 404。validate_base_url 合并为 resolve_base_url（校验与归一化一次遍历），Class A 集合改从 ENDPOINT_SUFFIX 推导，三个适配器的 FORBIDDEN_BASE_SUFFIXES 与 WireAdapter 对应成员全部删除，净减少一处特殊情况。WARNING 打在 config.py 靠字符串减法反推后缀，llm/ 保持零 logging。修掉剥离自带的边界：'https://v1'.endswith('/v1') 为真会剥成 https:/。行为翻转一处：anthropic + 以 /v1 结尾的 base 由拒绝改为接受。新增 ADR 0006 部分覆盖 0005 第 6 条，同步 CONTEXT.md、根 CLAUDE.md、environment-configuration.md、.env.example 内联提示。遗留：test_write_repository.py 4 个失败（claim_topic 返回 False）为既有缺陷，clean tree 上同样失败，未处理。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `eed12a1` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
