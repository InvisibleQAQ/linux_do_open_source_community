import { CONFIG } from 'src/global-config';

// ----------------------------------------------------------------------

/**
 * Thrown for any non-2xx response or transport failure. Carries `status` so the
 * UI can tell "not found" apart from "backend is down" — the PRD requires
 * distinguishable error states, and a bare `Error` cannot express that.
 *
 * `status` is 0 when the request never got a response (network error, timeout).
 */
export class ApiError extends Error {
  readonly status: number;

  readonly url: string;

  constructor(message: string, options: { status: number; url: string; cause?: unknown }) {
    super(message, { cause: options.cause });
    this.name = 'ApiError';
    this.status = options.status;
    this.url = options.url;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** Transport failures and 5xx are worth retrying; 4xx are not. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

// ----------------------------------------------------------------------

const REQUEST_TIMEOUT_MS = 15_000;

function buildUrl(path: string, params?: Record<string, string | number | undefined>): string {
  // `serverUrl` empty => same origin, which is what the Vite dev proxy relies on.
  const base = CONFIG.serverUrl || window.location.origin;
  const url = new URL(path, base);

  Object.entries(params ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== '') {
      url.searchParams.set(key, String(value));
    }
  });

  return url.toString();
}

/**
 * The single egress point for every backend call. Read-only by design: this app
 * has no write endpoints, so the client exposes no method parameter.
 *
 * `signal` comes from TanStack Query so unmounting a component aborts in flight
 * requests. It is combined with an internal timeout.
 */
export async function apiGet<T>(
  path: string,
  options: {
    params?: Record<string, string | number | undefined>;
    signal?: AbortSignal;
  } = {}
): Promise<T> {
  const url = buildUrl(path, options.params);
  const signals = [AbortSignal.timeout(REQUEST_TIMEOUT_MS)];

  if (options.signal) {
    signals.push(options.signal);
  }

  let response: Response;

  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.any(signals),
    });
  } catch (error) {
    throw new ApiError('无法连接到服务，请检查网络后重试。', { status: 0, url, cause: error });
  }

  if (!response.ok) {
    throw new ApiError(`请求失败（${response.status}）。`, { status: response.status, url });
  }

  try {
    return (await response.json()) as T;
  } catch (error) {
    throw new ApiError('服务返回了无法解析的数据。', { status: response.status, url, cause: error });
  }
}
