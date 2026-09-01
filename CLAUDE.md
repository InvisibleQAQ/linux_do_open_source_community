# Linux.do 开源项目聚合器

自动采集 Linux.do 主题 RSS，用 LLM 整理原帖中**明确出现**的 GitHub 仓库，前端按主题聚合展示。

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

# D1
npx wrangler d1 create linuxdo-oss                    # 把返回的 id 填进 wrangler.jsonc
npx wrangler d1 migrations apply linuxdo-oss --local
npx wrangler d1 migrations apply linuxdo-oss --remote

# 密钥（绝不进仓库）
uv run pywrangler secret put LLM_API_KEY
```

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

**PRD 的设计需要 Workers Paid。** 单轮 20 主题 × (2 次 RSS + 1 次 LLM) ≈ 60 次子请求，已超 Free 的 50；Free 的 cron CPU 只有 10 ms，连解析 RSS 都不够。

由此推出两条编码约束：
1. **并发上限 ≤ 6**（同时等待响应头的连接数上限）。
2. **全局作用域要轻**。启动时间上限 1 s，而 fastapi + pydantic 的导入在有内存快照的情况下已约 1 s（无快照约 10 s）。只在模块级导入 `fetch`/`scheduled` 必用的东西，其余在处理函数内惰性导入。出网用 `from workers import fetch`，**不引入 httpx**——它的同步客户端会阻塞 isolate，且多一个模块级导入就是多一份启动成本。

---

## 未解决的发布前置项

- **RSSHub 出网已验证**：spike Worker 通过 `workers.fetch` 拿到 HTTP 200 / `application/xml` / 21,731 字节真实 RSS。
- `[UNKNOWN]` **linux.do 的部署后 Worker 出网未验证**。本机跑 `pywrangler dev` 得到 403 Cloudflare 挑战页，但那**不能作为证据**——`pywrangler dev` 从开发机出网，而本机 IP 正被挑战（三种 UA 全部 403，同网络 curl RSSHub 也超时）。项目所有者确认该源在其环境可用，管线按此推进。真正关闭这一项需要：

  ```bash
  cd spikes/egress && uv run pywrangler deploy --temporary   # 无需登录
  curl https://<返回的地址>/
  ```

  部署后的 Worker 若仍返回 `Just a moment...`，说明 Cloudflare Worker egress 被 linux.do 真实拦截——这是应用代码无法绕过的阻塞项。
- `[UNKNOWN]` 自定义 LLM 端点是否真的实现了 Responses API 的 strict `text.format` 结构化输出。必须有能力探测，不允许静默回退到 Chat Completions。

顺带一个实测数字：Python Worker 本地冷启动前两次请求耗时 **27.6 s / 25.0 s**，热请求 551 ms / 832 ms。这正是把 `assets.run_worker_first` 限定在 `/api/*` 的理由——让普通页面访问完全不碰 Python。
