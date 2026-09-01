import '@testing-library/jest-dom/vitest';

import { afterAll, afterEach, beforeAll } from 'vitest';

import { server } from './msw-server';

// ----------------------------------------------------------------------

// MSW intercepts at the network layer, so tests exercise the real `apiGet`
// client — including its error mapping — instead of a hand-stubbed fetch.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));

afterEach(() => server.resetHandlers());

afterAll(() => server.close());
