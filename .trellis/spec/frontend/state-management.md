# State Management

---

## Three kinds of state, three homes

| Kind | Home | Example |
|------|------|---------|
| Server state | TanStack Query, via `src/api/hooks.ts` | topic feed, topic detail, project detail |
| User preference | `SettingsProvider` (ported from Minimal) | color mode, primary color, font size |
| Local UI state | `useState` in the component | an expanded row, a dialog's open flag |

There is no global client store and no need for one. Do not add Redux/Zustand/Jotai.

## Server state rules

- **Only `src/api/hooks.ts` calls the backend.** A component containing `fetch` is a defect.
- **Every cache key comes from `src/api/query-keys.ts`.** Inline array keys drift and silently split the cache.
- **Retry is decided by error class, not by count alone.** `src/api/query-client.ts` retries only when `ApiError.isRetryable` (status 0 or ≥ 500). A retried 404 is three wasted round trips and a slower error state.
- **`staleTime` is 60 s** because content is produced by a 5-minute cron. Anything fresher is a wasted request.

## Cursor pagination

```ts
useInfiniteQuery({
  queryKey: queryKeys.topics.feed(limit),
  queryFn: ({ pageParam, signal }) => apiGet(..., { params: { cursor: pageParam ?? undefined }, signal }),
  initialPageParam: null as string | null,
  getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
})
```

- `next_cursor === null` → return `undefined` → `hasNextPage` becomes false. **This is the only end-of-list signal.**
- Never infer "no more pages" from `items.length < limit`. A full page can be the last page.
- Drive the load-more control off `hasNextPage` and `isFetchingNextPage`, never off `isFetching` (which is also true during a background refresh of page 1).

## The four states, in priority order

Every data surface renders them in exactly this order. `src/features/topics/feed-view.tsx` is the reference implementation.

```
1. isPending                          -> skeleton        (no data has ever arrived)
2. isError && items.length === 0      -> error + retry   (dead end)
3. items.length === 0                 -> empty state
4. otherwise                          -> data
     + isFetching && !isFetchingNextPage -> refreshing banner   (partial data)
     + isError                           -> inline error, keep the list
```

The distinction that matters: **`isPending` is not `isFetching`.** Collapsing a populated list back into skeletons on every background refetch is a bug, not a loading state.

Never render an error by throwing the existing data away when data is on screen. Show it alongside.

## Request cancellation

`queryFn` receives `signal`; pass it to `apiGet`. `apiGet` combines it with a 15 s internal timeout via `AbortSignal.any`. Unmounting a component therefore aborts its in-flight request.

## Testing

`src/test/render.tsx` builds a fresh `QueryClient({ retry: false, gcTime: 0, staleTime: 0 })` per render. Do not share a client between tests — cached data leaks and retries make error tests slow and flaky.

`src/test/msw-server.ts` registers **no** default handlers and runs with `onUnhandledRequest: 'error'`. A test that forgets to declare the backend response fails loudly instead of hitting the network.
