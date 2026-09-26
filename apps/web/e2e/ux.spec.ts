import AxeBuilder from "@axe-core/playwright";
import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Themes, small screens and accessibility across every page of the app
 * (spec §42 and the UX items of the Definition of Done), against the running
 * stack and its seeded demo workspace.
 */

const THEME_COOKIE = "agenttwin_theme";
const SHOTS = "../../docs/screenshots";

/** Every list and form page. */
const PAGES = [
  "/overview",
  "/releases",
  "/regressions",
  "/approvals",
  "/policies",
  "/policies/new",
  "/decisions",
  "/changes",
  "/graph",
  "/simulations",
  "/simulations/new",
  "/evaluations",
  "/evaluations/new",
  "/datasets",
  "/datasets/new",
  "/reviews",
  "/judges",
  "/scenarios",
  "/scenarios/new",
  "/traces",
  "/agents",
];

/**
 * Detail pages, found from their list: the first link whose path has one
 * more segment (a second, deeper level for the case pages).
 */
const DETAILS: { list: string; depth: number; url?: string }[] = [
  { list: "/releases", depth: 2 },
  { list: "/regressions", depth: 2 },
  // The inbox shows what is pending; nothing may be.
  { list: "/approvals", depth: 2, url: "/approvals?status=all" },
  { list: "/policies", depth: 2 },
  { list: "/changes", depth: 2 },
  { list: "/simulations", depth: 2 },
  { list: "/evaluations", depth: 2 },
  { list: "/datasets", depth: 2 },
  { list: "/scenarios", depth: 2 },
  { list: "/traces", depth: 2 },
];

async function firstLinkBelow(page: Page, prefix: string, depth: number): Promise<string | null> {
  const hrefs = await page
    .locator(`main a[href^="${prefix}/"]`)
    .evaluateAll((links) => links.map((a) => a.getAttribute("href") ?? ""));
  return (
    hrefs
      .map((h) => h.split(/[?#]/)[0]!)
      .find((h) => h.split("/").filter(Boolean).length === depth && !h.endsWith("/new")) ?? null
  );
}

/** Every page the app has: the lists, then a detail and a case page of each. */
async function allPages(page: Page): Promise<string[]> {
  const found = [...PAGES];
  for (const { list, depth, url } of DETAILS) {
    await page.goto(url ?? list);
    await page.locator("main").waitFor();
    await page.waitForLoadState("networkidle");
    const detail = await firstLinkBelow(page, list, depth);
    expect(detail, `a ${list} detail link in the seeded workspace`).not.toBeNull();
    found.push(detail!);
    if (list === "/simulations" || list === "/evaluations") {
      await page.goto(detail!);
      await page.waitForLoadState("networkidle");
      const caseLink = await firstLinkBelow(page, `${detail}/cases`, 4);
      expect(caseLink, `a case of ${detail}`).not.toBeNull();
      found.push(caseLink!);
    }
  }
  return found;
}

async function settle(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.locator("main").waitFor();
  await page.waitForLoadState("networkidle");
  // Skeletons gone: the page shows its data (or its empty/error state).
  await expect(page.locator('[data-slot="skeleton"], .animate-pulse')).toHaveCount(0);
}

async function axe(page: Page) {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  return result.violations.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.help}\n` +
      v.nodes
        .slice(0, 5)
        .map((n) => `    ${n.target.join(" ")} — ${n.failureSummary?.split("\n").slice(1).join(" ")}`)
        .join("\n"),
  );
}

function bodyBackground(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

/** Relative luminance of a computed colour (rgb() or oklch()), via a canvas. */
function luminanceOf(page: Page, color: string): Promise<number> {
  return page.evaluate((c) => {
    const ctx = document.createElement("canvas").getContext("2d")!;
    ctx.fillStyle = c;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    const lin = (v: number) => {
      const s = v / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * lin(r!) + 0.7152 * lin(g!) + 0.0722 * lin(b!);
  }, color);
}

test.describe("themes", () => {
  test("the page renders in the chosen theme, and the choice is remembered", async ({ page, context }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/overview");
    // Default: system, which follows the OS (light here).
    await expect(page.locator("html")).toHaveAttribute("data-theme", "system");
    expect(await luminanceOf(page, await bodyBackground(page))).toBeGreaterThan(0.8);

    await page.getByRole("group", { name: "Theme" }).getByRole("button", { name: "Dark" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    expect(await luminanceOf(page, await bodyBackground(page))).toBeLessThan(0.05);
    const cookie = (await context.cookies()).find((c) => c.name === THEME_COOKIE);
    expect(cookie?.value).toBe("dark");
    await settle(page, "/overview");
    await page.screenshot({ path: `${SHOTS}/ux-dark-overview.png`, fullPage: true });

    // Rendered dark by the server on the next load: no flash of the light theme.
    const response = await page.request.get("/releases");
    expect(await response.text()).toContain('data-theme="dark"');
    await page.goto("/releases");
    await expect(page.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");

    await page.getByRole("button", { name: "Light" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    expect(await luminanceOf(page, await bodyBackground(page))).toBeGreaterThan(0.8);
    expect(problems).toEqual([]);
  });

  test("system follows the operating system", async ({ browser }) => {
    const context = await browser.newContext({ colorScheme: "dark" });
    const page = await context.newPage();
    await signIn(page, "viewer@demo.agenttwin.dev", "/traces");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "system");
    expect(await luminanceOf(page, await bodyBackground(page))).toBeLessThan(0.05);
    // The graph canvas follows it too.
    await page.goto("/graph");
    await expect(page.locator(".react-flow")).toHaveClass(/\bdark\b/);
    await context.close();
  });
});

test.describe("accessibility", () => {
  // Every page, twice: a long test.
  test.setTimeout(600_000);

  for (const theme of ["light", "dark"] as const) {
    test(`every page passes axe (WCAG 2.1 A/AA) in the ${theme} theme`, async ({
      page,
      context,
      baseURL,
    }) => {
      await context.addCookies([{ name: THEME_COOKIE, value: theme, url: baseURL! }]);
      await signIn(page, "owner@demo.agenttwin.dev", "/overview");
      const pages = await allPages(page);
      expect(pages.length).toBe(PAGES.length + DETAILS.length + 2);
      const failures: string[] = [];
      for (const path of pages) {
        await settle(page, path);
        for (const v of await axe(page)) failures.push(`${path}: ${v}`);
      }
      expect(failures, failures.join("\n")).toEqual([]);
    });
  }

  test("the sign-in page passes axe in both themes", async ({ page, context, baseURL }) => {
    for (const theme of ["light", "dark"]) {
      await context.addCookies([{ name: THEME_COOKIE, value: theme, url: baseURL! }]);
      await page.goto("/login");
      await page.getByLabel("Email").waitFor();
      expect(await axe(page), theme).toEqual([]);
    }
  });
});

test.describe("small screens", () => {
  test.setTimeout(600_000);
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test("no page scrolls sideways, and the navigation opens from the menu", async ({ page }) => {
    await signIn(page, "owner@demo.agenttwin.dev", "/overview");
    const menu = page.getByRole("button", { name: "Menu" });
    await expect(menu).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden();
    await menu.click();
    const nav = page.locator("#mobile-nav");
    await expect(nav.getByRole("link")).toHaveCount(15);
    await page.screenshot({ path: `${SHOTS}/ux-phone-menu.png` });
    await nav.getByRole("link", { name: "Releases" }).click();
    await expect(page).toHaveURL(/\/releases$/);
    await expect(nav).toBeHidden();

    const pages = await allPages(page);
    const overflowing: string[] = [];
    for (const path of pages) {
      await settle(page, path);
      const { scroll, width } = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        width: document.documentElement.clientWidth,
      }));
      if (scroll > width + 1) overflowing.push(`${path}: ${scroll}px wide in a ${width}px viewport`);
    }
    expect(overflowing, overflowing.join("\n")).toEqual([]);
    await settle(page, "/releases");
    await page.screenshot({ path: `${SHOTS}/ux-phone-releases.png`, fullPage: true });
  });

  test("the pages pass axe on a phone", async ({ page }) => {
    await signIn(page, "owner@demo.agenttwin.dev", "/releases");
    for (const path of ["/releases", "/traces", "/approvals"]) {
      await settle(page, path);
      expect(await axe(page), path).toEqual([]);
    }
  });
});
