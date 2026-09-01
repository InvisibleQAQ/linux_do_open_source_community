import { setupServer } from 'msw/node';

// ----------------------------------------------------------------------

/**
 * No default handlers on purpose: `onUnhandledRequest: 'error'` plus an empty
 * base set means a test that forgets to declare what the backend returns fails
 * loudly instead of silently hitting the network.
 */
export const server = setupServer();
