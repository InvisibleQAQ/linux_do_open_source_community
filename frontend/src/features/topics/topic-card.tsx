import type { TopicSummary } from 'src/api';

import Card from '@mui/material/Card';
import Link from '@mui/material/Link';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { paths } from 'src/routes/paths';
import { RouterLink } from 'src/routes/components';

import { Label } from 'src/components/label';
import { Iconify } from 'src/components/iconify';

import { ProjectRow } from './project-row';
import { formatTopicTime } from './format-time';

// ----------------------------------------------------------------------

type Props = {
  topic: TopicSummary;
};

/**
 * A topic and every project found inside it. The topic is the primary grouping
 * required by the PRD, so it owns the card; projects are flat rows within it.
 */
export function TopicCard({ topic }: Props) {
  return (
    <Card sx={{ p: 3 }}>
      <Stack sx={{ gap: 1 }}>
        <Link
          component={RouterLink}
          href={paths.topic(topic.topic_id)}
          variant="h6"
          sx={{ color: 'text.primary' }}
        >
          {topic.title}
        </Link>

        <Stack
          direction="row"
          sx={{ gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}
        >
          <Typography variant="caption" sx={{ color: 'text.disabled' }}>
            {formatTopicTime(topic.published_at)}
          </Typography>

          {topic.author && (
            <Typography variant="caption" sx={{ color: 'text.disabled' }}>
              @{topic.author}
            </Typography>
          )}

          <Label color="info" variant="soft">
            {topic.projects.length} 个项目
          </Label>

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
      </Stack>

      <Stack sx={{ mt: 2 }}>
        {topic.projects.map((project) => (
          <ProjectRow key={`${project.canonical_url}#${project.post_number}`} project={project} />
        ))}
      </Stack>
    </Card>
  );
}
