# Frontend Development Guidelines

React SPA for the Linux.do 开源项目聚合器. Public, read-only, Chinese-only, no auth.

Read `frontend/CLAUDE.md` first — it explains the Minimal v7.7.0 port and the five surgeries applied to the vendored theme. Nothing here makes sense without it.

---

## Guidelines Index

| Guide | Covers |
|-------|--------|
| [Directory Structure](./directory-structure.md) | Where a new file goes; the enforced import group order |
| [API Contract](./api-contract.md) | The frontend/backend wire shape and its rules |
| [State Management](./state-management.md) | TanStack Query, cursor pagination, the four required states |
| [Component Guidelines](./component-guidelines.md) | Component shape, `sx`-only styling, links, icons |
| [Hook Guidelines](./hook-guidelines.md) | Where fetching hooks live, `enabled` over early return |
| [Type Safety](./type-safety.md) | Compiler settings and the two load-bearing types |
| [Quality Guidelines](./quality-guidelines.md) | `pnpm check`, the forbidden list, testing bar, bundle budget |

Cross-cutting decisions live in `docs/adr/`:

- `0001` frontend dependency pins (why TS 5.9 and ESLint 9)
- `0002` single-Worker topology (why no `@cloudflare/vite-plugin`, why no CORS)
- `0003` data layer (why TanStack Query over the template's SWR)
- `0004` Minimal template whitelist port

---

## The five things most likely to bite you

1. **`src/features/*` sorts before `src/components/*`** in the import order. Run `pnpm lint:fix`.
2. **`isPending` is not `isFetching`.** Confusing them makes a populated list flash back to skeletons.
3. **`next_cursor === null` is the only end-of-list signal.** Not `items.length < limit`.
4. **MUI v9 has no system props.** `<Box mt={2}>` does not compile.
5. **Four dependencies are pinned back on purpose.** Read ADR 0001 before upgrading anything.

---

**Language**: guideline documents are written in Chinese or English as suits the reader; code comments are English.
