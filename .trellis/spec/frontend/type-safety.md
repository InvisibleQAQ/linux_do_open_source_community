# Type Safety

---

## Compiler settings

`frontend/tsconfig.json` runs `strict: true` + `strictNullChecks` + `noFallthroughCasesInSwitch`.

`noUncheckedIndexedAccess` and `verbatimModuleSyntax` were **tried and rejected**: both produce errors inside the 153 vendored Minimal files, which we do not want to fork. Our own code is held to a higher bar by ESLint instead. The tsconfig carries a comment saying so — do not "fix" it by turning them on without dealing with the vendored tree.

TypeScript is pinned to `~5.9.3` because `typescript-eslint@8`'s peer range is `typescript >=4.8.4 <6.1.0`. See `docs/adr/0001-frontend-dependency-pins.md`.

## Rules

- **No `any`.** Use `unknown` at a boundary and narrow. `toErrorMessage(error: unknown)` in `src/api/hooks.ts` is the pattern: accept `unknown`, narrow with `instanceof`, return something the UI can render.
- **No non-null assertions (`!`) on values that can genuinely be absent.** `src/main.tsx` throws an explicit error when `#root` is missing instead of asserting it away.
- **No type assertions to silence the compiler.** `as` is acceptable only where the compiler cannot know a fact it has no access to — casting a JSON response to its declared wire type in `apiGet`, for example, which is the one deliberate trust boundary.
- **Prefer `type` over `interface`** for object shapes, matching the ported code. Reserve `interface` for declaration merging (the theme augmentations in `src/theme/extend-theme-types.d.ts` require it).
- **`import type` for type-only imports.** The ported code does this consistently and the import-ordering rule places type imports in their own group.

## Wire types

`src/api/types.ts` is the hand-maintained mirror of the backend's Pydantic models.

- Field names stay snake_case. No conversion layer, no second source of truth.
- The cast in `apiGet` is unvalidated by design: the backend owns validation via Pydantic, and adding a runtime schema on the client would duplicate it. The trade-off and the generated alternative are recorded in `docs/adr/0003-data-layer-tanstack-query.md`.
- If a field can be absent, type it `| null` explicitly — the backend emits `null`, not a missing key.

## Types that catch real mistakes

Two places where a type is doing load-bearing work. Preserve both.

**`IconifyName`** (`src/components/iconify/register-icons.ts`) is derived from the keys of the bundled icon set, so `<Iconify icon="not:bundled" />` is a compile error rather than a silent runtime CDN fetch. This caught a dynamically chosen icon pair in `settings/drawer/fullscreen-button.tsx` during the icon trim.

**Optional settings fields** (`src/components/settings/types.ts`) — `direction`, `navLayout`, `navColor`, `fontFamily` are optional so that omitting them from `defaultSettings` both hides the corresponding drawer toggle and is type-checked. Consumers in `src/theme/with-settings/update-core.ts` supply explicit defaults so `undefined` never reaches `createTheme`.

## Generic helpers

Keep them minimal and inferable. `CursorPage<T>` is the only generic in the API layer, and `apiGet<T>` takes its type from the call site. Do not add generic wrappers whose type parameters callers must spell out by hand.
