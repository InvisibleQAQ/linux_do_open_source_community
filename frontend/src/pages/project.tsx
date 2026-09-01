import { useParams } from 'src/routes/hooks';

import { CONFIG } from 'src/global-config';
import { ProjectDetailView } from 'src/features/projects';

import { QueryEmptyState } from 'src/components/states';

// ----------------------------------------------------------------------

const metadata = { title: `项目详情 - ${CONFIG.appName}` };

export default function Page() {
  const { owner, repo } = useParams();

  return (
    <>
      <title>{metadata.title}</title>

      {owner && repo ? (
        <ProjectDetailView owner={owner} repo={repo} />
      ) : (
        <QueryEmptyState title="缺少仓库参数" description="请从列表页进入项目详情。" />
      )}
    </>
  );
}
