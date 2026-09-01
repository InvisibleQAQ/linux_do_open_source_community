import packageJson from '../package.json';

// ----------------------------------------------------------------------

export type ConfigValue = {
  appName: string;
  appVersion: string;
  /**
   * Origin of the backend Python Worker, e.g. `https://api.example.workers.dev`.
   * Empty string means "same origin" — the dev server proxies `/api` (see
   * `vite.config.ts`) and production can route `/api/*` on one hostname.
   */
  serverUrl: string;
  /** Base path for static assets under `public/`. Empty for root-hosted deploys. */
  assetsDir: string;
};

export const CONFIG: ConfigValue = {
  appName: 'Linux.do 开源项目聚合',
  appVersion: packageJson.version,
  serverUrl: import.meta.env.VITE_SERVER_URL ?? '',
  assetsDir: import.meta.env.VITE_ASSETS_DIR ?? '',
};
