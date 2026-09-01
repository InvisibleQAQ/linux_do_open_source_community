// ----------------------------------------------------------------------

export const paths = {
  feed: '/',
  topic: (topicId: number | string) => `/topics/${topicId}`,
  project: (owner: string, repo: string) => `/projects/${owner}/${repo}`,
  notFound: '/404',
};

// ----------------------------------------------------------------------

/** External destinations. Always rendered with `rel="noopener noreferrer"`. */
export const externalPaths = {
  linuxDoTopic: (topicId: number) => `https://linux.do/t/topic/${topicId}`,
  github: (owner: string, repo: string) => `https://github.com/${owner}/${repo}`,
};
