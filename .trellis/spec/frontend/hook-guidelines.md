# Hook Guidelines

---

## Data-fetching hooks

All of them live in `src/api/hooks.ts`. There are no per-feature fetching hooks.

Rules for adding one:

1. Add the response type to `src/api/types.ts` first, and update `.trellis/spec/frontend/api-contract.md`.
2. Add its key to `src/api/query-keys.ts`.
3. Pass `signal` from the query function through to `apiGet` so unmount cancels the request.
4. For a hook whose argument may be undefined (a route param that has not resolved), gate it with `enabled`, do not early-return — a conditional hook call breaks the hooks rule:

```ts
export function useTopic(topicId: number | string | undefined) {
  return useQuery({
    queryKey: queryKeys.topics.detail(topicId ?? ''),
    queryFn: ({ signal }) => apiGet<TopicDetail>(`/api/topics/${topicId}`, { signal }),
    enabled: topicId !== undefined && topicId !== '',
  });
}
```

5. Return the query object as-is. Do not destructure it into a custom shape — callers need `isPending`, `isError`, `isFetching`, `refetch` and, for infinite queries, `hasNextPage` / `fetchNextPage` / `isFetchingNextPage`. Wrapping hides exactly the fields the four-state rule depends on.

Data shaping goes in a plain exported function, not inside the hook: `selectTopics(pages)` flattens the paged feed and is trivially unit-testable without React.

## Router hooks

Use the ported wrappers in `src/routes/hooks/` (`useParams`, `usePathname`, `useRouter`, `useSearchParams`) rather than importing from `react-router` directly. They are the seam that keeps a future React Router major (v8 moves `RouterProvider` to `react-router/dom`) from touching every call site.

Route params are always `string | undefined`. Validate before use; `src/pages/topic.tsx` shows the pattern — render an explicit empty state rather than passing `undefined` down.

## Ported utility hooks

`minimal-shared/hooks` provides `useBoolean`, `useDebounce`, `useCopyToClipboard` and similar. Prefer them over hand-rolling; they are already a dependency.

## Rules of hooks

- No conditional or looped hook calls. Gate with `enabled`, or split the component.
- Every `useEffect` needs a complete dependency array — `eslint-plugin-react-hooks` is on and treats this as an error.
- Do not use `useEffect` to derive state from props. Compute it during render.
- `useMemo` is for real cost or referential stability, not decoration. The one in `src/app.tsx` exists so each `App` mount gets its own `QueryClient` — that is referential stability with a purpose, and it is commented as such.
