"use client";

import { LogIn } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { humanize } from "@/lib/format";

export interface AuthConfig {
  mode: "dev" | "oidc";
  demo_users?: { email: string; name: string; role: string }[];
  oidc?: { issuer?: string; client_id?: string };
}

export function LoginForm({
  config,
  configError,
  next,
  expired,
}: {
  config: AuthConfig | null;
  configError: string | null;
  next: string;
  expired: boolean;
}) {
  const router = useRouter();
  const [email, setEmail] = useState(config?.demo_users?.[0]?.email ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(address: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: address }),
        credentials: "same-origin",
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } };
        setError(body.error?.message ?? "Sign-in failed.");
        return;
      }
      router.replace(next);
      router.refresh();
    } catch {
      setError("The server could not be reached.");
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void signIn(email);
  }

  if (!config) {
    return (
      <Card>
        <CardContent>
          <p role="alert" className="text-sm text-rose-800">
            {configError ?? "The control plane is not reachable."} Start the stack with{" "}
            <code className="rounded bg-slate-100 px-1">make dev</code> and reload.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (config.mode === "oidc") {
    return (
      <Card>
        <CardContent className="text-sm text-slate-700">
          This deployment uses single sign-on ({config.oidc?.issuer ?? "OIDC"}). Browser sign-in through your
          identity provider is configured by your administrator; API clients authenticate with provider-issued
          tokens.
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="space-y-4">
        {expired ? (
          <p role="status" className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
            Your session ended. Sign in again to continue.
          </p>
        ) : null}
        <p className="text-sm text-slate-600">
          Development sign-in with a seeded demo account. Production deployments use OIDC single sign-on.
        </p>
        <form onSubmit={onSubmit} className="space-y-3">
          <div>
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          {error ? (
            <p role="alert" className="text-sm text-rose-700">
              {error}
            </p>
          ) : null}
          <Button type="submit" variant="primary" className="w-full" disabled={busy}>
            <LogIn className="h-4 w-4" aria-hidden="true" />
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        {config.demo_users?.length ? (
          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Demo accounts</p>
            <ul className="divide-y divide-slate-100 rounded-md border border-slate-200">
              {config.demo_users.map((u) => (
                <li key={u.email}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setEmail(u.email);
                      void signIn(u.email);
                    }}
                    className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-slate-50 disabled:opacity-50"
                  >
                    <span>
                      <span className="block font-medium text-slate-900">{u.name}</span>
                      <span className="block text-xs text-slate-500">{u.email}</span>
                    </span>
                    <span className="text-xs text-slate-600">{humanize(u.role)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
