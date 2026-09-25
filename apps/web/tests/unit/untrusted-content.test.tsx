import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OverrideBanner } from "@/components/releases/gate-sections";
import type { ReleaseGate } from "@/lib/api/control-plane";
import { liveOverriddenGate } from "./release-fixtures";

/**
 * Everything the UI shows from a trace, a scenario or a person's input is
 * untrusted (spec §63: XSS in trace content, malicious Markdown). React
 * escapes text; these tests keep the code from stepping around that, and the
 * live check is e2e/security.spec.ts.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, "../..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) return sources(p);
    return /\.(ts|tsx)$/.test(name) && !name.endsWith(".gen.ts") ? [p] : [];
  });
}

describe("untrusted content", () => {
  const files = sources(path.join(web, "src"));

  it("scans the whole application", () => {
    expect(files.length).toBeGreaterThan(100);
    expect(files.some((f) => f.endsWith(path.join("traces", "span-details.tsx")))).toBe(true);
  });

  it("never renders a string as markup or code", () => {
    const sinks =
      /dangerouslySetInnerHTML|\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write|\beval\(|new Function\(|srcDoc/;
    const found = files.filter((f) => sinks.test(readFileSync(f, "utf8"))).map((f) => path.relative(web, f));
    expect(found).toEqual([]);
  });

  it("has no Markdown or HTML renderer to be tricked", () => {
    const pkg = JSON.parse(readFileSync(path.join(web, "package.json"), "utf8")) as {
      dependencies: Record<string, string>;
      devDependencies: Record<string, string>;
    };
    const renderers = /markdown|marked|remark|rehype|showdown|mdx|html-react-parser|sanitize-html|dompurify/i;
    const deps = [...Object.keys(pkg.dependencies), ...Object.keys(pkg.devDependencies)];
    expect(deps.filter((d) => renderers.test(d))).toEqual([]);
  });

  it("opens other sites only without handing them this window", () => {
    const blank = files.flatMap((f) => {
      const text = readFileSync(f, "utf8");
      return [...text.matchAll(/<a\b[^>]*target="_blank"[^>]*>/gs)].map((m) => ({ f, tag: m[0] }));
    });
    expect(blank.length).toBeGreaterThan(0);
    for (const { f, tag } of blank) expect(tag, path.relative(web, f)).toContain('rel="noopener noreferrer"');
  });

  it.each(["javascript:alert(document.cookie)", "JaVaScRiPt:alert(1)", "data:text/html,<script>x</script>"])(
    "shows a ticket URL %s as text, not a link",
    (url) => {
      const gate = liveOverriddenGate as unknown as ReleaseGate;
      render(<OverrideBanner gate={{ ...gate, override: { ...gate.override!, ticket_url: url } }} />);
      expect(screen.getByTestId("override-banner")).toHaveTextContent(url);
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    },
  );
});
