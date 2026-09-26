import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Two layout rules that keep every page inside a phone's width (the
 * end-to-end suite measures it; these catch a new component before that).
 */

const SRC = path.resolve(__dirname, "../../src");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) return files(full);
    return name.endsWith(".tsx") ? [full] : [];
  });
}

/** Every string literal of the components with the classes it holds. */
function classStrings(): { where: string; classes: string[] }[] {
  return files(SRC).flatMap((file) =>
    [...readFileSync(file, "utf8").matchAll(/"([^"\n]*)"/g)].map((m) => ({
      where: path.relative(SRC, file),
      classes: m[1]!.split(/\s+/).filter(Boolean),
    })),
  );
}

describe("layout", () => {
  it("positions every scroll container, so screen-reader text inside cannot widen the page", () => {
    // An absolutely positioned descendant (`sr-only`) is clipped by a scroll
    // container only when the container is its containing block.
    const offenders = classStrings()
      .filter(({ classes }) => classes.some((c) => c === "overflow-x-auto" || c === "overflow-auto"))
      .filter(({ classes }) => !classes.some((c) => ["relative", "absolute", "fixed", "sticky"].includes(c)))
      .map(({ where, classes }) => `${where}: ${classes.join(" ")}`);
    expect(offenders).toEqual([]);
  });

  it("puts every table in a scroll container, so a long value scrolls the table, not the page", () => {
    const offenders = files(SRC).flatMap((file) => {
      const lines = readFileSync(file, "utf8").split("\n");
      return lines.flatMap((line, i) =>
        line.includes("<table") &&
        !lines
          .slice(Math.max(0, i - 3), i)
          .join("\n")
          .includes("overflow-x-auto")
          ? [`${path.relative(SRC, file)}:${i + 1}`]
          : [],
      );
    });
    expect(offenders).toEqual([]);
  });

  it("gives every responsive grid a shrinkable single column on small screens", () => {
    // Without a base grid-cols-*, the implicit column is as wide as its
    // widest child (a scrolled table), not as wide as the screen.
    const offenders = classStrings()
      .filter(({ classes }) => classes.includes("grid"))
      .filter(({ classes }) => classes.some((c) => /^(sm|md|lg|xl|2xl):grid-cols-/.test(c)))
      .filter(({ classes }) => !classes.some((c) => c.startsWith("grid-cols-")))
      .map(({ where, classes }) => `${where}: ${classes.join(" ")}`);
    expect(offenders).toEqual([]);
  });
});
