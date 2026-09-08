# ADR 0006：`LLM_BASE_URL` 只容忍「本协议自己的」端点后缀

- 日期：2026-09-08
- 状态：已接受
- 关系：**部分覆盖 ADR 0005 第 6 条**（那一条决定「拒绝以 `/responses`、
  `/chat/completions`、`/messages` 结尾的 base；`anthropic` 额外拒绝 `/v1`」）。
  0005 不修改，第 6 条自本 ADR 起以这里为准。
- 相关：`.trellis/tasks/09-08-llm-base-url-suffix-tolerance/prd.md`

## 背景

ADR 0005 第 6 条把「base 里带端点路径」一律判为配置错误，在 `load_settings`
启动时硬失败。理由是可诊断性：留着不管会拼成 `/responses/responses`，
五分钟后在 cron 里变成一个读起来像网络故障的 404。

这个理由对，但它把两件不同的事按同一条规则处理了。操作员从 provider 文档
复制粘贴到的**就是完整端点 URL** —— 这是最常见的一次配置动作，不是罕见失误。
而当粘进来的后缀恰好是**当前协议自己的** `ENDPOINT_SUFFIX` 时，
意图没有任何歧义：`LLM_PROTOCOL=chat_completions` 加
`.../v1/chat/completions`，操作员想连的只可能是那一个端点。
让它硬失败，等于要求操作员做一次零信息量的编辑。

反过来，当后缀属于**别的**协议时，病灶根本不在 URL 上。
`LLM_PROTOCOL=responses` 加 `.../v1/chat/completions`，真正配错的是协议。
把这个后缀剥掉会得到一个能通过校验、却指向网关没有实现的端点的根 ——
恰好把 0005 第 7 条想消除的那类「不可诊断的 404」重新造出来。

`.env.example` 的三协议 `/v1` 惯例说明本来就存在，但堆在文件头部的
注释块里，距离 `LLM_BASE_URL=` 赋值行 25 行远。

## 决定

### 1. 后缀分两类，处理方式相反

| | 判据 | 处理 |
|---|---|---|
| Class A | 后缀是**当前协议** `ENDPOINT_SUFFIX` 的一个非空路径前缀（含自身） | 剥掉，打一条 WARNING |
| Class B | 后缀是**另外两个协议**的完整 `ENDPOINT_SUFFIX`（或历史共享黑名单里的 `/messages`） | 硬拒绝 `RuntimeError` |

Class B 的错误信息要指出协议归属，而不是只说「这是完整端点路径」：
「该后缀属于协议 X —— 要么删掉后缀，要么把 `LLM_PROTOCOL` 改成 X」。
这是操作员真正需要知道的那句话。

### 2. Class A 集合从 `ENDPOINT_SUFFIX` 推导，per-adapter 黑名单删除

```
responses         /responses
chat_completions  /chat/completions   /chat
anthropic         /v1/messages        /v1     <- 不再需要单独声明
```

`anthropic` 的 `FORBIDDEN_BASE_SUFFIXES = ("/v1",)` 之所以存在，就是因为
`/v1` 是它 `ENDPOINT_SUFFIX` 的前缀。规则一旦写成「所有非空路径前缀」，
那条声明就是推导结果的手写副本，删掉。三个适配器的
`FORBIDDEN_BASE_SUFFIXES` 全部消失，`WireAdapter` 协议少一个成员。

规则保持统一，不为 `chat_completions` 的 `/chat` 开例外。`/chat` 被剥掉的
后果是 `.../v1/chat` 变 `.../v1`，再拼回 `/chat/completions` —— 结果正确。
为它加一条「除 `/chat` 外」就是把刚消除的特殊情况请回来。

### 3. `validate_base_url` 改为 `resolve_base_url`，返回归一化后的根

校验和归一化是同一次后缀遍历的两个结果。拆成 `validate_*` + `normalize_*`
两个函数意味着「什么算 Class A」被写两遍，而「validate 认为合法的输入
normalize 必须能剥干净」这个不变量没有任何机制守着 —— 两边一漂就是
「拒绝了却没剥」或「剥了却仍报错」。合并成一个函数：合法返回根，非法
`raise ValueError`。

签名与函数名变更，同步 `config.py`、`llm/__init__.py` 的导出、
`protocol.py` 的 `__all__`、`test_llm_protocol.py`。`resolve_base_url`
是包内 API，不是部署配置面，改名不构成破坏性变更。

### 4. WARNING 打在 `config.py`，不打在 `llm/`

`llm/` 全包目前零 logging，只在注释里引用日志规范。这是它能在纯 CPython
下脱离 Worker 单独测的一部分，保持不变。`resolve_base_url` 只返回根，
`config.py` 用 `base_url[len(root):]` 反推被剥的后缀 —— 只剥后缀，返回值
必然是入参的前缀，字符串减法精确，不需要 tuple 返回值撑签名。

日志内容遵守既有规范：只带后缀字面量和协议名，**绝不带 URL**。
后缀来自 `ENDPOINT_SUFFIX`，是有界的固定集合，不是操作员输入。
`load_settings` 只有 `sync.py` 一个调用点，所以这条 WARNING 是每 Sync Run
一次，与 Schema Mode 降级 WARNING 同级，读 API 路由不受影响。

### 5. 剥完必须重新确认结果仍是 `https://<非空 host>`

`"https://v1".endswith("/v1")` 是 `True`。`anthropic` 协议下这个输入会被剥成
`https:/`。scheme 检查在剥之前做过一次不够 —— 剥后必须再确认一次，
剥出非法形状就按 Class B 一样硬拒绝。这是「剥」这个动作自带的新边界，
不是已有校验的重复。

### 6. `?` / `#` 仍然硬拒绝，不进入容忍范围

ADR 0005 的论证不变：后缀是**路径**，`https://h/v1?k=1` 追加后会变成
请求查询串内部的一个路径。要支持就得解析并重组 URL，本项目没有端点需要它。
本 ADR 只放宽后缀，不放宽 URL 形状。

## 取舍与代价

- **容忍一旦上线就难以撤回。** 有人带后缀配置跑在线上之后，收回容忍
  等于让他们的 Worker 启动失败。这是接受本 ADR 的主要代价，也是它值得
  留一份记录的原因。
- **CONTEXT.md 的 Classifier Endpoint 领域语义不变**：它仍然恒为 API 根。
  变的只是「配置值如何推导出那个根」。定义里的 "configured by" 松成
  "derived from"，容忍规则写在实现层，不污染术语表。
- WARNING 只说剥了什么后缀，不说剥的是哪个 URL。诊断信息比理想值少一点，
  但日志规范对配置值是绝对的 —— 网关 base URL 可以在查询串里带 key。
- Class A / Class B 的非对称会让第一次读到的人问「为什么不一律剥」。
  这份 ADR 就是那个答案。

## 未被本 ADR 关闭的事

- `[UNKNOWN]` 自定义端点是否真实现 strict 结构化输出 —— 与本 ADR 无关，
  仍是 ADR 0005 留下的发布前置项。
- `[UNKNOWN]` linux.do 的部署后 Worker 出网 —— 与本 ADR 无关，仍是发布阻塞项。
