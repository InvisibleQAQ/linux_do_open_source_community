import Card from '@mui/material/Card';
import Stack from '@mui/material/Stack';
import Skeleton from '@mui/material/Skeleton';

// ----------------------------------------------------------------------

type Props = {
  count?: number;
};

/** First-load placeholder. Mirrors TopicCard's shape so the layout does not jump. */
export function TopicCardSkeleton({ count = 4 }: Props) {
  return (
    <Stack sx={{ gap: 3 }}>
      {Array.from({ length: count }, (_, index) => (
        <Card key={index} sx={{ p: 3 }}>
          <Skeleton variant="text" sx={{ width: '60%', fontSize: '1.25rem' }} />
          <Skeleton variant="text" sx={{ width: '30%' }} />

          <Stack sx={{ mt: 2, gap: 1 }}>
            <Skeleton variant="text" sx={{ width: '40%' }} />
            <Skeleton variant="text" sx={{ width: '90%' }} />
            <Skeleton variant="text" sx={{ width: '75%' }} />
          </Stack>
        </Card>
      ))}
    </Stack>
  );
}
