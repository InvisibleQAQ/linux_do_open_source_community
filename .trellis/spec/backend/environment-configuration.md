# Environment Configuration

## Scenario: LLM configuration

### 1. Scope / Trigger

Applies whenever code, local tooling, or deployment configuration reads or changes
`LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_PROTOCOL`, `LLM_SCHEMA_MODE`, or
their source files.

### 2. Signatures

```bash
cp .env.example .env
uv run pywrangler dev
uv run pywrangler secret put LLM_API_KEY
```

Python reads every value through attribute access on the Worker environment; see
`backend/src/linuxdo_oss/config.py`. The request URL is built by
`endpoint_url(protocol, base_url)` in `backend/src/linuxdo_oss/llm/protocol.py`,
which appends the selected protocol's own path suffix.

### 3. Contracts

| Environment | `LLM_API_KEY` | everything else |
|-------------|---------------|-----------------|
| Local | root `.env` | root `.env` (overrides `vars`) |
| Production | Wrangler secret | `wrangler.jsonc` `vars` |

`LLM_BASE_URL` is the **Classifier Endpoint**: an API root, never a full endpoint
path (see `CONTEXT.md`). It must start with `https://` and must not contain `?` or
`#`. `resolve_base_url` derives the root from the configured value, and the two
kinds of trailing endpoint path are treated oppositely:

- a path prefix of **this** protocol's own `ENDPOINT_SUFFIX` is **stripped**, with
  one WARNING per Sync Run. Pasting the endpoint URL out of a provider's docs is
  the ordinary way this value gets configured and the intent is unambiguous.
- **another** protocol's endpoint path is **refused**. There the wrong value is
  `LLM_PROTOCOL`, and stripping it would produce a root that validates and then
  404s from a cron run. See `docs/adr/0006-llm-base-url-normalization.md`.

The `/v1` convention is per-protocol:

| `LLM_PROTOCOL` | endpoint | root |
|----------------|----------|------|
| `responses` (default) | `{base}/responses` | conventionally ends `/v1`, kept as-is |
| `chat_completions` | `{base}/chat/completions` | conventionally ends `/v1`, kept as-is |
| `anthropic` | `{base}/v1/messages` | carries no `/v1`; a trailing `/v1` is stripped. Auth is `x-api-key` |

`LLM_SCHEMA_MODE` is `strict` (default), `json_object` or `none`. Anything but
`strict` logs one WARNING per run and gives up the structural `enum` guard; the
allowlist re-check is unaffected. There is no runtime capability probe and no
automatic fallback — see `docs/adr/0005-llm-multi-protocol.md`.

`strict` is equally strong across all three protocols, and each one pays for it
differently. Under `responses` and `chat_completions` the endpoint enforces the
schema during decoding. Under `anthropic` the schema travels as a forced tool call
carrying that API's own top-level `strict: true` tool flag, which is what gates
grammar-constrained sampling — a forced tool call without the flag binds the field
names only and is documented as best effort about types and required fields. The
flag is GA, needs no beta header, and requires `additionalProperties: false` on
every object plus every property in `required`, which `build_json_schema` already
satisfies for OpenAI strict mode. An endpoint that does not recognise the flag
answers 4xx, reported as `endpoint_config`; the remedy is a deliberate
`LLM_SCHEMA_MODE` change, never a retry with the flag dropped.

Both are absent-tolerant and typo-intolerant: absent means the pre-split default,
and an unrecognised value aborts rather than defaulting.

`.env.example` contains placeholders only. `.env` and `.dev.vars` are never
committed. `.dev.vars` is legacy and must not coexist with `.env`, because Wrangler
ignores `.env` when `.dev.vars` exists.

### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| Any required value is absent or blank | `load_settings` raises `RuntimeError` |
| `LLM_BASE_URL` is not `https://` | `load_settings` raises `RuntimeError`; `classify` re-checks and raises a non-retryable `ClassifierError` |
| `LLM_BASE_URL` ends with a path prefix of **this** protocol's `ENDPOINT_SUFFIX` | Stripped to the root; one WARNING per Sync Run carrying the suffix and the protocol name, never the URL |
| `LLM_BASE_URL` ends with **another** protocol's endpoint path | `load_settings` raises `RuntimeError` naming both protocols and the suffix, never the URL |
| `LLM_BASE_URL` ends with `/messages` and `LLM_PROTOCOL=anthropic` | `load_settings` raises `RuntimeError`: anthropic's own ending, but not a prefix of `/v1/messages`, so no strip yields a correct root |
| Stripping would eat the host (`https://v1` under `anthropic`) | `load_settings` raises `RuntimeError`; the strip is not allowed to break the authority |
| `LLM_BASE_URL` contains `?` or `#` | `load_settings` raises `RuntimeError`; a path suffix cannot be appended after a query string |
| `LLM_PROTOCOL` / `LLM_SCHEMA_MODE` unrecognised | `load_settings` raises `RuntimeError` listing the accepted values. **Never defaults** |
| `LLM_SCHEMA_MODE` is not `strict` | One WARNING per run, carrying the mode name only |
| Endpoint answers 401 / 403 / 404 | `ClassifierError`, category `endpoint_config`, `retryable=False`, message carries the status |
| `.dev.vars` exists beside `.env` | `.env` is ignored; remove or migrate `.dev.vars` |
| A real key appears in a tracked file | Security defect; remove and rotate the key |
| Production secret is absent | Deployment fails the required-secret check |

### 5. Good / Base / Bad Cases

- Good: copy `.env.example` to `.env`, replace every placeholder, start locally.
- Good: `LLM_PROTOCOL=anthropic` with `LLM_BASE_URL=https://api.anthropic.com`.
- Base: the provider's full endpoint URL pasted under the matching protocol —
  tolerated, stripped to the root, one WARNING. Not an error, but not silent either.
- Base: read-only API routes may start without LLM configuration, but scheduled sync
  must reject missing configuration.
- Base: an endpoint that rejects a strict schema is configured with
  `LLM_SCHEMA_MODE=json_object`, deliberately and visibly.
- Bad: put `LLM_API_KEY` in `wrangler.jsonc`, `.env.example`, logs, or committed docs.
- Bad: set `LLM_BASE_URL` to `http://...` or to a URL carrying a query string.
- Bad: `LLM_PROTOCOL=responses` with `LLM_BASE_URL=.../v1/chat/completions` — the
  protocol is what is wrong, and this is refused rather than quietly stripped.
- Bad: strip a suffix without logging it, or strip one belonging to another protocol.
- Bad: add a runtime probe, or retry a failed request under a weaker schema mode.
- Bad: drop `anthropic`'s tool-level `strict` flag after a 4xx — that is the same
  silent fallback, and it makes `strict` mean something different per protocol.

### 6. Tests Required

- `backend/tests/test_config.py` covers `load_settings` end to end: missing and
  blank values, both enums (defaults, every accepted value, every rejection), every
  base-URL shape rule, both directions of the suffix split (stripped vs refused), and
  that the degraded-mode and stripped-suffix WARNINGs each carry their bounded names
  and nothing else.
- Every failure must surface as `RuntimeError`, not `ValueError`: that is the type
  `run_sync` catches around `load_settings`, and anything else escapes a function
  that promises never to raise.
- No error message may echo a URL, model or key. The base-URL tests assert this
  directly, because a gateway base URL can carry a key in a query string.
- Repository checks assert `.env` remains ignored and `.env.example` remains tracked.
- A documentation-only placeholder change needs `git diff --check` and a search for
  stale `.dev.vars.example` references; it does not require the backend test suite.

### 7. Wrong vs Correct

Wrong:

```text
.dev.vars + .env
```

Correct:

```text
.env.example -> .env for local development
Wrangler secret + wrangler.jsonc vars for production
```

Wrong — a full endpoint path, and the `/v1` that anthropic's suffix already carries:

```text
LLM_PROTOCOL=anthropic
LLM_BASE_URL=https://api.anthropic.com/v1/messages
```

Correct:

```text
LLM_PROTOCOL=anthropic
LLM_BASE_URL=https://api.anthropic.com
```

Wrong — reacting to a capability at runtime:

```python
try:
    return await call(schema_mode=SchemaMode.STRICT)
except ClassifierError:
    return await call(schema_mode=SchemaMode.JSON_OBJECT)   # forbidden
```

Correct — the capability is declared, and the failure is a failure:

```text
LLM_SCHEMA_MODE=json_object      # in wrangler.jsonc vars, logged every run
```
