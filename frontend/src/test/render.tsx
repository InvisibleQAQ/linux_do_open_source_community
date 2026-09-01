import type { RenderOptions } from '@testing-library/react';

import { MemoryRouter } from 'react-router';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ThemeProvider } from 'src/theme';

import { defaultSettings, SettingsProvider } from 'src/components/settings';

// ----------------------------------------------------------------------

/**
 * A fresh QueryClient per render, with retries off and no cache retention.
 * Without this, a failing query would retry (slowing every error test) and
 * cached data would leak between tests.
 */
function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
}

export type RenderWithProvidersOptions = Omit<RenderOptions, 'wrapper'> & {
  route?: string;
};

export function renderWithProviders(
  ui: React.ReactNode,
  { route = '/', ...options }: RenderWithProvidersOptions = {}
) {
  const queryClient = createTestQueryClient();

  const result = render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}>
        <SettingsProvider defaultSettings={defaultSettings}>
          <ThemeProvider>{ui}</ThemeProvider>
        </SettingsProvider>
      </QueryClientProvider>
    </MemoryRouter>,
    options
  );

  return { ...result, queryClient };
}
