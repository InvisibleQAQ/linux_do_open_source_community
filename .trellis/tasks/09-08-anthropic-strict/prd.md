# anthropic 适配器发送 strict 工具标志

## Goal

让 `LLM_PROTOCOL=anthropic` + `LLM_SCHEMA_MODE=strict` 真正兑现 `strict` 这个档位的
承诺：`canonical_url` 的 `enum` 成为采样器越不出去的语法约束，而不是一条强指令。

## What I Already Know

### 现状是名不副实的

`llm/anthropic.py` 在 STRICT 档只发 `tools[].input_schema` + `tool_choice`，不发
`strict`。已核实（Anthropic 官方工具使用文档）：

- `strict: true` 是**工具定义的顶层字段**，与 `name` / `description` / `input_schema`
  同级，**不是** `tool_choice` 上的字段。
- **GA，不需要 beta header。**
- 要求 schema 满足 `additionalProperties: false` + 全部属性进 `required`。
- 语法约束挂在这个标志上。**没有它，强制工具调用只绑定字段名，对类型与必填是
  best effort** —— 也就是 `enum` 不构成硬约束。

`build_json_schema()`（`classifier.py:376`）为 OpenAI strict 模式而写，恰好已满足
两条前置要求：每层对象都有 `additionalProperties: false`，全部属性都在 `required` 里
（可选字段用 `{"type": ["string","null"]}` 而非从 required 移除）。**无需改 schema。**

### 这是我在 09-08-llm 里写错的一处

原 docstring 断言"Anthropic 没有 strict 标志，强制工具调用等同于 OpenAI strict 的
结构性约束"。`trellis-check` 抓到并如实改成了 best effort，同时把
`strict: true` 标记为 `[UNKNOWN]`（担心不认识该字段的中转站会 400）。
本任务关闭那个 `[UNKNOWN]`。

### 为什么该发，不是猜

ADR 0005 第 5 条的原则是**能力由配置声明**。`LLM_SCHEMA_MODE=strict` 本身就是运维在
声明"这个端点支持严格结构化输出"。不发 `strict` 意味着这个声明对三协议之一被单方面
打了折扣，而且是静默打折 —— 这与 ADR 反对静默降级的立场自相矛盾。

不兼容的逃生阀早已存在且是显式的：`LLM_SCHEMA_MODE=json_object` 或 `none`。中转站若
拒绝该字段，会得到一个 4xx，由 `CATEGORY_ENDPOINT_CONFIG` 明确报出"检查
LLM_PROTOCOL / LLM_BASE_URL / LLM_API_KEY"，而不是静默降级。**失败可见、可诊断、
有明确对策**，正是 ADR 想要的形状。

## Requirements

### R1 STRICT 档的工具定义加上 `strict: True`

`llm/anthropic.py::build_payload`，加在工具定义对象里（与 `name` /
`description` / `input_schema` 同级），仅在 `schema_mode is SchemaMode.STRICT` 时。
降级档不发 `tools`，不受影响。

### R2 改正随之失效的表述

`09-08-llm` 那轮为"不发 strict"写的说明现在全部反了，共 8 处要改：

- `llm/anthropic.py` 模块 docstring 第 3 条 + `build_payload` 里的注释
- `llm/protocol.py` `SchemaMode` docstring
- `classifier.py` 模块 docstring 第 2 条
- `CONTEXT.md` 的「Schema Mode」词条
- `CLAUDE.md` 的 Schema 降级表格 `strict` 行
- `wrangler.jsonc` `vars` 段注释
- `.env.example`
- `.trellis/spec/backend/environment-configuration.md` 的 `strict` 强度说明
- `docs/adr/0005-llm-multi-protocol.md`：把它从"未被本 ADR 关闭的事"移走，
  记为已关闭并写明依据（GA、无 beta header、schema 已满足前置要求）

改完后三协议在 STRICT 档下强度一致，`CONTEXT.md` 与 ADR 的原始承诺重新成立。

### R3 测试

`test_llm_anthropic.py`：断言 STRICT 档的工具定义含 `strict: True`；断言降级档
不含 `tools`（既有测试已覆盖，确认不回归）。`test_llm_protocol.py` 的
`test_strict_mode_delivers_the_schema_structurally` 的 anthropic 分支加上该断言。

## Non-Goals

- 不改 `build_json_schema()` —— 它已满足 Anthropic strict 的两条前置要求。
- 不动 `STRICT_MODE_UNSUPPORTED_KEYWORDS`：它是 OpenAI strict 的限制清单，
  Anthropic 的限制清单是否相同 `[UNKNOWN]`；但本项目的 schema 已避开清单上所有关键字，
  两边都无影响，无需为此新增一份清单。
- 不加运行时探测、不加失败后去掉 `strict` 重试 —— 那正是 ADR 0005 禁止的静默回退。
- 不改其他两个协议。

## Acceptance Criteria

1. `LLM_PROTOCOL=anthropic` + `strict` 的请求体里，`tools[0]` 同时含
   `name` / `description` / `input_schema` / `strict: True`，且 `strict` 在
   `tools[0]` 顶层而非 `tool_choice` 里。
2. `json_object` 与 `none` 档的请求体仍不含 `tools`、`tool_choice`、`strict`。
3. `build_json_schema()` 的产出未改动（既有的字节一致性测试仍通过）。
4. 上述 8 处表述全部与代码一致，全仓库搜不到"不发 strict / does not send it"这类残留。
5. `ruff check` / `format --check` 干净；纯测试通过数不低于 779，
   `test_write_repository.py` 那 4 个既有失败数量不变。

## Open Questions

无。是否发送 `strict` 这一分支已由项目所有者在 2026-09-08 决定：发送。
