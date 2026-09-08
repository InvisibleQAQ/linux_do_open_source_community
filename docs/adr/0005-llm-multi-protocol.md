# ADR 0005：LLM 三协议手写适配器 + 显式 Schema Mode

- 日期：2026-09-08
- 状态：已接受
- 相关：`.trellis/tasks/09-08-llm/prd.md`，精确化 `.trellis/tasks/08-31-cloudflare-stack-prd/research/llm-protocol.md`
- 修订：2026-09-08 追加第 9 条（`.trellis/tasks/09-08-anthropic-strict/prd.md`），
  关闭原「未被本 ADR 关闭的事」里 anthropic + strict 的 `[UNKNOWN]`
- 参考实现：`00_favbase` 的 `lib/providers.ts` + `lib/ai/index.ts`

## 背景

`research/llm-protocol.md` 定下"只用 OpenAI Responses API"，并写死
"Do not fall back silently to Chat Completions or unstructured JSON"。
落地成 `classifier.py` 里一条硬编码路径：`{base}/responses` + `text.format`
strict JSON Schema。

问题是第三方生态不长这样。`/v1/responses` 在 OpenAI 之外是少数派：
vLLM、Ollama、LM Studio、绝大多数中转站只实现 `/v1/chat/completions`；
DeepSeek 与 ZhiPu 官方只接受 `response_format: {"type":"json_object"}`，
收到 `json_schema` 直接 400；部分本地端点连 `response_format` 都不认。

而 `config.py` 只校验 `https://` 和尾部斜杠，不校验端点形状。配错的后果是
`ClassifierError("LLM request failed: FetchError", CATEGORY_TRANSPORT)` ——
`FetchError.status` 被丢掉，"端点没有这个协议(404)"和"网络超时"在日志里
无法区分。

## 决定

### 1. 三种 wire protocol，由 `LLM_PROTOCOL` 选择，默认 `responses`

`responses` | `chat_completions` | `anthropic`。默认值保住现有行为。

不做 `google` / Gemini `generateContent` 原生协议：Gemini 有 OpenAI 兼容层
（`/v1beta/openai/`），走 `chat_completions` 即可，多一份适配器换不到覆盖面。

### 2. 手写三份 wire shape，不引入任何 SDK

理由是 `classifier.py:20-24` 原本就写下的那条，对三份适配器同样成立：
SDK 在 Pyodide 上未验证，且每个模块级 import 都在部署时执行并烘进内存快照，
撞 Worker 1 s 启动上限。三个协议都是"一次 POST，一个 JSON body"，
`adapters/http.py` 已经把时限与体积都框住了。

对照参考实现：favbase 靠 Vercel AI SDK 的四个 provider 包做分派
（`@ai-sdk/openai` / `-anthropic` / `-google` / `-openai-compatible`）。
那套在浏览器扩展里成立，在 Pyodide Worker 里不成立。**能搬的是
`sdkType` 分派这个形状，不是它的实现。**

### 3. 不搬 provider registry

favbase 的 `lib/providers.ts` 预置 9 个 provider 的 `baseUrl` /
`defaultModel` / `regUrl`，唯一消费者是设置页下拉菜单。本项目无前端配置面，
Worker 只有一份部署配置，`LLM_BASE_URL` 本身就是那份配置。
搬 registry 只会得到一份没有读者的数据表。

### 4. `LLM_SCHEMA_MODE` 三档显式声明，禁止运行时回退

`strict` | `json_object` | `none`，默认 `strict`。

这一条是对 `research/llm-protocol.md` 的**精确化，不是推翻**：
禁止的是"**静默**回退"。降级必须写在部署配置里、启动时 log 一行档位名。
请求失败就是失败，不得改档重试。

之所以允许降级，是因为防幻觉保证并不建立在 schema 上。三道防线的真实分工：

1. prompt 写规则 —— 软约束。
2. `build_json_schema()` 把 `canonical_url` 做成候选 URL 的 `enum`，
   strict 模式下采样器无法越出 —— 唯一的结构性约束。
3. `_reject_unknown_and_duplicate()` 拿 `allowed_urls` 硬校验并抛异常 ——
   **真正保护数据库的那道**。

第 2 道降级只让被拒的 decision 变多（浪费 LLM 调用），不会让编造的仓库落库。
favbase 是同一个判断：`supportsSchemaDelivery()` 为 false 时走
`generateObject({output:'no-schema'})` + 客户端 `tagsSchema.parse` 兜底。

### 5. 不做运行时能力探测

Worker 无状态、isolate 之间不共享内存，探测结果无处缓存，等于每轮 cron
多付一次子请求；而且"探测失败就换档"正是第 4 条禁止的静默回退。
能力由配置声明，配错就在启动时或首次请求时硬失败。

### 6. `LLM_BASE_URL` 恒为 API 根，后缀由协议写死

三协议的 base 惯例天生不一致：OpenAI 系的根含 `/v1`
（`https://api.openai.com/v1` + `/chat/completions`），
Anthropic 的根**不含** `/v1`（`https://api.anthropic.com` + `/v1/messages`）。
favbase 同样躲不开，靠 `resolveModelsEndpoint()` 按 `sdkType` 分别拼。

所以 `config.py` 在启动时把能拓到的错全拓出来：拒绝以
`/responses`、`/chat/completions`、`/messages` 结尾的 base；
`anthropic` 协议下额外拒绝以 `/v1` 结尾（否则拼成 `/v1/v1/messages`）。

不引入 `LLM_ENDPOINT_PATH` 覆盖项 —— 没有已知用例，属于为臆想的问题提前付费。

### 7. 404/401/403 归入 `CATEGORY_ENDPOINT_CONFIG`

`adapters/http.py:92` 已把 4xx 判为 `retryable=False`，所以不是重试风暴，
是可诊断性缺陷。修法是把 `FetchError.status` 透进 `ClassifierError` 的分类与消息。
仍然不得带 URL、provider body、任何 key。

### 8. 新建 `llm/` 包承载 wire 层

七个轴按协议分叉（URL 后缀、鉴权头、token 上限字段名、system 位置、
schema 包装、输出提取、失败/拒答检测），再乘三档 Schema Mode 就是 3×3 矩阵。
摊在一个文件里会让每个函数长出三路分支 —— 那是"增加条件判断"，
不是"消除特殊情况"。一个适配器接口、三份实现、调用方零分支，分派只发生一次。

`llm/` 各模块只准 import stdlib，不得 import `workers` 或 `pydantic`，
不得反向 import `classifier` —— 保证纯 CPython 下可测。
`classifier.py` 因此只留"分类业务"：prompt、schema 本体、Decision 模型、
`allowed_urls` 硬校验、`classify()` 编排。

### 9. `anthropic` 的 STRICT 档发送工具级 `strict: true`

本条关闭原先列在"未被本 ADR 关闭的事"里的那个 `[UNKNOWN]`。

工具定义带上 `strict: true`（与 `name` / `description` / `input_schema` **同级**，
不在 `tool_choice` 里），仅在 `SchemaMode.STRICT` 下发送。依据三条，都已核实：

- 该标志**已 GA，不需要 beta header**。
- 它是工具定义的顶层字段，Anthropic 的 grammar-constrained sampling 挂在它上面。
  不带它的强制工具调用只绑定字段名，对类型与必填项是 best effort。
- 它要求 schema 满足 `additionalProperties: false` + 全部属性进 `required`。
  `build_json_schema()` 为 OpenAI strict 而写，两条**已经满足**（可选字段用
  `{"type": ["string","null"]}` 而不是从 `required` 移除），schema 无需改动。

不发它才是与本 ADR 自相矛盾的选项：第 5 条说"能力由配置声明"，而
`LLM_SCHEMA_MODE=strict` 就是运维在声明该端点支持严格结构化输出；对三协议之一
静默打折，正是第 4 条禁止的静默降级。不认这个字段的端点会返回 4xx，由第 7 条的
`CATEGORY_ENDPOINT_CONFIG` 明确报出——失败可见、可诊断，逃生阀是显式配置
`json_object` 或 `none`，**不是**去掉标志重试。

因此第 4 条把 `enum` 称为"唯一的结构性约束"现在对三个协议一致成立。

## 取舍与代价

- 三份适配器 = 三倍的 wire 层维护面。换来的是"配置文件改一行就能换端点"，
  以及 DeepSeek / ZhiPu / 本地推理这批端点从"不可用"变"可用"。
- `strict` 之外的两档下，`enum` 结构性约束消失，被拒 decision 会变多。
  这是显式选择的代价，不是缺陷。
- `LLM_PROTOCOL` 与 `LLM_SCHEMA_MODE` 成为公开配置面，日后要改语义就是
  破坏性变更。这是接受 ADR 的代价。
- 第 9 条的代价：不认工具级 `strict` 的 anthropic 兼容端点，在 STRICT 档会直接 4xx，
  而不是悄悄拿到一个较弱的保证。这是有意的——显式失败加一次配置改动，换掉一格
  名不副实的 `strict`。

## 未被本 ADR 关闭的事

- `[UNKNOWN]` 自定义端点是否真实现 strict `text.format` —— 本 ADR 让它
  **可绕开**（配 `json_object` 档），不等于验证过。
- `[UNKNOWN]` Anthropic strict 的 schema 关键字限制清单是否与
  `STRICT_MODE_UNSUPPORTED_KEYWORDS`（OpenAI 的那份）相同。本项目的 schema 已避开
  该清单上的全部关键字，所以两边都不受影响；没有为此新增第二份清单。
- `[UNKNOWN]` linux.do 的部署后 Worker 出网 —— 与本 ADR 无关，仍是发布阻塞项。
