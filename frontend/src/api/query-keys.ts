// ----------------------------------------------------------------------

/**
 * Every cache key in one place. Hierarchical so `queryClient` can invalidate a
 * whole subtree with a prefix, and so no two hooks can drift apart on key shape.
 */
export const queryKeys = {
  topics: {
    all: ['topics'] as const,
    feed: (limit: number) => ['topics', 'feed', limit] as const,
    detail: (topicId: number | string) => ['topics', 'detail', String(topicId)] as const,
  },
  projects: {
    all: ['projects'] as const,
    detail: (owner: string, repo: string) => ['projects', 'detail', owner, repo] as const,
  },
  health: ['health'] as const,
};
