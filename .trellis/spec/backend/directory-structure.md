# Directory Structure

Layout is enumerated in `backend/CLAUDE.md`. This file is about the boundaries.

---

## Where a new file goes

| It is... | Put it in | May import |
|----------|-----------|------------|
| Pure logic over plain data (parsing, normalizing, validating, deciding) | `linuxdo_oss/domain/` | stdlib only |
| The shape of an LLM request or reply (one module per protocol) | `linuxdo_oss/llm/` | stdlib only |
| Something that talks to the network or the runtime | `linuxdo_oss/adapters/` | `workers`, `js`, domain |
| SQL, or anything touching D1 | `linuxdo_oss/persistence/` | domain, `d1.py` |
| An HTTP route or response model | `linuxdo_oss/api/` | persistence, domain |
| Cron orchestration | `linuxdo_oss/sync.py` | all of the above |

## The import rules, and why each exists

**`domain/` may import nothing but the standard library.**

Not a style preference. `workers` and `js` do not exist on CPython, so a single
runtime import anywhere in the domain's import graph makes the entire pure test
suite unimportable. The suite is 177 tests running in half a second; that speed is
what makes it get run.

The pattern for a runtime API needed inside otherwise-pure-looking code: import it
*inside the function*. `adapters/http.py::_abort_signal` does exactly that with
`from js import AbortSignal`, and returns None when the import fails.

**`llm/` is stdlib-only too, and additionally may not import `classifier`.**

Same foundation, one extra rule. `classifier.py` dispatches *into* `llm/`, so an
import back is a cycle; and `classifier.py` depends on pydantic, so letting `llm/`
touch it would add a module-level import to the startup snapshot for no reason.
That is why `ClassifierError` and every `CATEGORY_*` sit in `llm/errors.py` — all
three adapters raise them — with `classifier.py` re-exporting the lot so
`from linuxdo_oss.classifier import ClassifierError` still works.

`backend/tests/test_llm_purity.py` enforces this in a SUBPROCESS. Asserting
`"pydantic" not in sys.modules` inside pytest proves nothing, because
`test_classifier.py` imports pydantic into the same interpreter; only a fresh
interpreter makes the import graph observable.

**One module per LLM protocol, and dispatch happens once.**

Seven things differ per protocol (URL suffix, auth header, output-token field
name, system-prompt placement, schema wrapper, output location, failure spelling)
times three Schema Modes. As `if` chains that is a 3x3 matrix smeared across every
function — new conditionals instead of eliminated special cases. As modules behind
one interface it is three local decisions and a caller that branches zero times.

`llm/protocol.py::get_adapter` imports the selected module *inside the function*:
a deployment runs one protocol, and the Worker's 1 s startup budget should not pay
for the two that never execute. `llm/__init__.py` therefore must not re-export the
adapter modules.

**A `JsProxy` may not leave `persistence/d1.py`.**

Everything crosses that boundary through `.to_py()` and becomes a plain dict, then
a Pydantic model. Cloudflare's own `query-d1` example hands a JsProxy to a
serializer and is marked `xfail`; their FastAPI example that builds real dicts
passes its whole suite.

**SQL lives in a module constant, never inline in a route or orchestrator.**

`persistence/read_queries.py` and the constants at the top of `sync.py` are
importable by tests, which is how the SQL is verified against a real SQLite
database with the real migrations applied. Inline SQL cannot be tested without a
Worker.

**`config.py` reads `env` once and passes a frozen dataclass down.**

No module below it touches `env`. That is what lets the domain and persistence
layers be exercised without a runtime, and it means a missing configuration value
fails once, loudly, at the top — not as a confusing HTTP error deep in the
classifier.

## Naming

- Modules and functions: `snake_case`. Files named for the thing they own
  (`github_url.py`, `read_queries.py`), not for their layer.
- SQL constants: `SCREAMING_SNAKE_CASE` ending in `_SQL`.
- Private helpers: leading underscore. Anything without one is API — put it in
  `__all__`.

## Database column names

Plain `snake_case` only, and avoid identifiers that collide with JS `Object` /
`JsProxy` members: `order`, `keys`, `get`, `length`, `constructor`, `name`. Rows
arrive as JsProxy and are read by attribute before conversion; a colliding name
forces quoting in every statement and produces dirty dicts. Cloudflare's own
example had to write `"order"` quoted everywhere — do not inherit that tax.
