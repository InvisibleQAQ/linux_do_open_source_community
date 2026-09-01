# 前端

React SPA，产出静态资源交给根目录的 Worker 托管。公开只读，无登录、无写操作、无 i18n。

## UI 基座：Minimal v7.7.0 白名单移植

设计体系来自商业模板 Minimal v7.7.0 (minimals.cc)，采取**白名单移植**而非整包复制：只搬设计体系，业务代码全部自写。

模板源路径（新增图标、补组件时的取件处）：
```
35_minimal/minimal-dashboard/minimal-dashboard v7.7.0/Vite.js (JavaScript，TypeScript)/minimal-vite-ts-main/
```

### 移植了什么

| 目录 | 来源 | 状态 |
|------|------|------|
| `src/theme/` | 模板 `src/theme/` | 原样搬 + 5 处手术，见下 |
| `src/layouts/core/` | 模板 `src/layouts/core/` | 原样搬。这是布局原语：`LayoutSection` / `HeaderSection` / `MainSection` |
| `src/components/{iconify,scrollbar,logo,label,empty-content,animate,progress-bar,snackbar,custom-breadcrumbs,settings}/` | 模板同名目录 | 原样搬 |
| `src/layouts/components/settings-button.tsx` | 模板同名文件 | 原样搬 |
| `eslint.config.mjs` `prettier.config.mjs` `.editorconfig` | 模板根目录 | 原样搬。**沿用是刻意的**：移植进来的 153 个文件在这套 import 排序规则下零告警，换配置会炸出几百个错误 |

### 没移植什么（以及为什么）

- `src/layouts/main|dashboard|auth-*` —— `main` 是营销站外壳（mega-menu + `src/_mock`），`dashboard` 假设已登录用户和侧边导航。本站是公开 feed，直接在 `layouts/core` 上自建 `src/layouts/feed/`。
- `src/actions/**` `src/auth/**` `src/sections/**` `src/locales/**` `src/_mock/**` —— 全部不需要。**SWR 只存在于这几个目录**，所以移植后依赖树里没有 SWR，数据层用 TanStack Query 不产生双套心智。
- `@mui/lab` `@mui/x-data-grid` `@mui/x-tree-view` `firebase` `aws-amplify` `@supabase/supabase-js` `@auth0/auth0-react` `fullcalendar` `tiptap` `apexcharts` `maplibre-gl` `i18next` `react-hook-form` `zod` `axios` `swr` 等 —— 模板 60+ 运行时依赖裁到 17 个。

### theme 的 5 处手术（改动前先读这里）

1. 删 `core/components/{mui-x-data-grid,mui-x-tree-view,mui-x-date-picker,timeline}.tsx` 及 `core/components/index.ts` 里对应的 import 与展开 —— 砍掉 3 个 MUI X 包和 `@mui/lab`。
2. 删 `with-settings/right-to-left.tsx` 及其导出 —— 本站 LTR only，顺带砍掉 `@emotion/cache` 和 `@mui/stylis-plugin-rtl`。
3. `extend-theme-types.d.ts` 保留 `@mui/x-date-pickers/themeAugmentation`。**这不是遗漏**：`core/components/text-field.tsx` 给 picker 输入槽（`MuiPickersInputBase` 等）写了样式，那些主题键必须在类型层存在。它是纯类型引用，无运行时导入，所以 `@mui/x-date-pickers` 在 `devDependencies`，构建时完全擦除。文件顶部有注释说明。
4. `theme-provider.tsx` 摘掉 `useTranslate`（无 i18n）和 `<Rtl>`。
5. `components/settings/types.ts` 把 `direction` / `navLayout` / `navColor` / `fontFamily` 改为可选，并从 `settings-config.ts` 的 `defaultSettings` 里省掉。**这是用模板自带的扩展点**：`SettingsDrawer` 的开关可见性由 `hasKeys(defaultSettings, [...])` 决定，省掉 key 就自动隐藏对应开关，不用改 drawer 代码。`with-settings/update-core.ts` 里给 `direction` 和 `fontFamily` 补了显式默认值，避免 `undefined` 透传进 `createTheme`。

### 图标：`src/components/iconify/icon-sets.ts` 已裁剪

模板内联 224 个图标共 167 KB，是一个对象，无法 tree-shake。现在只保留实际用到的 13 个（10 KB）。

**新增图标**：从模板的 `src/components/iconify/icon-sets.ts` 原样复制条目粘进来。不用担心漏加——`IconifyName` 类型由这些 key 推导，漏加会在调用点变成 TypeScript 错误，而不是运行时静默走 CDN。（骨架搭建时 `settings/drawer/fullscreen-button.tsx` 的三元动态图标就是这样被类型检查抓到的。）

---

## 目录

| 路径 | 职责 |
|------|------|
| `src/api/` | 唯一的后端出口。`types.ts` 是 API 契约，`client.ts` 是 `apiGet` + `ApiError`，`hooks.ts` 是 TanStack Query hooks，`query-keys.ts` 集中所有缓存键 |
| `src/features/topics/` `src/features/projects/` | 业务视图。`features/*` 在 eslint 的 import 分组里属于 `internal`，必须排在 `src/components/*` 之前 |
| `src/components/states/` | 四态面板（加载 / 空 / 错误 / 后台刷新）。**自写，非模板** |
| `src/layouts/feed/` | 本站唯一布局，建在 `layouts/core` 之上 |
| `src/pages/` | 路由壳。约定：`const metadata = { title }` + `<><title>{...}</title><View /></>`（React 19 原生文档元数据） |
| `src/routes/` | `paths.ts` 自写；`components/` `hooks/` 从模板搬 |
| `src/test/` | `render.tsx` 带全部 Provider 且每次新建 QueryClient；`msw-server.ts` 无默认 handler（漏声明就报错，不会静默打网络） |

## 硬性约定

- **数据获取只走 `src/api/hooks.ts`**。组件里不出现 `fetch`。
- **游标分页只认 `next_cursor === null`** 作为到底信号，禁止用 `items.length < limit` 推断。
- **四态必须显式处理**：先 `isPending`（首次加载）→ 再"错误且无缓存数据"→ 再空 → 最后数据 + 后台刷新提示。`isPending` 与 `isFetching` 的区别是关键：重新获取时不能把已有列表塌回骨架屏。
- **样式只用 `sx`**。MUI v9 已移除 Box / Grid / Stack / Typography / Link 的 system props，`<Box mt={2}>` 是非法的。
- **外链必须 `target="_blank" rel="noopener noreferrer"`**。
- **`post.text` 是后端转好的纯文本**，用 `<Typography>` 渲染，永远不用 `dangerouslySetInnerHTML`。
- 新增业务代码走 `pnpm lint:fix` 修 import 排序，不要手工调整。

## 版本锁定的理由

见 `../docs/adr/0001-frontend-dependency-pins.md`。摘要：TypeScript 锁 5.9（`typescript-eslint@8` 的 peer 是 `<6.1.0`），ESLint 锁 9（`eslint-plugin-import@2.32` 的 peer 上限是 9），react-router 锁 7（模板布局代码按 v7 写），framer-motion 锁 12（同理）。这三条都不是"没跟上最新"，是有约束的。

## 实测数据（2026-08-31 骨架完成时）

- `tsc` 0 错误，`eslint` 0 问题，`vitest` 4/4 通过
- `vite build` 952 ms
- 主包 705 kB（gzip 220 kB）；`empty-content-*.js` 224 kB 是 `MotionLazy` 惰性加载的 framer-motion 特性包，不在首屏关键路径
- 优化余量：MUI 是体积主因；若要进一步压，先考虑去掉 framer-motion（`components/animate` + `settings-button` 依赖它）
