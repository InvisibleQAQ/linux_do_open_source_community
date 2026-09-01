import type { Theme, ThemeProviderProps as MuiThemeProviderProps } from '@mui/material/styles';
import type {} from './extend-theme-types';
import type { ThemeOptions } from './types';

import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider as ThemeVarsProvider } from '@mui/material/styles';

import { useSettingsContext } from 'src/components/settings';

import { createTheme } from './create-theme';

// ----------------------------------------------------------------------

/**
 * Ported from Minimal v7.7.0 with two removals:
 * - `useTranslate()` / `localeComponents`: this app is Chinese-only, no i18n layer.
 * - `<Rtl>`: LTR only, so `with-settings/right-to-left.tsx` was dropped along with
 *   its `@emotion/cache` + `@mui/stylis-plugin-rtl` dependencies.
 */

export type ThemeProviderProps = Partial<MuiThemeProviderProps<Theme>> & {
  themeOverrides?: ThemeOptions;
};

export function ThemeProvider({ themeOverrides, children, ...other }: ThemeProviderProps) {
  const settings = useSettingsContext();

  const theme = createTheme({
    settingsState: settings.state,
    themeOverrides,
  });

  return (
    <ThemeVarsProvider disableTransitionOnChange theme={theme} {...other}>
      <CssBaseline />
      {children}
    </ThemeVarsProvider>
  );
}
