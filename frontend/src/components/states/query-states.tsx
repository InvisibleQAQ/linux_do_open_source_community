import Box from '@mui/material/Box';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import AlertTitle from '@mui/material/AlertTitle';

import { EmptyContent } from 'src/components/empty-content';

// ----------------------------------------------------------------------

/**
 * The four states the PRD requires every data surface to express explicitly:
 * loading, empty, error and partial-data. They live here rather than inline in
 * each view so a screen cannot silently forget one of them.
 */

export type QueryErrorStateProps = {
  message: string;
  onRetry?: () => void;
};

export function QueryErrorState({ message, onRetry }: QueryErrorStateProps) {
  return (
    <Alert
      severity="error"
      variant="outlined"
      action={
        onRetry ? (
          <Button color="error" size="small" onClick={onRetry}>
            重试
          </Button>
        ) : undefined
      }
    >
      <AlertTitle>加载失败</AlertTitle>
      {message}
    </Alert>
  );
}

// ----------------------------------------------------------------------

export type QueryEmptyStateProps = {
  title?: string;
  description?: string;
};

export function QueryEmptyState({
  title = '还没有收录任何项目',
  description = '同步任务每 5 分钟运行一次，只收录原帖中明确出现 GitHub 仓库的主题。',
}: QueryEmptyStateProps) {
  return <EmptyContent title={title} description={description} sx={{ py: 10 }} />;
}

// ----------------------------------------------------------------------

/**
 * Shown while a background refetch runs on top of data already on screen. This
 * is the "partial data" state — the list stays interactive, the banner explains
 * why numbers may still move.
 */
export function QueryRefreshingBanner() {
  return (
    <Box sx={{ mb: 2 }}>
      <Alert severity="info" variant="outlined">
        正在获取最新内容，当前显示的是已缓存的数据。
      </Alert>
    </Box>
  );
}
