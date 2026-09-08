# 后端

Cloudflare Python Worker：FastAPI 读 API + 每 5 分钟的 cron 同步编排。配置在**仓库根目录**的 `wrangler.jsonc` 和 `pyproject.toml`（`pywrangler` 会从 cwd 向上找 pyproject，两者必须同级）。

拓扑理由见 `docs/adr/0002-single-worker-topology.md`。平台硬限制见根目录 `CLAUDE.md`。

---

## 运行时是踩雷区，先读这一节

这些不是偏好，是核实过的运行时行为。违反其中任何一条都会在部署后才炸。

**1. 入口类必须叫 `Default` 并继承 `WorkerEntrypoint`。** 模块级 handler（`on_fetch` / `on_scheduled`）自 2025-08-14 起默认禁用。

**2. `asgi.entrypoint(app)` 不够用。** 它返回的类只有 `fetch`，没有 `scheduled`。所以 `backend/src/main.py` 自己写了两个 handler。两者签名不同且都是强制的：`fetch(self, request)` 只收 request，`scheduled(self, controller, env, ctx)` 四个参数全要。

**3. `env` 只能属性访问。** `env.LLM_API_KEY` 可以，`env["LLM_API_KEY"]` 会抛异常——包装器只实现了 `__getattr__`。

**4. FastAPI 路由里没有 `self.env`。** 从 ASGI scope 取：`request.scope["env"]`。已封装在 `api/deps.py`，路由不要直接碰 scope。cron 路径则是 `env` 参数直传，这就是 `run_sync(env)` 显式收参而不是读全局的原因。

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
    └── sync.py                # cron 编排（骨架，见下）
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

**纯层（默认，785 个测试约 1.8 秒，其中 4 个已知失败）** —— 普通 CPython pytest。覆盖 `domain/`、`persistence/read_queries.py` 的 SQL 与游标、`sync.py` 的 SQL 常量、`d1.py` 的上限守卫。D1 是 SQLite，所以表结构约束、幂等、抢占租约、键集分页全部用 stdlib `sqlite3` 跑真实迁移来验证——不需要 Worker。

测试直接 import 代码里的 SQL 常量（如 `from linuxdo_oss.sync import CLAIM_TOPIC_SQL`），不抄副本，避免测试与实现漂移。

**已知失败，与 LLM 改动无关**：`test_write_repository.py` 有 4 个测试挂在 `claim_topic` 返回 `False`，在未改动的工作树上可复现（`git stash` 验证过）。该文件只 import `persistence/`。

根因在 **fixture 侧，不是生产代码**，commit `325ac81` 的 message 已记录：`seed_topic` 对已结算的主题无条件断言抢占成功；`make_post` 的 guid 未按 topic 隔离，撞上全表 UNIQUE 的 `idx_topic_posts_guid`。修它要改 fixture。改动前请以此为基线，不要把它当成自己引入的回归。

**Worker 层（`@pytest.mark.worker`，尚未编写）** —— 起 `pywrangler dev` 子进程，用 `requests` 做黑盒 HTTP 断言。只有 JsProxy 转换、`batch()` 原子性、ASGI + cron 共存这些必须真运行时的东西才放这层。

---

## 未完成的部分

`sync.py` 的 `_run()` 抛 `NotImplementedError`，消息里写明了实现顺序。抢占/租约/计数器机制已固定并有测试，接端口进去即可，不要重构它。

LLM 边界已完成：`classifier.py`（分类业务）+ `llm/`（三协议 wire 适配器 `LLM_PROTOCOL`、三档 schema 降级 `LLM_SCHEMA_MODE`、候选仓库白名单、Pydantic 二次校验）。见 `docs/adr/0005-llm-multi-protocol.md`。它只缺调用方——`sync.py` 还没接。

尚未编写的模块，每个都要在 `domain/`（纯）与 `adapters/`（运行时）之间划清边界：

1. `feeds/channel.py` —— 读 RSSHub feed，从 `link` / `title` / `description` 各字段抽 topic id。**真实 feed 已确认**：RSS 2.0，item 只有 `title`/`description`/`link`/`guid`/`pubDate`，topic 链接藏在 `description` 的 HTML 里且带楼层后缀（`/t/topic/2837720/1`），`link` 指向 Telegram 而非 linux.do。
2. `feeds/topic.py` —— 读单主题 RSS，选首帖 + 含 GitHub 链接的回复。HTML→文本用 stdlib `html.parser`。
3. `feeds/xml_safe.py` —— **不要假定 `defusedxml` 在 Pyodide 下可用**（`[UNKNOWN]`，未在文档、Pyodide 索引或任何官方示例中出现，且 2021 年后未发版）。用 stdlib `xml.etree.ElementTree`，加上：字节上限、解析前拒绝前 4 KiB 含 `<!DOCTYPE` 或 `<!ENTITY` 的输入。
4. `persistence/write_repository.py` —— `batch()` 幂等 upsert。

## 三个 `[UNKNOWN]`，都要 spike 验证

1. **部署后 Worker 出网连通性**。`spikes/egress/` 已备好，先跑它。**必须看部署后的 URL**，`pywrangler dev` 可能从开发机出网从而掩盖封锁。
2. **`workers.fetch` 的超时机制**。它没有 timeout 选项。`adapters/http.py` 叠了两层：`signal=AbortSignal.timeout(ms)`（kwargs 原样进 JS `RequestInit`，workerd 里有这个 API，但无任何 Python 侧文档或示例）+ `asyncio.wait_for` 外层兜底。第一层是否生效要在部署后的 Worker 上验证。
3. **自定义 LLM 端点是否真支持 strict 结构化输出**。ADR 0005 明确**不做**运行时能力探测（Worker 无状态，探测结果无处缓存；且"探测失败就换档"正是被禁止的静默回退）。端点不支持时把 `LLM_SCHEMA_MODE` 配成 `json_object` 或 `none`——防幻觉保证不变，因为它靠的是 `_reject_unknown_and_duplicate()` 而不是 schema。**这一项现在可绕开，但仍未被验证**：部署前应人工验证真实端点，再据此定档。
