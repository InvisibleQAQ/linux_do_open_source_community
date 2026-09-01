import Card from '@mui/material/Card';
import Link from '@mui/material/Link';
import Stack from '@mui/material/Stack';
import Divider from '@mui/material/Divider';
import Typography from '@mui/material/Typography';

import { paths } from 'src/routes/paths';
import { RouterLink } from 'src/routes/components';

import { useProject, toErrorMessage } from 'src/api';
import { TopicCardSkeleton } from 'src/features/topics';
import { formatTopicTime } from 'src/features/topics/format-time';

import { Iconify } from 'src/components/iconify';
import { CustomBreadcrumbs } from 'src/components/custom-breadcrumbs';
import { QueryEmptyState, QueryErrorState } from 'src/components/states';

// ----------------------------------------------------------------------

type Props = {
  owner: string;
  repo: string;
};

/**
 * One globally deduplicated project and every topic that mentioned it.
 *
 * This view is why projects are stored once and mentions many times: the same
 * repository discussed in five topics shows five provenance entries, not five
 * conflicting project records.
 */
export function ProjectDetailView({ owner, repo }: Props) {
  const query = useProject(owner, repo);

  if (query.isPending) {
    return <TopicCardSkeleton count={1} />;
  }

  if (query.isError) {
    return <QueryErrorState message={toErrorMessage(query.error)} onRetry={() => query.refetch()} />;
  }

  const project = query.data;

  if (!project) {
    return <QueryEmptyState title="项目不存在" description="这个仓库还没有被任何主题收录。" />;
  }

  return (
    <Stack sx={{ gap: 3 }}>
      <CustomBreadcrumbs
        heading={project.display_name}
        links={[{ name: '最新', href: paths.feed }, { name: `${project.owner}/${project.repo}` }]}
      />

      <Card sx={{ p: 3 }}>
        <Typography variant="body1">{project.summary}</Typography>

        <Link
          href={project.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          variant="subtitle2"
          sx={{ mt: 2, display: 'inline-flex', alignItems: 'center', gap: 0.5 }}
        >
          <Iconify icon="socials:github" width={18} />
          {project.canonical_url}
        </Link>
      </Card>

      <Card sx={{ p: 3 }}>
        <Typography variant="h6">出现在这些主题中</Typography>

        <Stack sx={{ mt: 2, gap: 2 }}>
          {project.sources.map((source, index) => (
            <Stack key={`${source.topic_id}-${source.post_url}`} sx={{ gap: 0.5 }}>
              {index > 0 && <Divider sx={{ mb: 1.5, borderStyle: 'dashed' }} />}

              <Link
                component={RouterLink}
                href={paths.topic(source.topic_id)}
                variant="subtitle2"
                sx={{ color: 'text.primary' }}
              >
                {source.topic_title}
              </Link>

              <Stack direction="row" sx={{ gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
                <Typography variant="caption" sx={{ color: 'text.disabled' }}>
                  {formatTopicTime(source.topic_published_at)}
                </Typography>

                <Link
                  href={source.post_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  variant="caption"
                >
                  原帖
                </Link>

                <Typography variant="caption" sx={{ color: 'text.disabled' }}>
                  证据链接：{source.evidence_url}
                </Typography>
              </Stack>
            </Stack>
          ))}
        </Stack>
      </Card>
    </Stack>
  );
}
