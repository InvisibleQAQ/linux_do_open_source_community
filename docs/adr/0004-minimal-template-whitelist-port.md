# ADR 0004：Minimal 模板采取白名单移植

- 日期：2026-08-31
- 状态：已接受

## 背景

UI 要求对齐商业模板 Minimal v7.7.0 (minimals.cc)。模板 `src/` 约有 2000+ 文件、60+ 运行时依赖，覆盖 kanban / chat / mail / 日历 / 发票 / 电商等场景。本项目只需要一个公开只读的主题 feed。

## 决定

**白名单移植**：新建干净 Vite 8 工程，只搬设计体系（`theme/` + `layouts/core/` + 10 个低耦合 components），业务代码全部自写。

考虑过并否决的两个方案：

- **整包复制后删减**：出错概率低，但 `package.json` 带 60+ 依赖（firebase / amplify / supabase / fullcalendar / tiptap / apexcharts / maplibre 全在），删依赖是长尾手术，且残留的死代码会污染此后所有 AI 上下文和搜索结果。
- **只抄设计 token**：只搬 palette / typography / shadows，组件覆写和布局自写。依赖最少，但 Minimal 的质感恰恰在那 40+ 个组件覆写文件里，丢掉就只能做到"神似"。

## 依据

移植前用 grep 算出了依赖闭包，结论支持白名单可行：

- `src/theme/**` 的外部依赖只有 `@mui/material`、`minimal-shared/utils`（30 处）、`@emotion/react`，加上 4 个可切除的 MUI X / lab 类型引用；对 `src/*` 的引用只有 `src/components/settings`（4 处，其中 3 处是纯类型）和 `src/locales`（1 处，可切）。
- `src/layouts/core/**` 只依赖 `@mui/material`、`minimal-shared/{hooks,utils}` 和 `src/theme/create-classes` —— 极干净，原样可用。
- 白名单的 10 个 components 每个只带 1 个 npm 依赖（`@iconify/react` / `simplebar-react` / `nprogress` / `sonner` / `framer-motion` / `es-toolkit`）。
- `minimal-shared@1.1.6` 是公开 MIT 包，运行时只依赖 `es-toolkit`，不依赖 MUI，不构成对模板的锁定。

结果：60+ 运行时依赖降到 17 个，`src/` 153 个移植文件。

## 后果

- 沿用模板的 `eslint.config.mjs` / `prettier.config.mjs`。**这是刻意的**：移植文件在这套 import 排序规则下零告警，自写配置会炸出几百个错误。代价是新写业务代码要遵守同一套分组顺序（`pnpm lint:fix` 可自动修）。
- theme 做了 5 处手术，逐条记录在 `frontend/CLAUDE.md`，改 theme 前必须先读。
- `src/components/iconify/icon-sets.ts` 从 224 个图标（167 KB）裁到 13 个（10 KB）。新增图标从模板原样复制条目；`IconifyName` 类型会在调用点拦住漏加。
- 模板路径是持续的取件处，不是一次性来源。新增组件时先看模板有没有现成的，再自己写。
- 版本跟随模板而非无条件取最新，理由见 ADR 0001。

## 许可

Minimal 是商业模板，用户持有授权副本。把设计体系移植进用户自己的项目属于授权范围内的正常用法。移植代码不对外分发。
