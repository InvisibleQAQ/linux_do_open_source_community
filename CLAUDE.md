# Linux.do 开源项目聚合器

自动采集 Linux.do 的 Discourse tag RSS，用 LLM 整理原帖中**明确出现**的 GitHub 仓库，前端按主题聚合展示。

采集只有一次出网：tag feed 的每个 item 本身就带首帖全文，因此没有逐主题抓取。见 `docs/adr/0007-single-source-tag-feed.md`。

需求与验收标准：`.trellis/tasks/08-31-cloudflare-stack-prd/prd.md`
领域术语：`CONTEXT.md`（改术语必须同步这里）

---

## 部署形态：一个 Worker

**整个应用是单个 Cloudflare Worker**，同时承担三件事：静态资源、读 API、5 分钟 cron。配置在根目录 `wrangler.jsonc`。

为什么不是两个 Worker，理由都是硬约束，不要推翻：

- `@cloudflare/vite-plugin` **不能用**。它的 `maybeResolveMain` 只解析 `.js/.mjs/.ts/.mts/.jsx/.tsx` 入口，`.py` 会被拒。所以前端是纯 `vite build`，Worker 由 `pywrangler` 驱动。
- Cloudflare Pages 对新项目已实质冻结（官方："all of our investment... dedicated to improving Workers"），静态资源挂在 Worker 上。
- 一个 Worker 只能配置一组静态资源（`assets`）。

路由分工由 `wrangler.jsonc` 的 `assets` 块决定：

```
/api/*          -> run_worker_first 命中 -> Python Worker (FastAPI)
其他所有路径    -> 边缘资源路由 -> frontend/dist/ 的文件
未匹配的路径    -> not_found_handling: single-page-application -> index.html (200)
```

这带来一个重要性质：**普通页面访问不唤起 Python**，因此不吃 Python Worker 约 1 秒的冷启动。

因为同源，**MVP 不需要 CORS**。PRD 里"CORS 限制到配置的前端源"这条在单 Worker 拓扑下无对象，见 `docs/adr/0002-single-worker-topology.md`。

---

## 目录

| 路径 | 职责 |
|------|------|
| `wrangler.jsonc` | 唯一的 Worker 配置：入口、assets、cron、D1 绑定、非敏感 vars |
| `frontend/` | React SPA，产出 `frontend/dist/`，见 `frontend/CLAUDE.md` |
| `backend/` | Python Worker：FastAPI 读 API + cron 同步编排，见 `backend/CLAUDE.md` |
| `docs/adr/` | 架构决策记录。改技术选型前先读，推翻要新增 ADR，不要改旧的 |
| `.trellis/spec/` | 分层编码规范，写代码前读 |
| `.trellis/tasks/` | PRD、research 笔记、子代理上下文清单 |

**`.trellis/` 是被跟踪的**，规范和它管的代码在同一份历史里。两件随之而来的事：

- 什么不该跟踪由 `.trellis/.gitignore` 说了算（本地身份 `.developer`、per-dev 的
  `.current-task`、`.runtime/`、agent 运行时、备份、缓存）。**永远不要 `git add -f .trellis/`**
  —— `-f` 会绕过那份规则，把上面这些全拖进来。普通 `git add .trellis/` 是对的。
- 项目 `.gitignore` 里有一条 `!.trellis/`。它存在是因为机器级的全局 gitignore 可能带一条
  笼统的 `.trellis/`（本仓库作者的就带）。仓库内的 `.gitignore` 优先级高于
  `core.excludesFile`，所以这条否定规则让跟踪行为不依赖任何人的机器配置。
- 因为 `.trellis/` 不再被忽略，`add_session.py` 与 `task.py archive` 会**自己产生 commit**
  （`chore: record journal` / `chore(task): archive ...`）。这是 Trellis 的既定设计
  （`config.yaml` 的 `session_auto_commit` 默认 true），之前它们因为检测到被忽略而跳过 git。

前后端唯一的耦合面是 API 契约：`frontend/src/api/types.ts` ↔ `backend/src/linuxdo_oss/api/schemas.py`。**改一边必须改另一边**，并更新 `.trellis/spec/frontend/api-contract.md`。

---

## 常用命令

```bash
# 前端（在 frontend/ 下）
pnpm install
pnpm dev                 # :5173，/api 代理到 127.0.0.1:8787
pnpm check               # tsc + eslint + vitest，提交前必跑
pnpm build               # 产出 dist/，供 wrangler assets 使用

# Worker（在仓库根目录）
uv run pywrangler dev    # :8787
uv run pywrangler deploy
curl "http://localhost:8787/cdn-cgi/handler/scheduled"   # 本地触发 cron

# 本地 LLM 配置（只用 .env；不要同时保留 .dev.vars）
cp .env.example .env     # 然后填写 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL

# D1
npx wrangler d1 create linuxdo-oss                    # 把返回的 id 填进 wrangler.jsonc
npx wrangler d1 migrations apply linuxdo-oss --local
npx wrangler d1 migrations apply linuxdo-oss --remote

# 密钥（绝不进仓库）
uv run pywrangler secret put LLM_API_KEY
```

本地 LLM 配置全部从根目录 `.env` 读取（覆盖 `wrangler.jsonc` `vars` 的同名值）。
线上配置分开管理：`LLM_API_KEY` 使用 Wrangler secret，其余全部使用 `wrangler.jsonc` 的 `vars`。
如果旧的 `.dev.vars` 仍存在，Wrangler 会忽略 `.env`；先迁移内容并删除旧文件。

### LLM 三协议（`LLM_PROTOCOL`）

协议由配置选择，**绝不运行时探测**。每个协议自己往 API 根后面拼路径后缀，
所以 `LLM_BASE_URL` 必须是 `https://` 开头、不含 `?` 或 `#`。`/v1` 惯例按协议不同：

| `LLM_PROTOCOL` | 端点 | base 根 |
|---|---|---|
| `responses`（默认） | `{base}/responses` | 惯例带 `/v1`，原样保留 |
| `chat_completions` | `{base}/chat/completions` | 惯例带 `/v1`，原样保留 |
| `anthropic` | `{base}/v1/messages` | **不带** `/v1`，多写的 `/v1` 会被剥掉；鉴权头是 `x-api-key` |

末尾多写了端点路径时，`llm/protocol.py::resolve_base_url` 按**归属**分两路 ——
后缀属于当前协议自己的（是它 `ENDPOINT_SUFFIX` 的路径前缀）就剥掉，`config.py`
每次 Sync Run 打一条 WARNING；属于**别的**协议就在启动时拒绝，因为那种情况配错的
是 `LLM_PROTOCOL` 而不是 URL，剥掉只会把失败推迟到五分钟后的 cron 里。
判据从 `ENDPOINT_SUFFIX` 推导，所以适配器不再各自声明禁用后缀。
见 `docs/adr/0006-llm-base-url-normalization.md`。

vLLM / Ollama / LM Studio / 多数中转站只有 `chat/completions`；Gemini 走它的 OpenAI
兼容层（`/v1beta/openai`），因此本项目不实现 Gemini 原生协议。

### Schema 降级（`LLM_SCHEMA_MODE`）

| 取值 | 含义 |
|---|---|
| `strict`（默认） | 端点强制执行 `canonical_url` 的 `enum`，采样器越不出去。三协议强度一致：`responses` / `chat_completions` 在 schema 旁发 `strict: true`，`anthropic` 把同一个标志发在强制工具定义上——没有它，强制工具调用只绑定字段名（见 `llm/anthropic.py`） |
| `json_object` | 只保证是 JSON，schema 改为进 prompt（DeepSeek / ZhiPu 只接受这档） |
| `none` | 完全不发 output-format 字段（有些端点见到就 400） |

非 `strict` 每轮都打一条 WARNING。**禁止运行时自动回退**：请求失败就是失败，不改档重试。
降级不产生脏数据——编造的仓库仍被 `_reject_unknown_and_duplicate()` 硬拦，代价只是被拒的
decision 变多。理由见 `docs/adr/0005-llm-multi-protocol.md`。

wire 层在 `backend/src/linuxdo_oss/llm/`，一个协议一个模块；`classifier.py` 只管分类业务。

---

## 平台硬限制（会决定设计，别凭印象改）

| 项目 | Free | Paid |
|------|------|------|
| Cron CPU 时间 | **10 ms** | 30 s（间隔 < 1 小时） |
| 子请求数 / 次调用 | **50** | 10,000 |
| Cron 挂钟时长 | 15 min | 15 min |
| 同时等待响应头的连接 | 6 | 6 |
| Worker 体积（gzip） | 3 MB | 10 MB |
| Worker 启动时间 | 1 s | 1 s |

**PRD 的设计需要 Workers Paid。** 单轮 N 个主题（N = `SYNC_BATCH_SIZE`，默认 20）：

* **出网** `1 次 RSS + ≤N 次 LLM`（无候选仓库的主题直接结算成 `not_relevant`，不调 LLM）
* **D1** `6 + (3~4)N` 次 —— 固定 6 次是 open_run / reclaim / 查已知 id / 发现 batch / 查 due /
  close_run；每主题 claim + `load_stored_posts` + 结算，结算走 publish 时是 2 次（`_post_ids` 再
  加一次 batch）

N=20 合计 **87~107 次**，远超 Free 的 50；Free 的 cron CPU 只有 10 ms，连解析 RSS 都不够。
**D1 往返是大头，不是 LLM**，所以再省预算要从合并 D1 查询下手。删掉逐主题抓取省下的是每主题
1 次 RSS，见 ADR 0007。

由此推出两条编码约束：
1. **并发上限 ≤ 6**（同时等待响应头的连接数上限）。
2. **全局作用域要轻**。启动时间上限 1 s，而 fastapi + pydantic 的导入在有内存快照的情况下已约 1 s（无快照约 10 s）。只在模块级导入 `fetch`/`scheduled` 必用的东西，其余在处理函数内惰性导入。出网用 `from workers import fetch`，**不引入 httpx**——它的同步客户端会阻塞 isolate，且多一个模块级导入就是多一份启动成本。

---

## 未解决的发布前置项

- `[UNKNOWN]` **linux.do 的部署后 Worker 出网未验证**。本机跑 `pywrangler dev` 得到 403 Cloudflare 挑战页，但那**不能作为证据**——`pywrangler dev` 从开发机出网，而本机 IP 正被 linux.do 挑战。

  2026-09-08 实测（**RSSHub 已不在链路中**，这里只剩一个源）：
  - **linux.do 全路径从本机都是 403**：`GET https://linux.do/tag/2234-tag/2234.rss` 返回 7,100 字节挑战页，与 topic RSS 表现一致。

  所以本地把 `TAG_FEED_URL` 指向 linux.do 时，cron 第一步就失败，到不了 `published`。
  **但这不再等于本地无法验证**：期望 host 由 `TAG_FEED_URL` 推导而非硬编码，把它指向任一可达的
  Discourse 实例（`https://meta.discourse.org/tag/rss.rss` 实测 200 / 81,541 字节）即可端到端跑通
  解析 → 入库 → 分类。真正关闭这一项需要：

  ```bash
  cd spikes/egress && uv run pywrangler deploy --temporary   # 无需登录
  curl https://<返回的地址>/
  ```

  部署后的 Worker 若仍返回 `Just a moment...`，说明 Cloudflare Worker egress 被 linux.do 真实拦截——这是应用代码无法绕过的阻塞项。
- `[UNKNOWN]` **`2234-tag` 这个 tag 的内容构成未经核实**：本机 403 取不到任何样本。整条管线的产出质量取决于这个 tag 选得对不对。
- **strict 结构化输出已在一个真实端点上验证**（2026-09-08，`supercodes.vip/v1` + `gpt-5.6-terra`）。
  发了与 `llm/responses.py::build_payload` 同形的请求：`text.format` 带 `strict: true` 和
  `canonical_url` 的 `enum`，回来 HTTP 200 / `status: completed`，输出严格合 schema、enum 被遵守、
  summary 是中文。所以 `LLM_SCHEMA_MODE=strict` 对这个端点是正确配置。

  这条验证**绑定具体端点加模型**，换任何一个都要重跑一次——ADR 0005 不做运行时能力探测
  （Worker 无状态，探测结果无处缓存，而"探测失败就换档"正是被禁止的静默回退）。
  端点不支持 strict 时，把 `LLM_SCHEMA_MODE` 配成 `json_object` 或 `none` 即可，防幻觉保证不变。

  验证同一端点时顺带测出来的两件事，会浪费很多时间，记在这里：
  - **`/v1` 必须写在 `LLM_BASE_URL` 里。** `responses` 协议只往 base 后面拼 `/responses`，不会补 `/v1`。
    写成 `https://supercodes.vip` 会打到 `/responses`，返回 502。
  - **`GET /v1/models` 的列表不可信。** 它只列了 `gpt-5.5` 和 `gpt-image-2`，而 `gpt-5.5` 实际请求返回 502，
    列表里没有的 `gpt-5.6-terra` 才真正可用。别拿 `/models` 当模型是否可用的依据。

顺带一个实测数字：Python Worker 本地冷启动前两次请求耗时 **27.6 s / 25.0 s**，热请求 551 ms / 832 ms。这正是把 `assets.run_worker_first` 限定在 `/api/*` 的理由——让普通页面访问完全不碰 Python。
