# ADR 0002：单 Worker 拓扑（SPA + API + Cron 同体）

- 日期：2026-08-31
- 状态：已接受
- 相关：PRD "Implementation Decisions / Technology stack" 与 "Security and operations"

## 背景

PRD 写的是"frontend on Cloudflare Pages or Workers Assets; backend deployed with `pywrangler`"，读起来像两个部署单元。需要定下真实拓扑。

## 决定

**部署一个 Cloudflare Worker**，由根目录 `wrangler.jsonc` 描述，同时承担静态资源、`/api/*` 和 5 分钟 cron。

```jsonc
"main": "backend/src/main.py",
"compatibility_flags": ["python_workers"],
"assets": {
  "directory": "./frontend/dist/",
  "binding": "ASSETS",
  "not_found_handling": "single-page-application",
  "run_worker_first": ["/api/*"]
}
```

## 依据（均为已核实的硬约束）

1. **不能用 `@cloudflare/vite-plugin`**：它的 `maybeResolveMain` 只解析 `.js/.mjs/.ts/.mts/.jsx/.tsx` 入口，`.py` 不被识别为有效入口模块；其文档列出的非 JS 模块支持仅限 `.txt/.html/.sql/.bin/.wasm`。所以前端是纯 `vite build`，Worker 由 `pywrangler` 驱动，两者不共用 dev server。
2. **Cloudflare 官方引导新项目用 Workers 而非 Pages**：Pages 文档挂有横幅"Start new projects with Workers"，官方博客明确"all of our investment, optimizations, and feature work will be dedicated to improving Workers"。Pages 未废弃但实质冻结。
3. **一个 Python Worker 可以同时服务静态资源和 FastAPI**：这是 Cloudflare 自己在 Python Workers 的 FastAPI 页面上文档化的组合。
4. **一个 Worker 只能配置一组静态资源**，所以资源必然挂在这一个 Worker 上。

## 关键取舍：`run_worker_first: ["/api/*"]` 而非 `true`

Cloudflare 文档给了两种做法：

- **采用**：`run_worker_first: ["/api/*"]`。只有 `/api/*` 进 Python，其余路径与 SPA 回退全部由边缘资源路由处理，**普通页面访问零 Python 调用**。需要 Wrangler ≥ 4.20.0。
- 不采用：`run_worker_first: true` + FastAPI catch-all 路由 `@app.get("/{path:path}")` 里 `await env.ASSETS.fetch(...)`。正确但每个静态文件请求都要付一次 Python Worker 调用。

选前者的决定性理由：Python Worker 即使有自动内存快照，冷启动也约 1 秒（无快照约 10 秒），而 Worker 启动时间上限就是 1 秒。让静态资源绕开 Python 是把这个风险从关键路径上移走。

## 后果

- **MVP 不需要 CORS**。单一源，不存在跨源请求。PRD 的"Public API CORS is restricted to configured frontend origins"在本拓扑下无对象；只有将来真被迫拆分才引入，且优先用 service binding 而非公开路由。
- `frontend/src/global-config.ts` 的 `serverUrl` 生产环境保持为空，客户端调同源 `/api`。开发期由 `frontend/vite.config.ts` 的 proxy 指向 `127.0.0.1:8787`。
- `frontend` 不持有 `wrangler` 依赖，也没有自己的 `wrangler.jsonc`；它只负责产出 `dist/`。
- 自定义域用 `"routes": [{"pattern": "...", "custom_domain": true}]`，它把该主机名的所有路径指向本 Worker——正好符合单 Worker 拓扑。
- `frontend` 的 `build.outDir` 必须与 `wrangler.jsonc` 的 `assets.directory` 保持一致（`frontend/dist/`）。改一个要改另一个。
