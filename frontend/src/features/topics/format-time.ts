import 'dayjs/locale/zh-cn';

import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

// ----------------------------------------------------------------------

/**
 * Topic timestamps arrive as ISO 8601 UTC strings. Recent items read better as
 * "3 小时前"; older ones need the absolute date, because "2 个月前" tells a
 * reader nothing about whether the content is stale.
 */
export function formatTopicTime(isoDate: string): string {
  const parsed = dayjs(isoDate);

  if (!parsed.isValid()) {
    return '时间未知';
  }

  const daysAgo = dayjs().diff(parsed, 'day');

  return daysAgo < 7 ? parsed.fromNow() : parsed.format('YYYY-MM-DD');
}
