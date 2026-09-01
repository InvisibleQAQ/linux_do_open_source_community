import { CONFIG } from 'src/global-config';
import { FeedView } from 'src/features/topics';

// ----------------------------------------------------------------------

const metadata = { title: `最新开源项目 - ${CONFIG.appName}` };

export default function Page() {
  return (
    <>
      <title>{metadata.title}</title>

      <FeedView />
    </>
  );
}
