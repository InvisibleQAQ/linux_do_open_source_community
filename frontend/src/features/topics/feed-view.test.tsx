import { http, HttpResponse } from 'msw';
import { it, expect, describe } from 'vitest';
import userEvent from '@testing-library/user-event';
import { screen, waitFor } from '@testing-library/react';

import { server } from 'src/test/msw-server';
import { renderWithProviders } from 'src/test/render';
import { makeTopic, makeProject, makeTopicsPage } from 'src/test/fixtures';

import { FeedView } from './feed-view';

// ----------------------------------------------------------------------

const TOPICS_URL = '*/api/topics';

describe('FeedView', () => {
  it('groups projects under their topic and links to GitHub and the source post', async () => {
    server.use(
      http.get(TOPICS_URL, () =>
        HttpResponse.json(
          makeTopicsPage([
            makeTopic({
              title: '两个仓库的主题',
              projects: [
                makeProject({ display_name: '项目甲' }),
                makeProject({
                  canonical_url: 'https://github.com/acme/tool',
                  owner: 'acme',
                  repo: 'tool',
                  display_name: '项目乙',
                  post_number: 4,
                }),
              ],
            }),
          ])
        )
      )
    );

    renderWithProviders(<FeedView />);

    expect(await screen.findByText('两个仓库的主题')).toBeInTheDocument();

    // Both repositories in one topic must render — user story 7.
    expect(screen.getByText('项目甲')).toBeInTheDocument();
    expect(screen.getByText('项目乙')).toBeInTheDocument();
    expect(screen.getByText('2 个项目')).toBeInTheDocument();

    const githubLinks = screen.getAllByRole('link', { name: /GitHub 仓库/ });
    expect(githubLinks[0]).toHaveAttribute('href', 'https://github.com/octocat/hello-world');
    expect(githubLinks[0]).toHaveAttribute('rel', expect.stringContaining('noopener'));

    expect(screen.getByRole('link', { name: /原帖 #4/ })).toBeInTheDocument();
  });

  it('renders the empty state when the feed has no topics', async () => {
    server.use(http.get(TOPICS_URL, () => HttpResponse.json(makeTopicsPage([]))));

    renderWithProviders(<FeedView />);

    expect(await screen.findByText('还没有收录任何项目')).toBeInTheDocument();
  });

  it('renders a retryable error state when the request fails', async () => {
    server.use(http.get(TOPICS_URL, () => new HttpResponse(null, { status: 500 })));

    renderWithProviders(<FeedView />);

    expect(await screen.findByText('加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('advances the cursor when loading more, and stops when next_cursor is null', async () => {
    const firstTopic = makeTopic({ topic_id: 1, title: '第一页主题' });
    const secondTopic = makeTopic({ topic_id: 2, title: '第二页主题' });

    const seenCursors: (string | null)[] = [];

    server.use(
      http.get(TOPICS_URL, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor');
        seenCursors.push(cursor);

        return HttpResponse.json(
          cursor === null
            ? makeTopicsPage([firstTopic], 'cursor-2')
            : makeTopicsPage([secondTopic], null)
        );
      })
    );

    renderWithProviders(<FeedView />);

    expect(await screen.findByText('第一页主题')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '加载更多' }));

    expect(await screen.findByText('第二页主题')).toBeInTheDocument();

    // The second request must carry the cursor the first response handed back.
    expect(seenCursors).toEqual([null, 'cursor-2']);

    // `next_cursor: null` is the only end-of-list signal.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '加载更多' })).not.toBeInTheDocument();
    });
    expect(screen.getByText('已经到底了')).toBeInTheDocument();
  });
});
