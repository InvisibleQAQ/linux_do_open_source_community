# ADR 0001：前端依赖版本锁定

- 日期：2026-08-31
- 状态：已接受

## 背景

PRD 只写了"React, TypeScript, Vite, MUI, React Router"，没有锁版本。UI 要求对齐 Minimal v7.7.0 模板。而 2026-08 时点上，若无条件取最新版会打断工具链。

## 决定

| 包 | 锁定 | 最新 | 理由 |
|----|------|------|------|
| `typescript` | `~5.9.3` | 7.0.2 | **`typescript-eslint@8.68.0` 的 peer 是 `typescript >=4.8.4 <6.1.0`**。用 TS 7 会直接打断类型感知 lint。TS 7 另外移除了 `baseUrl`、`moduleResolution: node`，也不是 drop-in |
| `eslint` | `^9.39.2` | 10.9.1 | **`eslint-plugin-import@2.32.0` 的 peer 上限是 `^9`**。`typescript-eslint`、`react-hooks`、`perfectionist` 都已支持 10，但 import 插件不支持 |
| `eslint-plugin-perfectionist` | `^4.15.1` | 5.11.0 | 模板的 `eslint.config.mjs` 按 v4 的 `customGroups` 对象形态写。沿用配置的价值（153 个移植文件零告警）大于升级 |
| `react-router` | `^7.15.0`（解析到 7.18.3） | 8.3.1 | 移植的 `routes/components` `routes/hooks` 和模板布局代码按 v7 写。v8 把 `RouterProvider` 移到 `react-router/dom` 且要求 Node ≥ 22.22.0 |
| `framer-motion` | `^12.38.0`（解析到 12.43.0） | 13.1.1 | 移植的 `components/animate` 按 v12 写 |
| `@mui/material` | `^9.4.0` | 9.4.0 | 已是最新。v9 于 2026-04-07 stable，跳过 v8 以对齐 MUI X v9 |
| `vite` | `^8.2.2` | 8.2.2 | 已是最新。`@cloudflare/vite-plugin@1.54.2` 的 peer 含 `^8.0.0`（虽然本项目不用该插件，见 ADR 0002） |
| `@mui/x-date-pickers` | `^9.12.0`，**devDependency** | — | 纯类型依赖，见 `frontend/CLAUDE.md` theme 手术第 3 条 |

`@emotion/react` 和 `@emotion/styled` 显式列入 `dependencies`：它们在 `@mui/material@9.4.0` 的 `peerDependenciesMeta` 里标记为 optional，包管理器不会自动装，缺了会在运行时报错。同样标记为 optional 的 `@mui/material-pigment-css` 不采用。

## 后果

- 前四项是**被约束锁住**，不是疏于升级。升级路径有依赖顺序：先等 `eslint-plugin-import` 支持 ESLint 10、`typescript-eslint` 支持 TS 6.1+，才能动 TS/ESLint。
- react-router 与 framer-motion 的升级是独立的，但会牵动移植代码，属于单独一次改动，不要和别的事混在一起做。
- 推翻本 ADR 请新增 ADR，不要改这一份。

## 验证

`pnpm check` 在此组合下实测：`tsc` 0 错误、`eslint` 0 问题、`vitest` 4/4 通过、`vite build` 952 ms 成功。
