import type { TopicProject } from 'src/api';

import Box from '@mui/material/Box';
import Link from '@mui/material/Link';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { paths } from 'src/routes/paths';
import { RouterLink } from 'src/routes/components';

import { Iconify } from 'src/components/iconify';

// ----------------------------------------------------------------------

type Props = {
  project: TopicProject;
};

/**
 * One project inside a topic. Flat by design — the PRD forbids nesting a
 * decorative card inside the topic card, so this is a row, not a Card.
 *
 * Two outbound links per project, both required by the user stories: the
 * canonical GitHub repository, and the linux.do post that evidences it.
 */
export function ProjectRow({ project }: Props) {
  return (
    <Box
      sx={(theme) => ({
        py: 2,
        borderTop: `dashed 1px ${theme.vars.palette.divider}`,
      })}
    >
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        sx={{ gap: 1, alignItems: { sm: 'baseline' }, justifyContent: 'space-between' }}
      >
        <Link
          component={RouterLink}
          href={paths.project(project.owner, project.repo)}
          variant="subtitle2"
          sx={{ color: 'text.primary' }}
        >
          {project.display_name}
        </Link>

        <Typography variant="caption" sx={{ color: 'text.disabled', flexShrink: 0 }}>
          {project.owner}/{project.repo}
        </Typography>
      </Stack>

      <Typography variant="body2" sx={{ mt: 0.5, color: 'text.secondary' }}>
        {project.summary}
      </Typography>

      <Stack direction="row" sx={{ mt: 1, gap: 2, flexWrap: 'wrap' }}>
        <Link
          href={project.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          variant="caption"
          sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}
        >
          <Iconify icon="socials:github" width={14} />
          GitHub 仓库
        </Link>

        <Link
          href={project.post_url}
          target="_blank"
          rel="noopener noreferrer"
          variant="caption"
          sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, color: 'text.secondary' }}
        >
          <Iconify icon="eva:external-link-fill" width={14} />
          原帖 #{project.post_number}
        </Link>
      </Stack>
    </Box>
  );
}
