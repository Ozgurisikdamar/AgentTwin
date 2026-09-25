import type { Metadata } from "next";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { LoginForm, type AuthConfig } from "@/components/auth/login-form";
import { SESSION_COOKIE, safeNextPath } from "@/lib/bff";
import { serverFetch } from "@/lib/server/control-plane";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; expired?: string }>;
}) {
  const sp = await searchParams;
  const next = safeNextPath(sp.next);
  const expired = sp.expired === "1";
  if (!expired && (await cookies()).has(SESSION_COOKIE)) redirect(next);
  const config = await serverFetch<AuthConfig>("/api/v1/auth/config", { auth: false });
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-2">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 items-center justify-center rounded-md bg-indigo-600 font-bold text-white"
          >
            AT
          </span>
          <div>
            <h1 className="text-lg font-semibold text-slate-900">Sign in to AgentTwin</h1>
            <p className="text-sm text-slate-600">Production assurance for AI agents</p>
          </div>
        </div>
        <LoginForm
          config={config.ok ? config.data : null}
          configError={config.ok ? null : config.message}
          next={next}
          expired={expired}
        />
      </div>
    </main>
  );
}
