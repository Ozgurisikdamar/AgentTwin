import { act, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DARK_SHADE,
  PALETTE_FILE,
  SHADES,
  darkValues,
  lightValues,
  parseTailwindColors,
  renderPalette,
  tailwindThemeCss,
} from "../../scripts/theme-palette";
import { ThemeProvider, ThemeSwitcher, useTheme } from "@/components/shell/theme";
import { THEME_COOKIE, parseTheme, resolveTheme, themeCookie } from "@/lib/theme";

// ---------------------------------------------------------------- colour maths

/** WCAG relative luminance of a CSS colour (`#rgb`, `#rrggbb` or `oklch(L% C H)`). */
function luminance(css: string): number {
  let rgb: [number, number, number];
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(css);
  const ok = /^oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+|none)\)$/.exec(css);
  if (hex) {
    const h = hex[1]!.length === 3 ? [...hex[1]!].map((c) => c + c).join("") : hex[1]!;
    const srgb = [0, 2, 4].map((i) => Number.parseInt(h.slice(i, i + 2), 16) / 255);
    rgb = srgb.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)) as [
      number,
      number,
      number,
    ];
  } else if (ok) {
    const L = Number(ok[1]) / 100;
    const C = Number(ok[2]);
    const H = ok[3] === "none" ? 0 : (Number(ok[3]) * Math.PI) / 180;
    const a = C * Math.cos(H);
    const b = C * Math.sin(H);
    const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
    const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
    const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
    rgb = [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
    ].map((c) => Math.min(1, Math.max(0, c))) as [number, number, number];
  } else {
    throw new Error(`unsupported colour ${css}`);
  }
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p) as [number, number];
  return (x + 0.05) / (y + 0.05);
}

const colors = parseTailwindColors(tailwindThemeCss());
const LIGHT = lightValues(colors);
const DARK = darkValues(colors);

// --------------------------------------------------- colour pairs in the source

const SRC = path.resolve(__dirname, "../../src");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.tsx?$/.test(name) && !name.endsWith(".gen.ts") ? [full] : [];
  });
}

const TOKEN = /(?<![\w:-])((?:[a-z[\]=-]+:)*)(bg|text)-(white|black|[a-z]+-\d{2,3})(\/\d+)?(?![\w-])/g;

interface Pair {
  text: string;
  bg: string;
  where: string;
}

/**
 * The text/background colour pairs the components write in one class string,
 * per state (hover:, data-[state=active]: …); a text colour with no
 * background of its own sits on a card (white) or the page (slate-50).
 */
function sourcePairs(): Pair[] {
  const pairs = new Map<string, Pair>();
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, "utf8");
    for (const [, dq, bt] of text.matchAll(/"([^"\n]*)"|`([^`]*)`/g)) {
      const literal = dq ?? bt ?? "";
      const tokens = [...literal.matchAll(TOKEN)]
        .filter(
          ([, , , name, alpha]) =>
            alpha === undefined && (name === "white" || name === "black" || LIGHT.has(name!)),
        )
        .map(([, mods, kind, name]) => ({ mods: mods!, kind: kind!, name: name! }))
        .filter((t) => !t.mods.includes("placeholder:"));
      const states = new Set(tokens.map((t) => t.mods));
      for (const state of states) {
        const pick = (kind: string) => {
          const own = tokens.filter((t) => t.kind === kind && t.mods === state).map((t) => t.name);
          return own.length ? own : tokens.filter((t) => t.kind === kind && t.mods === "").map((t) => t.name);
        };
        const texts = pick("text");
        const bgs = pick("bg");
        for (const t of texts) {
          for (const b of bgs.length ? bgs : ["white", "slate-50"]) {
            const key = `${t}/${b}`;
            if (!pairs.has(key)) pairs.set(key, { text: t, bg: b, where: path.relative(SRC, file) });
          }
        }
      }
    }
  }
  return [...pairs.values()];
}

// ------------------------------------------------------------------------ tests

describe("palette", () => {
  it("is the committed palette.css (node scripts/theme-palette.ts regenerates it)", () => {
    const css = readFileSync(PALETTE_FILE, "utf8");
    expect(css).toBe(renderPalette(colors));
    // A browser without light-dark() keeps Tailwind's light colours instead
    // of losing every colour to an invalid value.
    expect(css).toMatch(/^@supports \(color: light-dark\(#000, #fff\)\) \{$/m);
    expect(css.match(/--color-[a-z0-9-]+: light-dark\(/g)).toHaveLength(LIGHT.size);
  });

  it("mirrors every family: each shade darker in the dark theme where it was lighter", () => {
    expect(DARK.get("white")).toBe(colors.families.get("slate")!.get(900));
    expect(DARK.get("black")).toBe(colors.white);
    for (const family of colors.families.keys()) {
      const dark = SHADES.map((s) => luminance(DARK.get(`${family}-${s}`)!));
      for (let i = 1; i < dark.length; i++)
        expect(dark[i], `${family}-${SHADES[i]}`).toBeGreaterThan(dark[i - 1]!);
    }
    // The dark surface sits between the page (slate-50) and a subtle panel (slate-100).
    const surface = luminance(DARK.get("white")!);
    expect(surface).toBeGreaterThan(luminance(DARK.get("slate-50")!));
    expect(surface).toBeLessThan(luminance(DARK.get("slate-100")!));
    expect(Object.keys(DARK_SHADE)).toHaveLength(SHADES.length);
  });

  it("keeps every colour pair the components use readable in the dark theme", () => {
    const pairs = sourcePairs();
    expect(pairs.length).toBeGreaterThan(40);
    const worse = pairs.flatMap((p) => {
      const light = contrast(LIGHT.get(p.text)!, LIGHT.get(p.bg)!);
      const dark = contrast(DARK.get(p.text)!, DARK.get(p.bg)!);
      // AA (4.5:1) in the dark theme, or at least as good as in the light one.
      return dark >= Math.min(4.5, light)
        ? []
        : [`${p.text} on ${p.bg} (${p.where}): ${light.toFixed(2)} → ${dark.toFixed(2)}`];
    });
    expect(worse).toEqual([]);
  });

  it("text in the light theme meets AA on its background", () => {
    const failing = sourcePairs().flatMap((p) => {
      const light = contrast(LIGHT.get(p.text)!, LIGHT.get(p.bg)!);
      return light >= 4.5 ? [] : [`${p.text} on ${p.bg} (${p.where}): ${light.toFixed(2)}`];
    });
    expect(failing).toEqual([]);
  });
});

describe("theme choice", () => {
  it("reads the cookie: light, dark or system; anything else is system", () => {
    expect(parseTheme("dark")).toBe("dark");
    expect(parseTheme("light")).toBe("light");
    expect(parseTheme("system")).toBe("system");
    expect(parseTheme("Dark")).toBe("system");
    expect(parseTheme("")).toBe("system");
    expect(parseTheme(undefined)).toBe("system");
  });

  it("remembers the choice for a year, Secure over HTTPS", () => {
    expect(themeCookie("dark", false)).toBe(`${THEME_COOKIE}=dark; Path=/; Max-Age=31536000; SameSite=Lax`);
    expect(themeCookie("light", true)).toMatch(/; Secure$/);
  });

  it("resolves system against the OS preference", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });
});

describe("ThemeSwitcher", () => {
  afterEach(() => {
    delete document.documentElement.dataset.theme;
    document.cookie = `${THEME_COOKIE}=; Max-Age=0; Path=/`;
    vi.unstubAllGlobals();
  });

  function Shown() {
    return <p data-testid="resolved">{useTheme().resolved}</p>;
  }

  it("shows the choice and switches the page and the cookie", () => {
    render(
      <ThemeProvider initial="light">
        <ThemeSwitcher />
        <Shown />
      </ThemeProvider>,
    );
    const group = screen.getByRole("group", { name: "Theme" });
    const buttons = [...group.querySelectorAll("button")];
    expect(buttons.map((b) => b.textContent)).toEqual(["Light", "Dark", "System"]);
    expect(screen.getByRole("button", { name: "Light" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Dark" }));
    expect(screen.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Light" })).toHaveAttribute("aria-pressed", "false");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.cookie).toContain(`${THEME_COOKIE}=dark`);
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("follows the OS while the choice is system, and when it changes", () => {
    let dark = true;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (query: string) => ({
      get matches() {
        return query === "(prefers-color-scheme: dark)" && dark;
      },
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    }));
    render(
      <ThemeProvider initial="system">
        <Shown />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    act(() => {
      dark = false;
      for (const fn of listeners) fn();
    });
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
  });
});
