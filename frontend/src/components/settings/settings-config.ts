import type { SettingsState } from './types';

import { CONFIG } from 'src/global-config';
import { themeConfig } from 'src/theme/theme-config';

// ----------------------------------------------------------------------

export const SETTINGS_STORAGE_KEY: string = 'linuxdo-oss-settings';

export const defaultSettings: SettingsState = {
  mode: themeConfig.defaultMode,
  contrast: 'default',
  primaryColor: 'default',
  compactLayout: true,
  fontSize: 16,
  version: CONFIG.appVersion,
};
