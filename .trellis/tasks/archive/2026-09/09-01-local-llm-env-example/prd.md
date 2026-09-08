# 统一本地 LLM 环境变量示例

## Goal

为本地 Wrangler 开发提供唯一、可执行的 LLM 配置示例，明确
`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 的归属，避免 `.dev.vars` 和 `.env`
并存后其中一份被静默忽略。

## What I Already Know

- 用户明确要求增加根目录 `.env.example`，并说明两个 LLM 配置项填在哪里。
- 当前 `.dev.vars.example` 只声明 `LLM_API_KEY`。
- 当前 `LLM_MODEL` 在 `wrangler.jsonc` 的 `vars` 中，值仍为占位符。
- Wrangler 本地开发支持 `.env` 覆盖 `env` 绑定，但 `.dev.vars` 与 `.env`
  是互斥的配置来源。
- `backend/src/linuxdo_oss/config.py` 同时要求 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，
  且强制 `LLM_BASE_URL` 为 `https://`；`classifier.py` 的 `_responses_url()` 负责拼
  接 `/responses`。代码层对 `LLM_BASE_URL` 的支持已存在，缺的只是示例与文档
  （2026-09-01 追加）。
- **2026-09-08 更新**：`_responses_url()` 已被 `09-08-llm` 移除。路径拼接改由
  `llm/protocol.py::endpoint_url()` 按 `LLM_PROTOCOL` 处理，`.env.example` 随之新增
  `LLM_PROTOCOL` 与 `LLM_SCHEMA_MODE` 两项。本任务的验收标准不受影响，但上面那条
  描述的是历史状态，不要当作当前实现读。

## Requirements

- 正式从 `.dev.vars` 迁移到 `.env` 作为唯一的本地配置来源。
- 根目录 `.env.example` 同时声明 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，
  并说明 `LLM_BASE_URL` 的契约（https、不带 `/responses` 后缀）。
- 删除 `.dev.vars.example`，避免两套互斥示例并存。
- 同步更新 `wrangler.jsonc` 和根目录 `CLAUDE.md` 的配置说明。
- 示例文件不得包含真实密钥。
- 本地配置只能有一个权威来源。
- 线上 `LLM_API_KEY` 仍使用 Wrangler secret，不写入仓库配置。
- 线上 `LLM_BASE_URL` 和 `LLM_MODEL` 仍由 `wrangler.jsonc` 的 `vars` 提供。
- 明确提示旧 `.dev.vars` 必须删除或迁移，否则 Wrangler 会忽略 `.env`。

## Acceptance Criteria

- [x] 根目录存在与实际 Wrangler 加载行为一致的环境变量示例。
- [x] `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 的本地及线上归属清晰。
- [x] 不再存在 `.dev.vars` / `.env` 两套互斥示例造成的歧义。
- [x] 相关 `CLAUDE.md` 与配置注释同步更新。

## Decision (ADR-lite)

**Context**: Wrangler 同时支持 `.dev.vars` 和 `.env`，但 `.dev.vars` 存在时会忽略
`.env`。保留两套示例会产生不可见的优先级错误。

**Decision**: 本地开发统一使用 `.env`；删除 `.dev.vars.example`，新增包含三个
LLM 配置项的 `.env.example`。生产环境继续使用 Wrangler secret + `vars`。

**Consequences**: 现有本地 `.dev.vars` 用户需要迁移或删除旧文件；线上配置方式不变。

## Definition of Done

- 示例与文档一致。
- 不提交任何真实密钥或本地 `.env`。
- 检查 Git 忽略规则和 Wrangler 配置引用。

## Out of Scope

- 创建真实 `.env`。
- 写入真实 API key。
- 部署 Worker 或设置线上 secret。

## Technical Notes

- `.gitignore` 已忽略 `.env` 并放行 `.env.example`。
- Cloudflare 文档确认 Wrangler 本地开发支持 `.env` 覆盖 `env` 对象变量。
- 迁移前约定来自 `.dev.vars.example`；该文件已删除，替代约定见
  `.env.example`、`wrangler.jsonc` 和根目录 `CLAUDE.md`。
- 最终合同记录在 `.trellis/spec/backend/environment-configuration.md`。
