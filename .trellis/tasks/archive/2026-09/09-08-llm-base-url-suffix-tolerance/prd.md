# `LLM_BASE_URL` 后缀容忍与内联提示

## Problem Statement

两个独立的缺陷，被同一个配置项串在一起。

**其一，提示存在但放错了地方。** `.env.example` 第 23-30 行已经写清了三协议的
`/v1` 惯例差异（`responses` / `chat_completions` 保留 `/v1`，`anthropic` 不带）。
问题不是缺提示，而是这段说明堆在文件头部的注释块里，离
`LLM_BASE_URL="..."` 赋值行 25 行远。操作员编辑的是赋值行，读到的却不是那段话。

**其二，硬拒绝把两类不同的错误按同一条规则处理。** `config.py:129` 经
`validate_base_url` 拒绝一切以端点路径结尾的 base（ADR 0005 第 6 条）。但：

- `LLM_PROTOCOL=chat_completions` + base 以 `/chat/completions` 结尾 ——
  意图零歧义，拒绝只是要求操作员做一次零信息量的编辑。而从 provider 文档
  复制粘贴完整端点 URL 是最常见的一次配置动作。
- `LLM_PROTOCOL=responses` + base 以 `/chat/completions` 结尾 ——
  真正配错的是**协议**。剥掉后缀会得到一个能通过校验、却指向网关未实现的
  端点的根，把 ADR 0005 第 7 条想消除的那类不可诊断 404 重新造出来。

## Solution

后缀按归属分两类，处理方式相反。判据是「这是当前协议自己的后缀吗」，
不是「这是不是一个端点路径」。

| | 判据 | 处理 |
|---|---|---|
| Class A | 后缀是**当前协议** `ENDPOINT_SUFFIX` 的非空路径前缀（含自身） | 剥掉 + 一条 WARNING |
| Class B | 后缀是**另两个协议**的完整 `ENDPOINT_SUFFIX`，或共享黑名单里的 `/messages` | 硬拒绝，错误信息指出协议归属 |

```
Class A（从 ENDPOINT_SUFFIX 推导，不再手写声明）
  responses         /responses
  chat_completions  /chat/completions   /chat
  anthropic         /v1/messages        /v1
```

副作用是这个判据把 `anthropic` 的 `FORBIDDEN_BASE_SUFFIXES = ("/v1",)` 变成
推导结果的手写副本 —— 三个适配器的 `FORBIDDEN_BASE_SUFFIXES` 全部删除，
`WireAdapter` 协议少一个成员。**净减少一处特殊情况，不是净增加分支。**

决策记录：`docs/adr/0006-llm-base-url-normalization.md`（部分覆盖 ADR 0005 第 6 条）。
术语已同步：`CONTEXT.md` 的 Classifier Endpoint 从 "configured by" 改为 "derived from"。

## Implementation Decisions

### 1. `validate_base_url` → `resolve_base_url(protocol, base_url) -> str`

校验与归一化是同一次后缀遍历的两个结果。拆成两个函数会把「什么算 Class A」
写两遍，而「validate 认为合法的输入 normalize 必须能剥干净」这个不变量
无人守护。合并：合法返回归一化后的根，非法 `raise ValueError`。

同步点（改签名必须全改）：

- `backend/src/linuxdo_oss/llm/protocol.py` — 函数本体 + `__all__`
- `backend/src/linuxdo_oss/llm/__init__.py:55,84` — 两处导出
- `backend/src/linuxdo_oss/config.py:24,129` — import + 调用
- `backend/tests/test_llm_protocol.py:37,172,177-186` — import + 四处断言

`resolve_base_url` 是包内 API，不是部署配置面，改名不构成破坏性变更。

### 2. WARNING 打在 `config.py`，`llm/` 保持零 logging

`llm/` 全包目前不含 logger（只在注释里引用日志规范），这是它能脱离 Worker
在纯 CPython 下单独测的一部分。`resolve_base_url` 只返回根，`config.py` 用
`base_url[len(root):]` 反推被剥的后缀 —— 只剥后缀，返回值必然是入参前缀，
字符串减法精确，不需要 tuple 返回值撑签名。

日志内容：只带后缀字面量与协议名，**绝不带 URL**（后缀来自 `ENDPOINT_SUFFIX`，
是有界固定集合，非操作员输入）。`load_settings` 只有 `sync.py:110` 一个调用点，
所以是每 Sync Run 一次，与 Schema Mode 降级 WARNING 同级，读 API 路由不受影响。

### 3. 剥完必须重新确认结果仍是 `https://<非空 host>`

`"https://v1".endswith("/v1")` 是 `True` —— `anthropic` 协议下会被剥成 `https:/`。
`config.py:104` 的 scheme 检查在剥之前，不够。剥后再确认一次，非法形状按
Class B 硬拒绝。这是「剥」自带的新边界，不是已有校验的重复。

### 4. Class B 错误信息带协议归属

现在的信息是「remove the trailing X」。Class B 下这句话是误导 —— 删掉后缀
只会让它去撞另一个 404。新信息要给两条出路：删掉后缀，或把 `LLM_PROTOCOL`
改成拥有该后缀的那个协议。仍然不回显 URL。

### 5. `?` / `#` 不进入容忍范围

后缀是**路径**，`https://h/v1?k=1` 追加后是请求查询串内部的路径。
本任务只放宽后缀，不放宽 URL 形状。ADR 0005 的这部分论证不变。

### 6. `.env.example` 内联提示

`LLM_BASE_URL=` 赋值行紧邻处加一段简短提示，给出三协议 `/v1` 惯例 +
「带了本协议自己的端点后缀会被剥掉并告警，带别协议的会被拒绝」。
文件头部原有的详细说明保留，不重复正文，只在赋值行旁给可操作的那几行。

## Testing Decisions

- `backend/tests/test_llm_protocol.py`
  - 每协议：base == 根 → 原样返回
  - 每协议：base 带自己完整 `ENDPOINT_SUFFIX` → 剥到根
  - `anthropic`：base 以 `/v1` 结尾 → 剥到根（这是从拒绝翻转为接受的那一格）
  - `chat_completions`：base 以 `/chat` 结尾 → 剥到根
  - 每协议：base 带另两协议的完整后缀 → `ValueError`，信息含目标协议名
  - `responses` / `chat_completions`：base 以 `/v1` 结尾 → **不剥**（`/v1` 是合法根）
  - `https://v1` + `anthropic` → `ValueError`，不返回 `https:/`
  - 尾部斜杠（``、`/`、`///`）不影响以上任何一条
  - 任何错误信息不含入参 URL 的 host
- `backend/tests/test_config.py`
  - Class A 经 `load_settings` 后 `settings.llm_base_url` 是根
  - Class A 打一条 WARNING，只含后缀与协议名，不含 URL、model、key
  - Class B 抛 `RuntimeError`（不是 `ValueError`）
  - `?` / `#` 仍抛 `RuntimeError`，信息不含 key
  - 现有的「`/v1` 根对 OpenAI 系协议合法」不回归
- `backend/tests/test_classifier.py:842` 的 base_url 参数化不受影响（走 settings，已归一化）

## Acceptance Criteria

1. `LLM_PROTOCOL=chat_completions` + `LLM_BASE_URL=https://gw.example.com/v1/chat/completions`
   启动成功，实际请求 `https://gw.example.com/v1/chat/completions`，日志一条 WARNING。
2. `LLM_PROTOCOL=anthropic` + `LLM_BASE_URL=https://api.anthropic.com/v1/messages`
   启动成功，实际请求 `https://api.anthropic.com/v1/messages`。
3. `LLM_PROTOCOL=anthropic` + `LLM_BASE_URL=https://api.anthropic.com/v1`
   启动成功（原为拒绝），实际请求 `https://api.anthropic.com/v1/messages`。
4. `LLM_PROTOCOL=responses` + `LLM_BASE_URL=https://gw.example.com/v1/chat/completions`
   启动失败，信息指出该后缀属于 `chat_completions`，不回显 host。
5. `LLM_PROTOCOL=responses` + `LLM_BASE_URL=https://api.openai.com/v1` 行为不变（不剥、不告警）。
6. 三个适配器的 `FORBIDDEN_BASE_SUFFIXES` 与 `WireAdapter` 里对应的成员声明已删除。
7. 任何错误或日志消息都不含 URL、model 或 key。
8. `.env.example` 赋值行旁有内联提示。
9. `.trellis/spec/backend/environment-configuration.md` 的失败矩阵、Good/Bad Cases、
   Tests Required 三处与新行为一致。
10. `uv run pytest` 全绿。

## Out of Scope

- `?` / `#` 的容忍（决定 5）
- 新增 `LLM_ENDPOINT_PATH` 覆盖项（ADR 0005 已拒绝，无新用例）
- 任何运行时协议探测或失败后换档（ADR 0005 第 4、5 条禁止）
- 前端与 API 契约：零影响

## Further Notes

`[UNKNOWN]` 不清楚是否存在真实网关的 API 根**本身**就以 `/chat` 结尾。
若存在，Class A 会把它剥成上一级，再拼回 `/chat/completions` —— 结果仍然正确，
所以不为此开例外（开例外等于把刚消除的特殊情况请回来）。
