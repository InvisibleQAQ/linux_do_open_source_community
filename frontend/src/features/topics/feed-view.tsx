import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';
import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';

import { selectTopics, useTopicsFeed, toErrorMessage } from 'src/api';

import { QueryEmptyState, QueryErrorState, QueryRefreshingBanner } from 'src/components/states';

import { TopicCard } from './topic-card';
import { TopicCardSkeleton } from './topic-card-skeleton';

// ----------------------------------------------------------------------

/**
 * The app's first screen: the real feed, newest topic first. Not a landing page.
 *
 * All four required states are handled explicitly and in priority order:
 * first-load → hard error → empty → data (with a background-refresh banner).
 */
export function FeedView() {
  const query = useTopicsFeed();

  const topics = selectTopics(query.data?.pages);

  // `isPending` is true only when there is no data at all; a refetch over
  // existing data must NOT collapse the list back into skeletons.
  if (query.isPending) {
    return <TopicCardSkeleton />;
  }

  // An error with nothing cached is a dead end; an error with data on screen is
  // handled by the banner below instead of throwing the content away.
  if (query.isError && topics.length === 0) {
    return <QueryErrorState message={toErrorMessage(query.error)} onRetry={() => query.refetch()} />;
  }

  if (topics.length === 0) {
    return <QueryEmptyState />;
  }

  return (
    <Stack sx={{ gap: 3 }}>
      <Box>
        <Typography variant="h4">最新开源项目</Typography>
        <Typography variant="body2" sx={{ mt: 0.5, color: 'text.secondary' }}>
          按主题发布时间从新到旧排列，仅收录原帖中明确出现的 GitHub 仓库。
        </Typography>
      </Box>

      {query.isFetching && !query.isFetchingNextPage && <QueryRefreshingBanner />}

      {query.isError && (
        <QueryErrorState message={toErrorMessage(query.error)} onRetry={() => query.refetch()} />
      )}

      {topics.map((topic) => (
        <TopicCard key={topic.topic_id} topic={topic} />
      ))}

      {query.hasNextPage && (
        <Box sx={{ display: 'flex', justifyContent: 'center', pt: 1 }}>
          <Button
            variant="soft"
            loading={query.isFetchingNextPage}
            onClick={() => query.fetchNextPage()}
          >
            加载更多
          </Button>
        </Box>
      )}

      {!query.hasNextPage && (
        <Typography variant="caption" sx={{ textAlign: 'center', color: 'text.disabled' }}>
          已经到底了
        </Typography>
      )}
    </Stack>
  );
}
