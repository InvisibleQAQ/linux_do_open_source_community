# 后端

Cloudflare Python Worker：FastAPI 读 API + 每 5 分钟的 cron 同步编排。配置在**仓库根目录**的 `wrangler.jsonc` 和 `pyproject.toml`（`pywrangler` 会从 cwd 向上找 pyproject，两者必须同级）。

拓扑理由见 `docs/adr/0002-single-worker-topology.md`。平台硬限制见根目录 `CLAUDE.md`。

---

## 运行时是踩雷区，先读这一节

这些不是偏好，是核实过的运行时行为。违反其中任何一条都会在部署后才炸。

**1. 入口类必须叫 `Default` 并继承 `WorkerEntrypoint`。** 模块级 handler（`on_fetch` / `on_scheduled`）自 2025-08-14 起默认禁用。

**2. `asgi.entrypoint(app)` 不够用。** 它返回的类只有 `fetch`，没有 `scheduled`。所以 `backend/src/main.py` 自己写了两个 handler。两者签名不同且都是强制的：`fetch(self, request)` 只收 request，`scheduled(self, controller, env, ctx)` 四个形参必须全部声明，否则调用时 TypeError。

**3. `env` 只能属性访问。** `env.LLM_API_KEY` 可以，`env["LLM_API_KEY"]` 会抛异常——包装器只实现了 `__getattr__`。

**4. FastAPI 路由里没有 `self.env`。** 从 ASGI scope 取：`request.scope["env"]`。已封装在 `api/deps.py`，路由不要直接碰 scope。

**4b. 反过来，`scheduled` 里只有 `self.env`——它的 `env` 形参实测是 `None`。** 声明了不等于收到了。wrangler 4.129.1 下绑定表已正确列出 `env.LLM_BASE_URL` 等全部变量，但 `getattr(env, "LLM_BASE_URL", None)` 返回 `None`，`type(env).__name__ == "NoneType"`；同一次调用里 `self.env` 拿到的是真值。所以 cron 路径写 `run_sync(self.env)`。曾经写成 `run_sync(env)`，后果是每轮 cron 都以 `missing required configuration: LLM_BASE_URL` 中止，而且被 `run_sync` 的 `except RuntimeError` 吞掉——HTTP 仍返回 200 `ok`，只有日志里能看见。`run_sync` 显式收 env 而不是读全局，是因为 cron 没有 request、也就没有 ASGI scope 可读。

**5. `src/` 不在 import path 上，但 `main` 所在目录是。** 所以是 `from linuxdo_oss.api.app import app`，绝不是 `from src.linuxdo_oss...`。

**6. `compatibility_date` 是冻结的。** Python 小版本由日期 + flag 共同决定：`python_workers` 单独 = 3.12；日期 ≥ 2025-09-29 = **3.13（当前）**；日期 ≥ 2026-09-08 = 3.14 且 wasm wheel ABI 全变。改这个日期是一次迁移任务，要重跑 `pywrangler sync` 和全量测试并重新锁 pydantic，不是例行维护。

**7. 禁止创建 `requirements.txt`。** `pywrangler` 的 `check_requirements_txt()` 一旦发现该文件就以 exit 1 中止，在任何同步工作之前。

**8. `triggers.crons` 在部署时整体替换现有触发器。** 注释掉 `crons` **不会**禁用它——要禁用得写 `"crons": []`。配置变更传播最多 15 分钟。

**9. 不要用 `threading` / `multiprocessing`。** 它们能 import 但在 wasm VM 里不工作。并发用 `asyncio.gather` + `asyncio.Semaphore`，上限 6（同时等待响应头的连接数硬上限）。

**10. 全局作用域要轻。** 模块级 import 在部署时执行并被烘进内存快照，而 Worker 启动上限是 1 秒，fastapi + pydantic 的初始化在有快照的情况下已约 1 秒。用不到的东西不要在模块级导入。

---

## 分层与禁止跨越的边界

```
backend/src/
├── main.py                    # 入口：Default(WorkerEntrypoint)，fetch + scheduled
└── linuxdo_oss/
    ├── config.py              # env -> Settings（frozen dataclass），一次读完往下传
    ├── domain/                # 纯逻辑，零 I/O
    │   ├── github_url.py      # 提取 + 规范化（已完成，115 个测试）
    │   └── timestamps.py      # 唯一的时间戳格式（已完成）
    ├── feeds/                  # RSS/XML 读取层
    │   ├── xml_safe.py        # 字节上限 + DTD/实体拒绝
    │   ├── rss.py             # RSS 2.0 / Atom item 遍历 + RFC 822 日期
    │   ├── html_text.py       # cooked HTML -> 纯文本，保留绝对 href
    │   └── tag_feed.py        # 唯一的读取器：一次请求拿到主题列表 + 首帖全文
    ├── llm/                   # LLM wire 协议：纯逻辑，stdlib only
    │   ├── protocol.py        # LLMProtocol / SchemaMode 枚举 + get_adapter 分派 + base URL 校验
    │   ├── errors.py          # ClassifierError + 全部 CATEGORY_*（被三个适配器共用）
    │   ├── json_text.py       # 文本 -> JSON，唯一的宽容点：仅降级档解 markdown 围栏
    │   ├── responses.py       # OpenAI Responses：text.format
    │   ├── chat_completions.py# OpenAI Chat Completions：response_format.json_schema
    │   └── anthropic.py       # Anthropic Messages：x-api-key + 强制 tool_choice + 工具级 strict
    ├── adapters/              # 运行时适配，可以 import workers / js
    │   └── http.py            # 唯一出网出口：限时 + 限大小
    ├── persistence/           # D1 边界
    │   ├── d1.py              # JsProxy -> dict 转换、参数/字节上限
    │   ├── read_queries.py    # 读 SQL + 游标编解码（纯，可测）
    │   └── read_repository.py # 读仓储，返回 Pydantic 模型
    ├── api/
    │   ├── app.py             # FastAPI 应用与路由
    │   ├── schemas.py         # 响应模型 = 与前端的契约的一半
    │   └── deps.py            # request.scope["env"] 访问器
    └── sync.py                # cron 编排 + 编排层 SQL 常量 + 重试预算
```

**`domain/` 不得 import `adapters/`、`persistence/`、`workers`、`js`、`httpx`。** 这条是可测试性的地基：`domain/` 必须能在普通 CPython 的 pytest 下 import 和运行，而 `workers` / `js` 在 CPython 下不存在。`adapters/http.py` 里 `from js import AbortSignal` 写在函数内而不是模块级，就是这个原因。

**`llm/` 只准 import stdlib，且不得反向 import `classifier`。** 同一条地基，多一条约束：`classifier.py` 分派进 `llm/`，反向 import 就是环；而 `classifier.py` 依赖 pydantic，让 `llm/` 沾上它就多付一份启动快照成本。这也是 `ClassifierError` 和全部 `CATEGORY_*` 下沉到 `llm/errors.py` 的原因——三个适配器都要抛它。`classifier.py` 原样 re-export，所以 `from linuxdo_oss.classifier import ClassifierError` 仍然可用。这条约束由 `backend/tests/test_llm_purity.py` 用子进程守着（同进程里 pydantic 早被别的测试导入了，断言不成立）。

**`llm/` 的分派是惰性的。** `get_adapter()` 在函数内 import 选中的那一个模块。一次部署只用一个协议，而 Worker 启动上限 1 秒，为永远不会跑的两个协议付导入成本没有道理。所以 `llm/__init__.py` **不得**导出三个适配器模块。

**`JsProxy` 不得离开 `persistence/d1.py`。** 所有行经 `.to_py()` 变成 dict，再进 Pydantic 模型。Cloudflare 自己的 `query-d1` 示例把 JsProxy 直接交给序列化器，被标了 `@pytest.mark.xfail(reason="500 error, fixme")`；他们那个转成真 Python dict 的 FastAPI 示例则全套测试通过。

---

## D1 的三条约束

**没有交互式事务。** 语句自动提交。`batch()` 是唯一的回滚边界，所以：单主题的写入是**一次** `batch()` 调用；抢占工作是**一条**条件 UPDATE，靠 `meta.changes` 判断本轮是否抢到（read-then-write 会 race）。

**每条语句最多 100 个绑定参数。** `d1.py` 里的 `MAX_BOUND_PARAMS` 和 `chunk_rows()` 负责这件事，helper 会在超限时立刻抛异常而不是让 D1 在运行时拒绝。

**D1 查询也计入子请求配额。** 一页 20 个主题如果每主题查一次项目，就是 20 次往返；`read_repository._projects_by_topic` 用一条 `IN (...)` 查询解决整页。

时间戳全部是 TEXT，格式固定为 ISO 8601 UTC 毫秒（`2026-08-31T15:18:11.000Z`）。schema 用 `<=` 和 `ORDER BY` 直接比较 TEXT，只有格式定宽、零填充、恒为 UTC 时才正确。所以任何时间戳都必须经 `domain/timestamps.py`，不许手写格式化。

---

## 命令

`pywrangler` 自身只有 `sync` 和 `types` 两个子命令，其余全部代理给 `npx wrangler`。

```bash
uv run pywrangler sync                  # 解析依赖并把 wheel 打进 Worker bundle
uv run pywrangler dev                   # :8787
uv run pywrangler deploy                # 会先自动 sync
uv run pywrangler types                 # 从 wrangler.jsonc 的绑定生成 Env 类型
uv run pywrangler secret put LLM_API_KEY

# 本地触发 cron
curl "http://localhost:8787/cdn-cgi/handler/scheduled"

# D1
npx wrangler d1 create linuxdo-oss                     # 把 id 填进 wrangler.jsonc
uv run pywrangler d1 migrations apply linuxdo-oss --local
uv run pywrangler d1 migrations apply linuxdo-oss --remote

# 测试与规范
uv run pytest backend/tests -m "not worker"   # 纯测试，快
uv run pytest backend/tests                   # 含需要本地 Worker 的
uv run ruff check backend/ && uv run ruff format --check backend/
```

工具最低版本由 pywrangler 强制：**uv ≥ 0.12.3**、wrangler ≥ 4.127.1。

`pylock.toml` 要**提交**——它是解析出的锁文件，是唯一能跨机器复现 wasm wheel 集合（含 pydantic-core）的东西。`python_modules/` 和 `.venv-workers/` 不提交。

---

## 测试策略

Cloudflare 没有 Python Workers 的测试框架，`@cloudflare/vitest-pool-workers` 是 JS/TS 专用。所以分两层：

**纯层（默认，706 passed / 1 skipped，约 1.9 秒，全绿）** —— 普通 CPython pytest。覆盖 `domain/`、`persistence/read_queries.py` 的 SQL 与游标、`sync.py` 的 SQL 常量、`d1.py` 的上限守卫。D1 是 SQLite，所以表结构约束、幂等、抢占租约、键集分页全部用 stdlib `sqlite3` 跑真实迁移来验证——不需要 Worker。

测试直接 import 代码里的 SQL 常量（如 `from linuxdo_oss.sync import CLAIM_TOPIC_SQL`），不抄副本，避免测试与实现漂移。

**那 4 个长期已知失败已修复**（随 ADR 0007 的状态机改动一并处理，因为留着会遮蔽真实回归）。三个 fixture 缺陷：`make_post` 的 guid 未按 topic 隔离，撞上全表 UNIQUE 的 `idx_topic_posts_guid`；`seed_topic` 对已结算主题无条件断言抢占成功（那本就抢不到，`CLAIM_TOPIC_SQL` 不接受任何终态）；失败重试用 `now=NOW` 抢占，而 `retry_after=LATER` 尚未到期。现在纯层全绿。

`test_schema.py` 曾自带一份 CLAIM SQL 副本，因而在状态机改名时自洽地继续通过 —— 已改为 import `CLAIM_TOPIC_SQL` 与 `RECLAIM_EXPIRED_LEASES_SQL`。**不要再抄 SQL 副本进测试。**

**Worker 层（`@pytest.mark.worker`，尚未编写）** —— 起 `pywrangler dev` 子进程，用 `requests` 做黑盒 HTTP 断言。只有 JsProxy 转换、`batch()` 原子性、ASGI + cron 共存这些必须真运行时的东西才放这层。

---

## 未完成的部分

后端的采集链路已经接通：`sync.py` 的 `_run()` / `_process_topic()` 不再抛
`NotImplementedError`。一轮 cron 的形状是

```
open_run
  -> reclaim_expired_leases          先回收，否则死掉的上一轮永久占着行
  -> read_tag_feed                   唯一一次出网（TAG_FEED_URL）
  -> save_discovered_topics          行与首帖正文一起落库，状态直接是 ready
  -> list_due_topics(SYNC_BATCH_SIZE) -> 逐个 claim_topic（条件 UPDATE 定胜负）
  -> _process_claimed_topics          Semaphore(max_concurrency)，每任务独立 try
  -> close_run                        在 finally 里，失败的轮次也要留下记录
```

单主题的形状是 `load_stored_posts（从 D1 读回，**不出网**）-> _candidates_of ->
无候选就 mark_not_relevant（**不调 LLM**）-> classify -> publishable_decisions ->
全非 include 也 mark_not_relevant -> publish_topic_result`。

四件容易改错的事：

- **状态机是 4 态，且写入侧与选择侧必须同时改。** `ready`（出生）-> `classifying`
  （claim）-> `published` / `not_relevant` / `failed`。`UPSERT_DISCOVERED_TOPIC_SQL`
  里的 `'ready'` 字面量和 `DUE_TOPICS_SQL` 的 `status = 'ready'` 是同一个决定写在两
  个文件里：只改一边，每一行都会落进没人选的状态，**不报错、不分类、永远如此**。
  `discovered` / `fetching` 已废弃但仍是合法值，所以删除它们不需要迁移。

- **全非 include 结算成 `not_relevant`，不是 `published`。** `read_queries.py` 只按
  `status = 'published'` 暴露主题，发布一个零 mention 的主题就是在公开列表里放一张空卡片。
- **失败必须落库，而落库本身不许抛。** `_record_topic_failure` 跑在 per-topic 隔离块里，
  它自己 try 住一切并只记日志：如果它抛了，异常会逃出 `process` 把同批其余主题一起带走，
  正好毁掉这个结构存在的意义。主题保留租约，等租约过期被回收。
- **重试预算的数字在 `sync.py`**：`MAX_ATTEMPTS = 5`、`RETRY_BASE_SECONDS = 300`、
  `LEASE_SECONDS = 900`。`attempts` 由 `CLAIM_TOPIC_SQL` 递增，计的是**抢占次数**而非失败次数。
  退避在 Python 侧算，不在 SQL 里——SQLite 的 `datetime()` 不输出毫秒，会破坏全库统一的
  ISO 8601 毫秒 TEXT 比较。见 `.trellis/spec/backend/error-handling.md` 的 "The numbers"。

LLM 边界已完成并已接上调用方：`classifier.py`（分类业务）+ `llm/`（三协议 wire 适配器
`LLM_PROTOCOL`、三档 schema 降级 `LLM_SCHEMA_MODE`、候选仓库白名单、Pydantic 二次校验）。
见 `docs/adr/0005-llm-multi-protocol.md`。

`feeds/`（`tag_feed.py` / `rss.py` / `xml_safe.py` / `html_text.py`）与
`persistence/write_repository.py` 也都已实现并有纯层测试覆盖。

**真正还缺的是 Worker 层测试**（`@pytest.mark.worker`，见上一节）：JsProxy 转换、
`batch()` 原子性、ASGI 与 cron 共存、以及下面三个 `[UNKNOWN]`，都只有在真运行时里才能验证。

## 三个 `[UNKNOWN]`，都要 spike 验证

1. **部署后 Worker 出网连通性**。`spikes/egress/` 已备好，先跑它。**必须看部署后的 URL**，`pywrangler dev` 可能从开发机出网从而掩盖封锁。本机对 linux.do 全路径 403，但把 `TAG_FEED_URL` 指向别的 Discourse 实例（如 `meta.discourse.org`）可以在本地端到端验证解析与分类 —— 期望 host 由该 URL 推导，没有硬编码。
2. **`workers.fetch` 的超时机制**。它没有 timeout 选项。`adapters/http.py` 叠了两层：`signal=AbortSignal.timeout(ms)`（kwargs 原样进 JS `RequestInit`，workerd 里有这个 API，但无任何 Python 侧文档或示例）+ `asyncio.wait_for` 外层兜底。第一层是否生效要在部署后的 Worker 上验证。
3. **自定义 LLM 端点是否真支持 strict 结构化输出**。ADR 0005 明确**不做**运行时能力探测（Worker 无状态，探测结果无处缓存；且"探测失败就换档"正是被禁止的静默回退）。端点不支持时把 `LLM_SCHEMA_MODE` 配成 `json_object` 或 `none`——防幻觉保证不变，因为它靠的是 `_reject_unknown_and_duplicate()` 而不是 schema。**这一项现在可绕开，但仍未被验证**：部署前应人工验证真实端点，再据此定档。
