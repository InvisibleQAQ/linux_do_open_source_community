# LLM 多协议支持（配置文件驱动）

## Goal

把分类器的 LLM 出网从"只有 OpenAI Responses API"扩成三种 wire protocol，
全部由部署配置选择，无任何运行时探测与前端配置面。

参考实现：`C:\Users\18368\Desktop\00_myCode\24_cyberSquirrel\00_favbase`
的 `lib/providers.ts` + `lib/ai/index.ts`（`sdkType` 分派 + `supportsJsonSchema`
能力位 + schema 不可用时降级 + 客户端硬校验兜底）。

## What I Already Know

### 现状（已核对代码，非推测）

- `classifier.py:878` `_responses_url()` 硬拼 `{base}/responses`；`build_payload()`
  （`classifier.py:478`）只产出 Responses API 形状：`input[]` + `text.format` +
  `max_output_tokens`；`_extract_output_text()`（`classifier.py:662`）只走
  `output[]` → `type=="message"` → `content[].type=="output_text"`。
- `classifier.py:44` 注释与 `backend/tests/test_classifier.py:236`
  （`test_payload_uses_responses_structured_outputs_not_chat_completions`）
  双重钉死"不用 Chat Completions"。
- `research/llm-protocol.md` 写死 "Do not fall back silently to Chat Completions
  or unstructured JSON"。
- `config.py:52-57` 对 `LLM_BASE_URL` 只做两件事：`rstrip("/")`、要求 `https://`。
  **不校验端点形状**。
- `sync.py:139` `_process_topic` 仍是 `NotImplementedError` ——
  **`classify()` 目前零生产调用点**。改动半径 = `classifier.py` +
  `test_classifier.py` + `config.py` + 配置文档，没有调用链要保护。

### 防幻觉三道防线的真实分工（决定了降级是否安全）

1. prompt 里写规则 —— 软约束。
2. `build_json_schema()`（`classifier.py:407`）把 `canonical_url` 做成候选 URL 的
   `enum`，strict 模式下采样器无法越出 —— 唯一的**结构性**约束。
3. `_reject_unknown_and_duplicate()`（`classifier.py:712`）拿 `allowed_urls` 硬校验
   并抛异常 —— **真正保护数据库的那道**。

结论：第 2 道降级只会让被拒的 decision 变多（浪费 LLM 调用），不会产生脏数据。
所以"允许显式降级"与防幻觉保证不冲突。

### favbase 方案的可搬性（逐块判定）

| favbase 组件 | 判定 | 理由 |
|---|---|---|
| `lib/providers.ts` 9 个 provider registry（预置 `baseUrl`/`defaultModel`/`regUrl`） | **不搬** | registry 的唯一消费者是设置页下拉菜单。本项目无前端配置面，Worker 只有一份部署配置，`LLM_BASE_URL` 本身就是那份配置 |
| `lib/ai/index.ts` 按 `sdkType` 分派到 Vercel AI SDK 构造器 | **只搬形状** | Pyodide 上无 Vercel AI SDK，且不得引 `openai` SDK（`classifier.py:20-24`：Pyodide 未验证 + 模块级导入进内存快照撞 1 s 启动限）。wire shape 手写 |
| `supportsJsonSchema` 能力位 + `supportsSchemaDelivery()` + 降级 + 客户端校验兜底 | **搬核心思想** | 正是本项目 `[UNKNOWN]`（"必须有能力探测，不允许静默回退"）要的东西 |

favbase 对"自定义 base URL"场景恰好只给两种协议（`lib/ai/index.ts:31`
`customProtocol?: 'openai' | 'claude'`，`google` 只存在于预置 provider）。
本项目 100% 是自定义 base URL 场景，故对齐目标是 openai 系 + anthropic 系。
注意 favbase 的 openai 分支走 `.chatModel()` = Chat Completions，
与本项目现有的 Responses 不同。

## Requirements

### R1 三种 wire protocol，由 `LLM_PROTOCOL` 选择

`LLM_PROTOCOL` ∈ `responses` | `chat_completions` | `anthropic`，默认 `responses`
（保住现有行为，Never Break Userspace）。非法值在 `load_settings()` 里直接报错。

七个按协议分叉的轴，一个都不能漏：

| 轴 | `responses` | `chat_completions` | `anthropic` |
|---|---|---|---|
| URL 后缀 | `/responses` | `/chat/completions` | `/v1/messages` |
| 鉴权头 | `Authorization: Bearer` | `Authorization: Bearer` | `x-api-key` + `anthropic-version: 2023-06-01` |
| token 上限字段 | `max_output_tokens` | `max_tokens` | `max_tokens` |
| system 位置 | `input[]` 内 `role=system` | `messages[]` 内 `role=system` | **顶层 `system` 字段** |
| schema 包装（strict 档） | `text.format = {type:json_schema, name, schema, strict:true}` | `response_format = {type:json_schema, json_schema:{name, schema, strict:true}}` | `tools=[{name, input_schema}]` + `tool_choice={type:"tool", name}` |
| 输出提取 | `output[]` → `message` → `content[].output_text` | `choices[0].message.content` | `content[]` → `type=="tool_use"` → `.input`（strict 档）／`type=="text"` → `.text`（降级档） |
| 失败/拒答检测 | `status ∈ {failed, incomplete}` + `content[].type=="refusal"` | `finish_reason ∈ {length, content_filter}` + `message.refusal` | `stop_reason=="max_tokens"` |

### R2 `LLM_SCHEMA_MODE` 三档显式降级

`LLM_SCHEMA_MODE` ∈ `strict` | `json_object` | `none`，默认 `strict`。

- `strict` —— 按 R1 表格下发 JSON Schema，`enum` 硬约束生效。
- `json_object` —— 只发 `response_format={"type":"json_object"}`（anthropic 无此
  概念，退成不带 `tools` 的普通消息），JSON Schema 改为进 prompt。
- `none` —— 完全不发 `response_format`／`tools`，schema 只进 prompt。
  应对见到 `response_format` 就 400 的端点。

三档共用同一条解析尾链：`json.loads` → `_ClassificationResult.model_validate`
→ `_reject_unknown_and_duplicate()`。降级不动这条链。

硬规矩：**禁止运行时自动回退**。请求失败就是失败，不得改档重试。
`research/llm-protocol.md` 那条"禁止降级"精确化为"禁止**静默**降级"——
降级必须由 `LLM_SCHEMA_MODE` 显式声明，且启动时 log 一行 WARNING
（只带档位名，不带任何配置值）。

`json_object` 与 `none` 档下 schema 进 prompt 时，prompt 必须含 "json" 字面词
（OpenAI 规范对 `json_object` 的要求；favbase 的 `lib/tagging/prompt.ts` 同样处理）。

### R3 `LLM_BASE_URL` 语义 = API 根，后缀由协议写死

`config.py` 在 `load_settings()` 里把能拓到的错全拓出来：

- 保留：`https://` 开头、`rstrip("/")`。
- 新增：不得以 `/responses`、`/chat/completions`、`/messages` 结尾
  （用户把全路径填进来的典型错误）。
- 新增：`LLM_PROTOCOL == anthropic` 且 base 以 `/v1` 结尾 → 报错，
  提示 anthropic 的 base 不带 `/v1`（否则拼成 `/v1/v1/messages`）。

拼接：`responses → {base}/responses`、`chat_completions → {base}/chat/completions`、
`anthropic → {base}/v1/messages`。

不引入 `LLM_ENDPOINT_PATH` 覆盖项 —— 目前没有已知用例，属于为臆想的问题提前付费。

### R4 404/401 不再伪装成传输错误

`classifier.py:846-856` 现在只把异常**类型名**放进消息，`FetchError.status`
被丢掉。结果："端点不支持该协议(404)"和"网络超时"在日志里长得一样。

改为：读 `getattr(error, "status", 0)`，`401/403/404` 归入新的
`CATEGORY_ENDPOINT_CONFIG`（不可重试），消息带上 status 码。
仍然不得带 URL、provider body、任何 key。

（`adapters/http.py:92` 已经把 4xx 判为 `retryable=False`，所以不是重试风暴，
纯粹是可诊断性问题。）

### R5 代码结构：新建 `llm/` 包

```
backend/src/linuxdo_oss/llm/
  __init__.py           导出 Protocol / SchemaMode / get_adapter
  protocol.py           两个枚举 + get_adapter() 分派（惰性 import 选中的那一份）
  responses.py          现有逻辑原样搬迁，行为不变
  chat_completions.py
  anthropic.py
```

每个适配器模块导出统一接口，调用方零分支：

- `endpoint_url(base_url) -> str`
- `auth_headers(api_key) -> dict[str, str]`
- `build_payload(*, model, system_prompt, user_content, json_schema, schema_mode, max_output_tokens) -> dict`
- `extract_output_text(raw, schema_mode) -> str`（含该协议的失败/拒答检测，
  抛 `ClassifierError`）

`classifier.py` 保留：`SYSTEM_PROMPT`、`build_json_schema()`、
`STRICT_MODE_UNSUPPORTED_KEYWORDS`、`Decision` / `CandidateRepository` /
`_ClassificationResult`、`_reject_unknown_and_duplicate()`、`parse_response()`
的共用尾链、`classify()` 编排。即"分类业务"，不再含"wire 格式"。

约束继承：`llm/` 各模块只准 import stdlib（`json`、`typing`），
**不得 import `workers`、不得 import `pydantic`**，保证纯 CPython 下可测。
`ClassifierError` 要么留在 `classifier.py` 由适配器接收注入，要么下沉到
`llm/errors.py` —— 实现时二选一，不得让 `llm/` 反向 import `classifier`。

`STRICT_MODE_UNSUPPORTED_KEYWORDS` 是 OpenAI strict 模式的限制，
anthropic 的 `input_schema` 是标准 JSON Schema、限制不同。若两者要分别表达，
放各自适配器模块；`build_json_schema()` 产出的 schema 本体保持协议无关。

### R6 配置面同步

- `wrangler.jsonc` `vars` 增 `LLM_PROTOCOL`、`LLM_SCHEMA_MODE`。
- `.env.example` 增两项并写清契约（含 anthropic 的 base 不带 `/v1`）。
- 根 `CLAUDE.md`、`backend/CLAUDE.md`、`.trellis/spec/backend/environment-configuration.md`
  同步。
- `sync.py:132` 那行 `"linuxdo_oss.classifier — Responses-API adapter"` 要改，
  它现在会说谎。

### R7 术语与决策记录

- `CONTEXT.md` 的「Responses-Compatible Endpoint」已被本任务作废，替换为
  「Classifier Endpoint」+「LLM Protocol」+「Schema Mode」三条。
- 新增 `docs/adr/0005-llm-multi-protocol.md`，记录：为何手写三份 wire shape 而非用
  SDK、为何选显式配置而非运行时探测、以及如何精确化 `research/llm-protocol.md`
  的禁止降级条款。
- `research/llm-protocol.md` 顶部加一行指向 ADR 0005，不改旧结论文本。（已完成）
- **父 PRD 冲突已修订**（2026-09-08，项目所有者确认后执行）。
  `.trellis/tasks/08-31-cloudflare-stack-prd/prd.md` 实际有 **9 处**与 ADR 0005 矛盾，
  不是最初报告的 3 处。全部改完，`docs/adr/0005-llm-multi-protocol.md` 在其中被引用 2 次：
  - Technology stack 的 LLM 一行 —— 改为三协议 + `LLM_PROTOCOL`。
  - "LLM classification contract" 段首加修订横幅，说明分类层契约未变、只有 wire 层变了。
  - 该段新增 `LLM_PROTOCOL` / `LLM_SCHEMA_MODE` 两条，并说明 base URL 是根、
    `/v1` 惯例按协议不同、鉴权头按协议不同。
  - `text.format` 那条 —— 改为三协议各自的 schema 包装写法，并明确"不得把一个协议的
    写法发给另一个协议（会被接受然后忽略）"。
  - "parses typed Responses API output items" —— 改为各适配器解析自己协议的输出项与
    拒答/截断/失败拼写。
  - 兼容性那条 —— 保留"绝不静默回退"，并明确失败不换协议、不换档重试。
  - **`:169` 验收项** —— 改为"部署前人工验证真实端点，结果记入
    `LLM_PROTOCOL` / `LLM_SCHEMA_MODE`"，并写明为何不做自动探测。
  - Further Notes 的"contract is fixed to Responses" —— 改为"分类层固定、wire 层由配置选"。
  - Further Notes 新增一条：防幻觉保证靠 `_reject_unknown_and_duplicate()` 而非 schema。

## Non-Goals

- 不做前端／任何 UI 配置面。
- 不做 provider registry（预置 base URL / 默认模型 / 注册页链接）。
- 不做 `google` / Gemini `generateContent` 原生协议 —— Gemini 有 OpenAI 兼容层
  （`/v1beta/openai/`），走 `chat_completions` 即可。
- 不做运行时能力探测、不做自动降级、不做失败后换档重试。
- 不做 streaming。
- 不实现 `sync.py` 的编排（那是 `08-31-cloudflare-stack-prd` 的范围）。
- 不引入 `openai` / `anthropic` / `httpx` 任何 SDK。

## Acceptance Criteria

1. `LLM_PROTOCOL` 不设或设为 `responses`、`LLM_SCHEMA_MODE` 不设时，
   `build_payload()` 产出的请求体与改动前**逐字节相同**；
   `test_payload_uses_responses_structured_outputs_not_chat_completions` 不改也通过。
2. 三协议 × 三 schema 档 = 9 组组合各有单测断言请求体形状与解析路径。
3. `LLM_PROTOCOL=anthropic` + base 以 `/v1` 结尾 → `load_settings()` 抛
   `RuntimeError`，消息说明原因且不含任何配置值。
4. base 以 `/responses`、`/chat/completions`、`/messages` 结尾 → 同上。
5. `LLM_PROTOCOL` / `LLM_SCHEMA_MODE` 为非法值 → `load_settings()` 抛错，
   不静默取默认。
6. 端点返回 404/401 → `ClassifierError` 的 category 为 `CATEGORY_ENDPOINT_CONFIG`、
   `retryable=False`、消息含 status 码、不含 URL/body/key。
7. 任意 schema 档下，LLM 返回 `allowed_urls` 之外的 URL 一律被
   `_reject_unknown_and_duplicate()` 拒绝 —— 有专门的降级档单测证明这点。
8. `llm/` 包内任何模块在纯 CPython 下 `import` 成功（不碰 `workers`、不碰 `pydantic`）。
9. `LLM_SCHEMA_MODE != strict` 时启动 log 一行 WARNING，只含档位名。
10. `CONTEXT.md`、ADR 0005、`.env.example`、`wrangler.jsonc`、两个 `CLAUDE.md`、
    `environment-configuration.md` 全部同步；`sync.py:132` 的描述已改。

## 实现记录（2026-09-08）

全部 R1–R7 已落地，10 条验收标准逐条验证通过。`uv run ruff check` / `format --check`
干净，纯测试 771 passed / 1 skipped。

与本 PRD 的两处偏差，都是实现时发现更好的做法：

1. **测试文件划分**：R5 的草图列了 `test_llm_responses.py`。没有建它——`responses` 协议
   是默认路径，`test_classifier.py`（82 个测试）已经完整覆盖它，再写一份就是重复。
   实际新增：`test_config.py`(39)、`test_llm_protocol.py`(90，含 3×3 矩阵)、
   `test_llm_chat_completions.py`(21)、`test_llm_anthropic.py`(21)、
   `test_llm_purity.py`(8，子进程守 stdlib-only 约束)。

2. **多出一条 base URL 规则**：写测试时发现 `endswith` 挡不住带 query string 的 base URL，
   而且更糟——往 `?k=1` 后面拼路径后缀会产出 `https://h/v1?k=1/responses` 这种请求。
   现在 `validate_base_url` 直接拒绝含 `?` 或 `#` 的 base URL。R3 没写这条，是漏了。

R5 里"降到约 600 行"这个预估是错的：`classifier.py` 实际 878 → **845 行**，基本持平。
搬走约 150 行 wire 代码，同时新增约 120 行新行为（`endpoint_config` 分类、
从 Settings 读两个枚举、降级档的 prompt schema 注入）。**关注点分离的目标达成了
——文件里已无 wire 格式——但行数预测没达成。** 已把预估从 R5 里删掉，避免文档说谎。

同一轮自查还发现并修掉一处自己引入的 copy-paste：`_settings_protocol` 与
`_settings_schema_mode` 是两份几乎相同的逻辑，已合并为 `_settings_enum()`。

三处顺带修掉的既有文档/实现不一致：`backend/CLAUDE.md` 的测试数（177 → 771，早已过期）、
`sync.py:132` 对 classifier 的描述、`09-01-local-llm-env-example/prd.md` 里指向已删函数的段落。

`llm/` 未建独立 `CLAUDE.md`：本项目的目录文档粒度是顶层（`backend/` / `frontend/`），
包级说明写在 `llm/__init__.py` 的 docstring 与 `backend/CLAUDE.md` 的分层段里。

## Open Questions

设计分支：无。四个（协议集合 / 降级政策 / base URL 语义 / 代码结构）已在 2026-09-08
的 grill 中全部关闭。

父 PRD 的矛盾已由项目所有者确认后修订完毕，共 9 处，见 R7。无遗留。

**与本任务无关的既有失败**：`backend/tests/test_write_repository.py` 有 4 个测试失败
（`claim_topic` 返回 False）。已用 `git stash` 在未改动的工作树上复现，确认是本任务之前
就存在的问题；该文件只 import `persistence/`，不碰本任务改的任何模块。

根因已在 commit `325ac81` 的 message 里记录过，**在测试 fixture 侧，不是生产代码**：
`seed_topic` 对已结算的主题无条件断言抢占成功；`make_post` 的 guid 未按 topic 隔离，
撞上全表 UNIQUE 的 `idx_topic_posts_guid`。修它要改 fixture，属于另一个任务的范围。

本任务不涉及、也不解决根 `CLAUDE.md` 里那两条既有 `[UNKNOWN]`
（linux.do 部署后 Worker 出网、自定义端点是否真支持 strict `text.format`）。
第二条会被 R2 的显式降级**绕开**而非关闭 —— 用户可以配 `json_object` 档避开它。
