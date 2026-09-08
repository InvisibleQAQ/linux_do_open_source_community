# Component Guidelines

---

## Shape

```tsx
import type { TopicSummary } from 'src/api';

import Card from '@mui/material/Card';

// ----------------------------------------------------------------------

type Props = {
  topic: TopicSummary;
};

/** One sentence on why this component exists, if it is not obvious. */
export function TopicCard({ topic }: Props) {
  return <Card sx={{ p: 3 }}>...</Card>;
}
```

- **Named exports** for components, except `pages/` which uses `export default function Page()` because `React.lazy` needs a default.
- `type Props = { ... }` declared above the component. Do not inline the prop type in the signature.
- One component per file, filename in kebab-case matching the component's role (`topic-card.tsx` → `TopicCard`).
- The `// ---...---` separator after imports is the Minimal convention; keep it so ported and new files look the same.

## Styling

- **`sx` only.** MUI v9 removed system props from Box, Grid, Stack, Typography, Link and DialogContentText.
- Reach for theme tokens, not literals: `sx={{ color: 'text.secondary' }}`, not `sx={{ color: '#637381' }}`.
- When you need a theme value in a callback, use the function form: `sx={(theme) => ({ borderTop: \`dashed 1px ${theme.vars.palette.divider}\` })}`. Note `theme.vars.*` — the theme runs with CSS variables enabled.
- Vertical stacks use `Stack`, not `Grid`. MUI v9's `Grid` no longer accepts `direction="column"`, and `Grid` columns are `size={{ xs: 12, md: 6 }}`, not `xs`/`md` props.
- Prefer composing MUI components over `styled()`. If you do need `styled()`, follow the ported files' pattern: define the styled root at the bottom of the file under a separator.

## Structure

- **Do not nest a Card inside a Card.** The PRD is explicit: a topic owns the card, projects are flat rows within it. `features/topics/project-row.tsx` is deliberately a `Box`, not a `Card`.
- Keep components presentational. Data comes in as props; the only components that call hooks from `src/api` are the top-level `*-view.tsx` files.
- A component that renders a list must handle the empty list itself rather than relying on the caller.

## Links

| Destination | Use |
|-------------|-----|
| Internal route | `<Link component={RouterLink} href={paths.topic(id)}>` — note `RouterLink` takes `href`, not `to` |
| External site | `<Link href={url} target="_blank" rel="noopener noreferrer">` |

Build internal URLs from `src/routes/paths.ts`. No string-concatenated routes.

## Icons

`<Iconify icon="socials:github" width={14} />`. The `icon` prop is typed to the 13 icons bundled in `src/components/iconify/icon-sets.ts`; an unbundled name is a TypeScript error. To add one, copy its entry from the Minimal template — see `frontend/CLAUDE.md`.

## Accessibility

- Icon-only buttons need `aria-label` (the ported `settings-button.tsx` shows the pattern).
- Do not put text meaning in color alone. `Label` carries text, not just a hue.
- Interactive elements must be real `Button` / `Link` / `IconButton`, never a clickable `Box`.

## Ported components

`components/{iconify,scrollbar,logo,label,empty-content,animate,progress-bar,snackbar,custom-breadcrumbs,settings}` and `layouts/core` are vendored from Minimal v7.7.0.

**Prefer configuring them over editing them.** The `SettingsDrawer` case is the model: its toggles are hidden by omitting keys from `defaultSettings`, using the template's own `hasKeys(defaultSettings, [...])` mechanism, with no change to the drawer. When an edit is genuinely unavoidable, comment what changed and record it in `frontend/CLAUDE.md`.
