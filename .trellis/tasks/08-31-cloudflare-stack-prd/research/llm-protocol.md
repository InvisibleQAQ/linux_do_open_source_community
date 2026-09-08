# LLM Protocol Decision

> **Amended 2026-09-08 by `docs/adr/0005-llm-multi-protocol.md`.** The decision below
> described a single protocol. Three are now supported (`LLM_PROTOCOL`), and the
> "do not fall back" rule was narrowed to "do not fall back **silently**" — a weaker
> schema mode is a configuration choice (`LLM_SCHEMA_MODE`), never a runtime reaction
> to a failure. The compatibility requirements and the boundary note below still hold
> verbatim for the `responses` protocol under `strict` mode. Nothing here was deleted.

## Decision

- Use the OpenAI Responses API request/response shape.
- Allow a custom HTTPS API root through `LLM_BASE_URL` and select the model through `LLM_MODEL`.
- Call `<base_url>/responses` through a small HTTP adapter rather than requiring the OpenAI Python SDK in the Python Workers runtime.
- Require Responses API Structured Outputs using `text.format` with `type: json_schema` and `strict: true`.
- Keep Pydantic validation at the application boundary even when the endpoint claims strict schema support.
- Do not fall back silently to Chat Completions or unstructured JSON when a custom endpoint is only partially compatible.

## Compatibility requirements

- Accept the Responses API `input` message shape.
- Accept strict JSON Schema under `text.format`.
- Return typed response output items or an explicitly handled refusal/incomplete/failure state.
- Preserve stable behavior for the configured model under the supplied schema.

## Official reference

- [Structured model outputs | OpenAI API](https://developers.openai.com/api/docs/guides/structured-outputs) documents Structured Outputs for Responses and the `text.format` JSON Schema shape.

## Boundary

OpenAI's official documentation defines the OpenAI endpoint behavior. A third-party custom base URL is compatible only if it implements the required behavior; compatibility cannot be inferred from its URL or marketing claim and must be verified by an integration check.
