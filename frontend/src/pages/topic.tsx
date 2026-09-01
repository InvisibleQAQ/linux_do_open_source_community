import { useParams } from 'src/routes/hooks';

import { CONFIG } from 'src/global-config';
import { TopicDetailView } from 'src/features/topics';

import { QueryEmptyState } from 'src/components/states';

// ----------------------------------------------------------------------

const metadata = { title: `主题详情 - ${CONFIG.appName}` };

export default function Page() {
  const { topicId } = useParams();

  return (
    <>
      <title>{metadata.title}</title>

      {topicId ? (
        <TopicDetailView topicId={topicId} />
      ) : (
        <QueryEmptyState title="缺少主题 ID" description="请从列表页进入主题详情。" />
      )}
    </>
  );
}
