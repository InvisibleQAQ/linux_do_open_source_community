# ADR 0003：数据层用 TanStack Query，不跟随模板的 SWR

- 日期：2026-08-31
- 状态：已接受

## 背景

PRD 技术栈写的是 TanStack Query。UI 基座 Minimal v7.7.0 自带 SWR ^2.4.1 且不含 TanStack Query。二者只能留一个。

"向模板对齐"曾是候选理由，但它站不住：**Minimal 的 `src/theme/**`、`src/layouts/**` 以及本项目移植的所有 `src/components/*` 对 `swr` 和 `axios` 零引用**。SWR 只出现在 `src/actions/**`、`src/auth/**`、`src/sections/_examples/**`，而这三块在一个公开只读站点里全部丢弃。所以在数据层上，模板没有给出任何对齐对象。

## 决定

用 `@tanstack/react-query@^5.102.8`，不引入 SWR。

## 依据

判据来自 PRD 的具体要求，不是偏好。

**1. `GET /api/topics` 是游标分页。**

TanStack 的 `useInfiniteQuery` 把游标当一等公民：`initialPageParam` + `getNextPageParam(lastPage) => lastPage.next_cursor ?? undefined`，返回 `hasNextPage` / `fetchNextPage` / `isFetchingNextPage`，另有 `maxPages` 可封顶内存。

SWR 的 `useSWRInfinite` 是按页序号驱动的：key 签名是 `(index: number, previousPageData) => Args`，前进靠 `setSize(size + 1)`，响应对象只多出 `size` / `setSize`，**没有 `hasNextPage`**，要自己从末页游标推导。

**2. 倒序 feed 的头部会插入新主题。**

`useSWRInfinite` 的 `revalidateFirstPage` 默认为 `true`（已在 `swr@2.4.1` 的 `dist/infinite/index.js` 中核实），即每次 load-more 都重取第一页。对"按时间倒序、头部持续插入"的 feed，这会让游标链与已渲染列表错位或重复，需要额外兜底。

**3. PRD 要求显式的 loading / empty / **partial-data** / error 四态。**

TanStack 有两轴状态机：`status`（pending·error·success）× `fetchStatus`（fetching·paused·idle），外加 `isFetchingNextPage`、`isPlaceholderData`。"首次加载"与"已有数据但在后台刷新"天然可分——后者正是 partial-data。

SWR 的响应对象状态字段全集是 `data / error / mutate / isValidating / isLoading`（已在 `swr@2.4.1` 的 `dist/_internal/types.d.ts` 中核实）。能表达，但四态要自己拼。

**4. PRD 要求测试覆盖三态。**

TanStack 的隔离方式是每个测试新建 `QueryClient({ retry: false, gcTime: 0 })`，见 `frontend/src/test/render.tsx`。SWR 需要 `<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>`，漏一个就跨测试污染。

## 后果

- `swr` 与 `axios` 都不进依赖树。
- 所有数据获取集中在 `frontend/src/api/hooks.ts`，组件内不出现 `fetch`。
- 缓存键集中在 `frontend/src/api/query-keys.ts`，避免 hooks 之间键形态漂移。
- 重试策略按错误类型分流：`ApiError.isRetryable`（status 0 或 ≥ 500）才重试，404 不重试，见 `frontend/src/api/query-client.ts`。
- **不采用 React Router 的 `loader`**：PRD 要的四态和游标分页需要在组件里可寻址 `isPending` / `isError` / `isFetchingNextPage`，用 loader 会与 query 缓存重复一层。路由只做 `Component` 壳。

## 验证

`frontend/src/features/topics/feed-view.test.tsx` 断言了游标链实际取值 `[null, 'cursor-2']`、`next_cursor: null` 时"加载更多"消失，以及空态与可重试错误态。4/4 通过。

## 未采纳但记录在案

用 `openapi-typescript` 从 FastAPI 的 `/openapi.json` 生成类型、配 `openapi-fetch` 消费，可以消除前后端类型漂移。当前手写 `frontend/src/api/types.ts`（约 100 行，4 个端点）更直白，且不让前端构建依赖运行中的后端。若契约开始频繁变动，再引入这条路径，并把生成的 `.d.ts` 提交进仓库、手动再生成而非放进构建。
