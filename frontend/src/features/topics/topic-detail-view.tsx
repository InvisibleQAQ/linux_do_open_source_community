import Box from '@mui/material/Box';
import Card from '@mui/material/Card';
import Link from '@mui/material/Link';
import Stack from '@mui/material/Stack';
import Divider from '@mui/material/Divider';
import Typography from '@mui/material/Typography';

import { paths } from 'src/routes/paths';

import { useTopic, toErrorMessage } from 'src/api';

import { Iconify } from 'src/components/iconify';
import { CustomBreadcrumbs } from 'src/components/custom-breadcrumbs';
import { QueryEmptyState, QueryErrorState } from 'src/components/states';

import { ProjectRow } from './project-row';
import { formatTopicTime } from './format-time';
import { TopicCardSkeleton } from './topic-card-skeleton';

// ----------------------------------------------------------------------

type Props = {
  topicId: string;
};

export function TopicDetailView({ topicId }: Props) {
  const query = useTopic(topicId);

  if (query.isPending) {
    return <TopicCardSkeleton count={1} />;
  }

  if (query.isError) {
    return <QueryErrorState message={toErrorMessage(query.error)} onRetry={() => query.refetch()} />;
  }

  const topic = query.data;

  if (!topic) {
    return <QueryEmptyState title="主题不存在" description="它可能还没有被同步，或者不含开源项目。" />;
  }

  return (
    <Stack sx={{ gap: 3 }}>
      <CustomBreadcrumbs
        heading={topic.title}
        links={[{ name: '最新', href: paths.feed }, { name: `#${topic.topic_id}` }]}
      />

      <Stack direction="row" sx={{ gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
        <Typography variant="caption" sx={{ color: 'text.disabled' }}>
          {formatTopicTime(topic.published_at)}
        </Typography>

        {topic.author && (
          <Typography variant="caption" sx={{ color: 'text.disabled' }}>
            @{topic.author}
          </Typography>
        )}

        <Link
          href={topic.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          variant="caption"
          sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}
        >
          <Iconify icon="eva:external-link-fill" width={14} />
          Linux.do 原帖
        </Link>
      </Stack>

      <Card sx={{ p: 3 }}>
        <Typography variant="h6">收录的项目</Typography>

        {topic.projects.length === 0 ? (
          <Typography variant="body2" sx={{ mt: 1, color: 'text.secondary' }}>
            这个主题下没有已发布的项目。
          </Typography>
        ) : (
          <Stack sx={{ mt: 1 }}>
            {topic.projects.map((project) => (
              <ProjectRow
                key={`${project.canonical_url}#${project.post_number}`}
                project={project}
              />
            ))}
          </Stack>
        )}
      </Card>

      <Card sx={{ p: 3 }}>
        <Typography variant="h6">来源帖子</Typography>

        <Stack sx={{ mt: 2, gap: 2 }}>
          {topic.posts.map((post, index) => (
            <Box key={post.post_number}>
              {index > 0 && <Divider sx={{ mb: 2, borderStyle: 'dashed' }} />}

              <Stack direction="row" sx={{ gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                <Typography variant="subtitle2">
                  #{post.post_number}
                  {post.is_first_post && ' · 首帖'}
                </Typography>

                {post.author && (
                  <Typography variant="caption" sx={{ color: 'text.disabled' }}>
                    @{post.author}
                  </Typography>
                )}

                <Link
                  href={post.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  variant="caption"
                >
                  查看原文
                </Link>
              </Stack>

              {/*
                `post.text` is plain text produced by the backend's HTML-to-text
                conversion. It is rendered as text, never as HTML — the PRD
                forbids injecting RSS HTML into the DOM.
              */}
              <Typography
                variant="body2"
                sx={{ mt: 1, color: 'text.secondary', whiteSpace: 'pre-wrap' }}
              >
                {post.text}
              </Typography>
            </Box>
          ))}
        </Stack>
      </Card>
    </Stack>
  );
}
