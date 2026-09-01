import type { Breakpoint } from '@mui/material/styles';
import type { LayoutSectionProps } from '../core';

import Box from '@mui/material/Box';
import Link from '@mui/material/Link';
import Container from '@mui/material/Container';
import Typography from '@mui/material/Typography';

import { paths } from 'src/routes/paths';

import { CONFIG } from 'src/global-config';

import { Logo } from 'src/components/logo';

import { SettingsButton } from '../components';
import { MainSection, LayoutSection, HeaderSection } from '../core';

// ----------------------------------------------------------------------

export type FeedLayoutProps = Omit<LayoutSectionProps, 'children'> & {
  children: React.ReactNode;
  layoutQuery?: Breakpoint;
};

/**
 * The single layout of this app.
 *
 * Built directly on Minimal's `layouts/core` primitives rather than on its
 * `layouts/main` (a marketing shell with a mega-menu nav, mock data and a large
 * footer) or `layouts/dashboard` (assumes an authenticated user and a side nav).
 * This is a public read-only feed: one header, one content column, one footer.
 */
export function FeedLayout({ children, sx, layoutQuery = 'md', ...other }: FeedLayoutProps) {
  const renderHeader = () => (
    <HeaderSection
      layoutQuery={layoutQuery}
      slots={{
        leftArea: (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <Logo href={paths.feed} />
            <Typography
              variant="subtitle1"
              noWrap
              sx={{ display: { xs: 'none', sm: 'block' } }}
            >
              {CONFIG.appName}
            </Typography>
          </Box>
        ),
        rightArea: (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <SettingsButton />
          </Box>
        ),
      }}
    />
  );

  const renderFooter = () => (
    <Box component="footer" sx={{ py: 5, textAlign: 'center' }}>
      <Container>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          项目数据来自{' '}
          <Link
            href="https://linux.do"
            target="_blank"
            rel="noopener noreferrer"
            underline="always"
          >
            linux.do
          </Link>{' '}
          社区公开主题，由自动化流程整理，仅收录原帖中明确出现的 GitHub 仓库。
        </Typography>
      </Container>
    </Box>
  );

  return (
    <LayoutSection
      headerSection={renderHeader()}
      footerSection={renderFooter()}
      sx={sx}
      {...other}
    >
      <MainSection>
        <Container sx={{ py: { xs: 3, md: 5 } }}>{children}</Container>
      </MainSection>
    </LayoutSection>
  );
}
