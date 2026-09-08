# Quality Guidelines

---

## Before you commit

```bash
cd frontend && pnpm check      # tsc --noEmit && eslint && vitest run
```

All three must be clean. Baseline at skeleton completion (2026-08-31): `tsc` 0 errors, `eslint` 0 problems, `vitest` 4/4.

`pnpm build` does **not** run tsc or eslint (`vite-plugin-checker` has `enableBuild: false`) — the checker's eslint pass consumed 91% of a 63 s build. Type and lint safety comes from `pnpm check` and CI, not from the build.

---

## Forbidden

| Pattern | Why |
|---------|-----|
| `fetch(...)` outside `src/api/` | The API client owns timeouts, error mapping and cancellation. A bare fetch bypasses all three. |
| `dangerouslySetInnerHTML` | RSS-derived text reaches the frontend as plain text. Injecting it as HTML reintroduces the XSS the backend's HTML-to-text conversion exists to prevent. |
| MUI system props — `<Box mt={2}>`, `<Typography color="primary.main">` | Removed in MUI v9. Use `sx={{ ... }}`. |
| `<a href>` to an external site without `target="_blank" rel="noopener noreferrer"` | PRD security requirement. |
| An inline array as a query key | Keys must come from `src/api/query-keys.ts` or the cache silently splits. |
| Inferring "no more pages" from `items.length < limit` | Only `next_cursor === null` ends the list. |
| Collapsing populated data into a skeleton on refetch | Use `isPending`, not `isFetching`. |
| A new global state library | See `.trellis/spec/frontend/state-management.md`. |
| Hand-sorting imports | `pnpm lint:fix`. The ordering is machine-enforced. |
| Adding a dependency to match "latest" | Four pins are held back by real peer constraints. Read `docs/adr/0001-frontend-dependency-pins.md` first. |

---

## Testing bar

- Tests assert **externally observable behavior** — rendered text, link `href`s, which requests were sent — never internal function calls. This mirrors the PRD's testing decisions.
- Use MSW to define backend responses; never stub `fetch` by hand. The real `apiGet` and its error mapping should be exercised.
- Every data surface needs coverage of the four states. `src/features/topics/feed-view.test.tsx` is the reference: data + grouping, empty, error-with-retry, and cursor advance/end-of-list.
- When a test needs a payload, build it with `src/test/fixtures.ts` factories and override only the field under test. Hand-written literals drift from the contract.

---

## Comments

Comment the **why**, never the what. The load-bearing comments in this codebase explain non-obvious decisions:

- why `@mui/x-date-pickers` is a type-only devDependency (`src/theme/extend-theme-types.d.ts`)
- why `next_cursor` is the only end-of-list signal (`src/api/hooks.ts`)
- why `ApiError` carries `status` (`src/api/client.ts`)
- why the QueryClient is built in a `useMemo` (`src/app.tsx`)

If a reviewer would ask "why is it done this way?", answer it in place.

---

## Bundle budget

At skeleton completion: main chunk 705 kB (gzip 220 kB); `empty-content-*.js` 224 kB is `MotionLazy`'s lazily loaded framer-motion feature bundle, off the first-paint critical path.

If the main chunk grows past ~800 kB raw, investigate before adding more. MUI dominates; the next largest removable item is `framer-motion` (`components/animate` and `layouts/components/settings-button.tsx` depend on it).

Never re-add the full 224-icon set to `src/components/iconify/icon-sets.ts`. Copy individual entries.
