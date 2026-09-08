# Directory Structure

How frontend code is organized. Read before adding a file.

---

## Layout

```
frontend/src/
├── api/                  # the ONLY egress to the backend
│   ├── types.ts          # wire contract (snake_case, mirrors Pydantic)
│   ├── client.ts         # apiGet + ApiError
│   ├── hooks.ts          # TanStack Query hooks
│   ├── query-keys.ts     # every cache key
│   └── query-client.ts   # retry policy, staleTime
├── features/             # business UI, grouped by domain concept
│   ├── topics/
│   └── projects/
├── components/           # cross-feature presentational pieces
│   ├── states/           # OURS: the four required states
│   └── {iconify,scrollbar,logo,label,empty-content,animate,
│         progress-bar,snackbar,custom-breadcrumbs,settings}/   # ported from Minimal
├── layouts/
│   ├── core/             # ported primitives: LayoutSection/HeaderSection/MainSection
│   ├── components/       # ported: settings-button
│   └── feed/             # OURS: the app's single layout
├── theme/                # ported design system — read frontend/CLAUDE.md before editing
├── pages/                # route shells only
├── routes/
│   ├── paths.ts          # OURS
│   ├── components/       # ported: RouterLink, ErrorBoundary
│   ├── hooks/            # ported: useParams/usePathname/useRouter/useSearchParams
│   └── sections/         # route table
├── test/                 # render helper, msw server, fixtures
├── app.tsx               # provider stack
├── main.tsx              # router mount
├── global-config.ts      # CONFIG
└── global.css
```

## Where does a new file go?

| It is... | Put it in |
|----------|-----------|
| A call to the backend | `api/hooks.ts` (and `api/types.ts` if the shape is new). Nowhere else. |
| UI specific to topics or projects | `features/<domain>/` |
| UI reused by more than one feature | `components/` |
| A route target | `pages/` — a shell only: `metadata` + `<title>` + one view component |
| Layout chrome (header, footer, nav) | `layouts/feed/` |
| A design-system change | `theme/` — but read the 5 surgery notes in `frontend/CLAUDE.md` first |

## Rules

- **`pages/` holds no logic.** A page is `const metadata = { title }` plus `<><title>{metadata.title}</title><SomeView /></>`. All rendering lives in `features/`.
- **`features/` must not import from another `features/` sibling's internals** — go through its `index.ts`. `features/projects/project-detail-view.tsx` importing `src/features/topics/format-time` is the one existing exception; if a second appears, promote the module to `src/utils/`.
- **Ported files are vendored code.** Prefer configuring them over editing them (the `SettingsDrawer` visibility trick in `frontend/CLAUDE.md` is the model). When an edit is unavoidable, add a comment saying what changed and why, and record it in `frontend/CLAUDE.md`.
- **Barrel files (`index.ts`) export values first, then types**, sorted by line length ascending — `eslint-plugin-perfectionist` enforces it. Run `pnpm lint:fix`, don't hand-sort.

## Import group order

Enforced as an **error** by `perfectionist/sort-imports`. Groups, in order, blank line between each, line-length ascending within each:

1. style / side-effect (`import 'dayjs/locale/zh-cn'`)
2. types
3. builtin + external (`react`, `msw`, `dayjs`)
4. `@mui/*`
5. `src/routes/*`
6. `src/hooks/*`, `src/utils/*`
7. **internal** — any other `src/*`, including `src/api` and **`src/features/*`**
8. `src/components/*`
9. relative (`./`, `../`)

The common mistake: putting `src/features/*` after `src/components/*`. It belongs in group 7, before components.
