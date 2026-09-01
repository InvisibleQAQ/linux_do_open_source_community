import Button from '@mui/material/Button';

import { paths } from 'src/routes/paths';
import { RouterLink } from 'src/routes/components';

import { CONFIG } from 'src/global-config';

import { EmptyContent } from 'src/components/empty-content';

// ----------------------------------------------------------------------

const metadata = { title: `页面不存在 - ${CONFIG.appName}` };

export default function Page() {
  return (
    <>
      <title>{metadata.title}</title>

      <EmptyContent
        title="页面不存在"
        description="这个地址没有对应的内容。"
        sx={{ py: 10 }}
        action={
          <Button component={RouterLink} href={paths.feed} variant="contained" sx={{ mt: 3 }}>
            返回首页
          </Button>
        }
      />
    </>
  );
}
