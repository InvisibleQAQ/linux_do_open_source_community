import type { CursorPage, TopicDetail, TopicSummary, HealthStatus, ProjectDetail } from './types';

import { useQuery, useInfiniteQuery } from '@tanstack/react-query';

import { queryKeys } from './query-keys';
import { apiGet, ApiError } from './client';

// ----------------------------------------------------------------------

const DEFAULT_PAGE_SIZE = 20;

/**
 * The newest-first topic feed.
 *
 * Cursor pagination lives in `getNextPageParam`: the backend returns
 * `next_cursor: null` on the last page, and returning `undefined` here is what
 * flips `hasNextPage` to false. Do NOT derive "has more" from page length —
 * a full page can still be the last one.
 */
export function useTopicsFeed(limit: number = DEFAULT_PAGE_SIZE) {
  return useInfiniteQuery({
    queryKey: queryKeys.topics.feed(limit),
    queryFn: ({ pageParam, signal }) =>
      apiGet<CursorPage<TopicSummary>>('/api/topics', {
        params: { cursor: pageParam ?? undefined, limit },
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
}

/** Flattens the paged feed into one list for rendering. */
export function selectTopics(pages: CursorPage<TopicSummary>[] | undefined): TopicSummary[] {
  return pages?.flatMap((page) => page.items) ?? [];
}

// ----------------------------------------------------------------------

export function useTopic(topicId: number | string | undefined) {
  return useQuery({
    queryKey: queryKeys.topics.detail(topicId ?? ''),
    queryFn: ({ signal }) => apiGet<TopicDetail>(`/api/topics/${topicId}`, { signal }),
    enabled: topicId !== undefined && topicId !== '',
  });
}

export function useProject(owner: string | undefined, repo: string | undefined) {
  return useQuery({
    queryKey: queryKeys.projects.detail(owner ?? '', repo ?? ''),
    queryFn: ({ signal }) =>
      apiGet<ProjectDetail>(`/api/projects/${owner}/${repo}`, { signal }),
    enabled: Boolean(owner) && Boolean(repo),
  });
}

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => apiGet<HealthStatus>('/api/health', { signal }),
  });
}

// ----------------------------------------------------------------------

/** Narrows an unknown query error into the message the UI should show. */
export function toErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.isNotFound ? '没有找到这个内容。' : error.message;
  }
  return '发生了未知错误。';
}
