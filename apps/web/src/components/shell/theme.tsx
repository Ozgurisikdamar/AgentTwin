"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { type Theme, resolveTheme, themeCookie } from "@/lib/theme";
import { cn } from "@/lib/utils";

interface ThemeState {
  /** The choice: light, dark or system. */
  theme: Theme;
  /** What is shown: the choice, with "system" resolved. */
  resolved: "light" | "dark";
  setTheme(theme: Theme): void;
}

const ThemeContext = createContext<ThemeState | null>(null);

const DARK_QUERY = "(prefers-color-scheme: dark)";

function subscribeToScheme(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.(DARK_QUERY).matches);
}

/** Holds the theme the server rendered with and switches it. */
export function ThemeProvider({ initial, children }: { initial: Theme; children: ReactNode }) {
  const [theme, setState] = useState<Theme>(initial);
  // The CSS follows the OS by itself; only code that needs the resolved theme
  // (the graph canvas) learns it here, after hydration.
  const dark = useSyncExternalStore(subscribeToScheme, prefersDark, () => false);

  const setTheme = useCallback((next: Theme) => {
    setState(next);
    document.documentElement.dataset.theme = next;
    // Not HttpOnly: the choice is a display preference, not a credential.
    document.cookie = themeCookie(next, window.location.protocol === "https:");
  }, []);

  const value = useMemo(
    () => ({ theme, resolved: resolveTheme(theme, dark), setTheme }),
    [theme, dark, setTheme],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const state = useContext(ThemeContext);
  if (!state) throw new Error("useTheme outside ThemeProvider");
  return state;
}

const OPTIONS: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Monitor },
];

/** Three toggle buttons; the pressed one is the current choice. */
export function ThemeSwitcher({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  return (
    <div
      role="group"
      aria-label="Theme"
      className={cn("inline-flex items-center rounded-md border border-slate-300 p-0.5", className)}
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => {
        const pressed = theme === value;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={pressed}
            title={`${label} theme`}
            onClick={() => setTheme(value)}
            className={cn(
              "inline-flex h-7 w-7 items-center justify-center rounded",
              pressed ? "bg-slate-200 text-slate-900" : "text-slate-600 hover:bg-slate-100",
            )}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            <span className="sr-only">{label}</span>
          </button>
        );
      })}
    </div>
  );
}
