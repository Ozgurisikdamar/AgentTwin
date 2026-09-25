"use client";

import {
  Activity,
  Bot,
  Bug,
  Database,
  FlaskConical,
  GitCompareArrows,
  GitPullRequestArrow,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Network,
  Rocket,
  UserCheck,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useState } from "react";
import { humanize } from "@/lib/format";
import type { Me } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MeProvider } from "./me-context";

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

export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

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
          <span
            aria-hidden="true"
            className="flex h-7 w-7 items-center justify-center rounded-md bg-indigo-600 text-sm font-bold text-white"
          >
            AT
          </span>
          <span className="text-sm font-semibold tracking-tight">AgentTwin</span>
        </div>
        <nav aria-label="Main" className="flex-1 space-y-0.5 p-2">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
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
          })}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-4 border-b border-slate-200 bg-white px-4">
          <nav aria-label="Main (compact)" className="flex gap-1 md:hidden">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="rounded px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="hidden text-sm text-slate-600 md:block">
            <span className="font-medium text-slate-900">{me.organization.name}</span>
          </div>
          <div className="flex items-center gap-3">
            <div className="text-right leading-tight">
              <p className="text-sm font-medium text-slate-900" data-testid="current-user">
                {me.user?.display_name ?? me.principal.email ?? me.principal.sub}
              </p>
              <p className="text-xs text-slate-500">{humanize(me.principal.role)}</p>
            </div>
            <button
              type="button"
              onClick={signOut}
              disabled={signingOut}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 px-2.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              Sign out
            </button>
          </div>
        </header>
        <main id="main" className="min-w-0 flex-1 p-4 md:p-6">
          <MeProvider me={me}>{children}</MeProvider>
        </main>
      </div>
    </div>
  );
}
