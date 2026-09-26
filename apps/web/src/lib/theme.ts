/**
 * The colour theme (ADR-0035). The choice lives in a cookie so the server
 * renders the page in it: no script runs before the first paint and nothing
 * flashes. "system" follows the operating system through CSS alone.
 */

export const THEMES = ["light", "dark", "system"] as const;
export type Theme = (typeof THEMES)[number];

export const THEME_COOKIE = "agenttwin_theme";
const ONE_YEAR_SECONDS = 365 * 24 * 60 * 60;

/** The theme a cookie value names; anything else is "system". */
export function parseTheme(value: string | undefined | null): Theme {
  return THEMES.includes(value as Theme) ? (value as Theme) : "system";
}

/** The `document.cookie` assignment that remembers a theme. */
export function themeCookie(theme: Theme, secure: boolean): string {
  return `${THEME_COOKIE}=${theme}; Path=/; Max-Age=${ONE_YEAR_SECONDS}; SameSite=Lax${secure ? "; Secure" : ""}`;
}

/** The theme actually shown: "system" resolved against the OS preference. */
export function resolveTheme(theme: Theme, prefersDark: boolean): "light" | "dark" {
  if (theme === "system") return prefersDark ? "dark" : "light";
  return theme;
}
