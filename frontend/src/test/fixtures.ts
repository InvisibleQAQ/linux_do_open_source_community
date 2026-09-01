import type { CursorPage, TopicProject, TopicSummary } from 'src/api';

// ----------------------------------------------------------------------

export function makeProject(overrides: Partial<TopicProject> = {}): TopicProject {
  return {
    canonical_url: 'https://github.com/octocat/hello-world',
    owner: 'octocat',
    repo: 'hello-world',
    display_name: 'Hello World',
    summary: '一个用于演示的示例仓库。',
    evidence_url: 'https://github.com/octocat/hello-world/issues/12',
    post_url: 'https://linux.do/t/topic/2837720/1',
    post_number: 1,
    ...overrides,
  };
}

export function makeTopic(overrides: Partial<TopicSummary> = {}): TopicSummary {
  return {
    topic_id: 2837720,
    canonical_url: 'https://linux.do/t/topic/2837720',
    title: '分享一个好用的开源工具',
    author: 'someone',
    published_at: '2026-08-31T15:18:11.000Z',
    projects: [makeProject()],
    ...overrides,
  };
}

export function makeTopicsPage(
  items: TopicSummary[] = [makeTopic()],
  nextCursor: string | null = null
): CursorPage<TopicSummary> {
  return { items, next_cursor: nextCursor };
}
