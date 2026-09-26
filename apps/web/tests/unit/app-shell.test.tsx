import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/shell/app-shell";
import { ThemeProvider } from "@/components/shell/theme";
import type { Me } from "@/lib/types";

vi.mock("next/navigation", () => ({
  usePathname: () => "/releases/r1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const ME: Me = {
  principal: { org: "o1", sub: "u1", email: "olivia@demo.agenttwin.dev", role: "owner" },
  user: { id: "u1", email: "olivia@demo.agenttwin.dev", display_name: "Olivia Owner" },
  organization: { id: "o1", slug: "demo-co", name: "Demo Co" },
  permissions: [],
};

function renderShell() {
  render(
    <ThemeProvider initial="system">
      <AppShell me={ME}>
        <p>page</p>
      </AppShell>
    </ThemeProvider>,
  );
}

describe("AppShell", () => {
  it("marks the current section in the sidebar and names its controls", () => {
    renderShell();
    const sidebar = screen.getByRole("navigation", { name: "Main" });
    expect(within(sidebar).getAllByRole("link")).toHaveLength(15);
    expect(within(sidebar).getByRole("link", { name: "Releases" })).toHaveAttribute("aria-current", "page");
    expect(within(sidebar).getByRole("link", { name: "Traces" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Theme" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });

  it("opens the navigation from a menu button on small screens and closes it", () => {
    renderShell();
    const menu = screen.getByRole("button", { name: "Menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("mobile-nav")).toBeNull();

    fireEvent.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(menu).toHaveAttribute("aria-controls", "mobile-nav");
    const panel = document.getElementById("mobile-nav")!;
    expect(within(panel).getAllByRole("link")).toHaveLength(15);
    expect(within(panel).getByRole("link", { name: "Releases" })).toHaveAttribute("aria-current", "page");

    // Following a link closes the menu.
    fireEvent.click(within(panel).getByRole("link", { name: "Traces" }));
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("mobile-nav")).toBeNull();

    // Escape closes it and gives the focus back to the button.
    fireEvent.click(menu);
    const link = within(document.getElementById("mobile-nav")!).getByRole("link", { name: "Graph" });
    link.focus();
    fireEvent.keyDown(link, { key: "Escape" });
    expect(document.getElementById("mobile-nav")).toBeNull();
    expect(menu).toHaveFocus();

    // Escape on the button itself, too.
    fireEvent.click(menu);
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(document.getElementById("mobile-nav")).toBeNull();
  });
});
