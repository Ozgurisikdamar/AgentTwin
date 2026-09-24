import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/app-shell";
import { serverFetch } from "@/lib/server/control-plane";
import type { Me } from "@/lib/types";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const me = await serverFetch<Me>("/api/v1/me");
  if (!me.ok) {
    if (me.status === 401) redirect("/api/auth/expired");
    return (
      <main className="mx-auto max-w-xl p-8" role="alert">
        <h1 className="text-lg font-semibold text-slate-900">AgentTwin is not reachable</h1>
        <p className="mt-2 text-sm text-slate-700">
          The web app could not reach the control plane ({me.code}). If you run the local stack, check it with{" "}
          <code className="rounded bg-slate-100 px-1">make doctor</code> and reload this page.
        </p>
      </main>
    );
  }
  return <AppShell me={me.data}>{children}</AppShell>;
}
