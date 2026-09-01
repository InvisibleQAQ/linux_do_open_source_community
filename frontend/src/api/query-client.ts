import { QueryClient } from '@tanstack/react-query';

import { ApiError } from './client';

// ----------------------------------------------------------------------

/**
 * Retry only what is worth retrying. A 404 retried three times is three wasted
 * round trips and a three-times-slower error state for the user.
 */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && !error.isRetryable) {
    return false;
  }
  return failureCount < 2;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        // Content is refreshed by a 5-minute cron, so anything fresher than that
        // is a wasted request.
        staleTime: 60_000,
        refetchOnWindowFocus: false,
      },
    },
  });
}
