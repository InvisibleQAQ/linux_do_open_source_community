import type { RouteObject } from 'react-router';

import { lazy, Suspense } from 'react';

import { FeedLayout } from 'src/layouts/feed';
import { TopicCardSkeleton } from 'src/features/topics';

// ----------------------------------------------------------------------

const FeedPage = lazy(() => import('src/pages/feed'));
const TopicPage = lazy(() => import('src/pages/topic'));
const ProjectPage = lazy(() => import('src/pages/project'));
const NotFoundPage = lazy(() => import('src/pages/not-found'));

/**
 * Every route shares one layout and one Suspense boundary. The fallback is the
 * feed skeleton rather than a full-screen splash so route changes do not blank
 * the page — the header and footer stay put.
 */
function withLayout(page: React.ReactNode) {
  return (
    <FeedLayout>
      <Suspense fallback={<TopicCardSkeleton count={3} />}>{page}</Suspense>
    </FeedLayout>
  );
}

export const routesSection: RouteObject[] = [
  { index: true, element: withLayout(<FeedPage />) },
  { path: 'topics/:topicId', element: withLayout(<TopicPage />) },
  { path: 'projects/:owner/:repo', element: withLayout(<ProjectPage />) },
  { path: '*', element: withLayout(<NotFoundPage />) },
];
