"use client";

import {
  Activity,
  Bot,
  Bug,
  Database,
  FlaskConical,
  Gavel,
  GitCompareArrows,
  GitPullRequestArrow,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Network,
  Rocket,
  ShieldCheck,
  Stamp,
  UserCheck,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useRef, useState } from "react";
import { humanize } from "@/lib/format";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MeProvider } from "./me-context";
import { ThemeSwitcher } from "./theme";

interface NavItem {
  href: string;
  label: string;
  icon: typeof Activity;
}

// Sections appear here as they ship (see docs/plan/implementation-board.md).
const NAV: NavItem[] = [
  { href: "/overview", label: "Overview", icon: LayoutDashboard },
  { href: "/releases", label: "Releases", icon: Rocket },
  { href: "/regressions", label: "Regressions", icon: Bug },
  { href: "/approvals", label: "Approvals", icon: Stamp },
  { href: "/policies", label: "Policies", icon: ShieldCheck },
  { href: "/decisions", label: "Decisions", icon: Gavel },
  { href: "/changes", label: "Changes", icon: GitPullRequestArrow },
  { href: "/graph", label: "Graph", icon: Network },
  { href: "/simulations", label: "Simulations", icon: FlaskConical },
  { href: "/evaluations", label: "Evaluations", icon: GitCompareArrows },
  { href: "/datasets", label: "Datasets", icon: Database },
  { href: "/reviews", label: "Reviews", icon: UserCheck },
  { href: "/scenarios", label: "Scenarios", icon: ListChecks },
  { href: "/traces", label: "Traces", icon: Activity },
  { href: "/agents", label: "Agents", icon: Bot },
];

function NavLinks({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return NAV.map((item) => {
    const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
    const Icon = item.icon;
    return (
      <Link
        key={item.href}
        href={item.href}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        className={cn(
          "flex items-center gap-2 rounded-md px-2.5 py-2 text-sm font-medium",
          active ? "bg-indigo-50 text-indigo-800" : "text-slate-700 hover:bg-slate-100",
        )}
      >
        <Icon className="h-4 w-4" aria-hidden="true" />
        {item.label}
      </Link>
    );
  });
}

function Brand() {
  return (
    <>
      <span
        aria-hidden="true"
        className="flex h-7 w-7 items-center justify-center rounded-md bg-indigo-600 text-sm font-bold text-white"
      >
        AT
      </span>
      <span className="text-sm font-semibold tracking-tight">AgentTwin</span>
    </>
  );
}

export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);
  // Small screens: the navigation opens from a menu button under the header.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);

  function closeMenu(returnFocus: boolean) {
    setMenuOpen(false);
    if (returnFocus) menuButton.current?.focus();
  }

  async function signOut() {
    setSigningOut(true);
    try {
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    } finally {
      router.replace("/login");
      router.refresh();
    }
  }

  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
        <div className="flex h-14 items-center gap-2 border-b border-slate-100 px-4">
          <Brand />
        </div>
        <nav aria-label="Main" className="flex-1 space-y-0.5 p-2">
          <NavLinks pathname={pathname} />
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="relative flex h-14 items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 sm:gap-4 sm:px-4">
          <div className="flex items-center gap-2 md:hidden">
            <button
              ref={menuButton}
              type="button"
              aria-expanded={menuOpen}
              aria-controls="mobile-nav"
              onClick={() => setMenuOpen((open) => !open)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && menuOpen) closeMenu(true);
              }}
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-300 text-slate-700 hover:bg-slate-100"
            >
              {menuOpen ? (
                <X className="h-4 w-4" aria-hidden="true" />
              ) : (
                <Menu className="h-4 w-4" aria-hidden="true" />
              )}
              <span className="sr-only">Menu</span>
            </button>
            <Brand />
          </div>
          <div className="hidden text-sm text-slate-600 md:block">
            <span className="font-medium text-slate-900">{me.organization.name}</span>
          </div>
          <div className="flex items-center gap-2 sm:gap-3">
            <ThemeSwitcher />
            <div className="hidden text-right leading-tight sm:block">
              <p className="text-sm font-medium text-slate-900" data-testid="current-user">
                {me.user?.display_name ?? me.principal.email ?? me.principal.sub}
              </p>
              <p className="text-xs text-slate-500">{humanize(me.principal.role)}</p>
            </div>
            <button
              type="button"
              onClick={signOut}
              disabled={signingOut}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 px-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50 sm:px-2.5"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              <span className="sr-only sm:not-sr-only">Sign out</span>
            </button>
          </div>
          {menuOpen ? (
            <nav
              id="mobile-nav"
              aria-label="Main"
              onKeyDown={(e) => {
                if (e.key === "Escape") closeMenu(true);
              }}
              className="absolute inset-x-0 top-14 z-40 max-h-[calc(100vh-3.5rem)] space-y-0.5 overflow-y-auto border-b border-slate-200 bg-white p-2 shadow-md md:hidden"
            >
              <NavLinks pathname={pathname} onNavigate={() => closeMenu(false)} />
            </nav>
          ) : null}
        </header>
        <main id="main" className="min-w-0 flex-1 p-4 md:p-6">
          <MeProvider me={me}>{children}</MeProvider>
        </main>
      </div>
    </div>
  );
}
