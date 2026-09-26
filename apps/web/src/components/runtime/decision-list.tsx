"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type Decision, type DecisionPage, queryOf } from "@/lib/api/runtime";
import { formatRelative } from "@/lib/format";
import { EFFECTS, OUTCOMES, effectLabel, outcomeLabel } from "@/lib/runtime";
import { useUrlQuery } from "@/lib/use-url-query";
import { ProjectPicker, useProject } from "./project-picker";
import { EffectBadge, OutcomeBadge } from "./runtime-badges";

const PAGE_SIZE = 50;
const TRACE = /^[0-9a-f]{32}$/;

function DecisionRow({ d, now, projectId }: { d: Decision; now: Date; projectId: string }) {
  return (
    <tr className="align-top" data-testid="decision-row" data-decision-id={d.id}>
      <td className="whitespace-nowrap px-3 py-2.5 text-slate-700" title={d.created_at}>
        {formatRelative(d.created_at, now)}
      </td>
      <td className="px-3 py-2.5">
        <div className="text-slate-900">{d.summary}</div>
        <div className="mt-1 text-xs text-slate-500">
          {d.agent} v{d.agent_version} · {d.environment}
        </div>
      </td>
      <td className="px-3 py-2.5">{d.effect ? <EffectBadge effect={d.effect} /> : "—"}</td>
      <td className="px-3 py-2.5">
        <OutcomeBadge outcome={d.outcome} />
        {d.error_code ? <div className="mt-1 font-mono text-xs text-rose-800">{d.error_code}</div> : null}
      </td>
      <td className="px-3 py-2.5 text-slate-800">
        {d.policy ?? <span className="text-slate-500">no policy</span>}
        {d.rule ? <div className="text-xs text-slate-500">{d.rule}</div> : null}
        {d.fail_mode_applied ? <div className="text-xs text-amber-900">fail mode applied</div> : null}
      </td>
      <td className="px-3 py-2.5">
        {d.approval_id ? (
          <Link
            href={`/approvals/${d.approval_id}?project_id=${projectId}`}
            className="text-xs text-indigo-700 hover:underline"
            aria-label={`Approval request of ${d.summary}`}
          >
            Approval
          </Link>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2.5">
        {d.trace_id ? (
          <Link href={`/traces/${d.trace_id}`} className="font-mono text-xs text-indigo-700 hover:underline">
            {d.trace_id.slice(0, 12)}
          </Link>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}

const COLUMNS = ["When", "Action", "Decision", "Outcome", "Policy", "Approval", "Trace"];

export function DecisionList() {
  const { params, update, replace } = useUrlQuery();
  const effect = EFFECTS.find((e) => e === params.get("effect"));
  const outcome = OUTCOMES.find((o) => o === params.get("outcome"));
  const tool = params.get("tool") ?? "";
  const traceParam = (params.get("trace_id") ?? "").toLowerCase();
  const traceId = TRACE.test(traceParam) ? traceParam : undefined;
  const now = new Date();
  const { projects, list, projectId } = useProject();

  const decisions = useInfiniteQuery({
    queryKey: ["policy-decisions", projectId, effect, outcome, tool, traceId],
    queryFn: ({ pageParam, signal }) =>
      api<DecisionPage>(
        withQuery(
          "/policy-decisions",
          queryOf<"listPolicyDecisions">({
            project_id: projectId,
            effect,
            outcome,
            tool: tool || undefined,
            trace_id: traceId,
            limit: PAGE_SIZE,
            cursor: pageParam || undefined,
          }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: Boolean(projectId),
  });
  const items = decisions.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(effect || outcome || tool || traceId);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Decisions"
        description="Every tool call the runtime gateway decided: what it allowed, held for a person or refused, and why."
        actions={
          <Button
            size="sm"
            onClick={() => void decisions.refetch()}
            disabled={decisions.isFetching || !projectId}
          >
            <RefreshCw
              className={`h-4 w-4 ${decisions.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </Button>
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        <ProjectPicker id="decision-project" list={list} projectId={projectId} />
        <div className="w-44">
          <Label htmlFor="decision-effect">Decision</Label>
          <Select
            id="decision-effect"
            value={effect ?? ""}
            onChange={(e) => update({ effect: e.target.value })}
          >
            <option value="">Any</option>
            {EFFECTS.map((e) => (
              <option key={e} value={e}>
                {effectLabel(e)}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-44">
          <Label htmlFor="decision-outcome">Outcome</Label>
          <Select
            id="decision-outcome"
            value={outcome ?? ""}
            onChange={(e) => update({ outcome: e.target.value })}
          >
            <option value="">Any</option>
            {OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {outcomeLabel(o)}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-48">
          <Label htmlFor="decision-tool">Tool</Label>
          <Input
            key={tool}
            id="decision-tool"
            defaultValue={tool}
            placeholder="refund_payment"
            onBlur={(e) => update({ tool: e.target.value.trim() })}
            onKeyDown={(e) => {
              if (e.key === "Enter") update({ tool: e.currentTarget.value.trim() });
            }}
          />
        </div>
        <div className="w-80">
          <Label htmlFor="decision-trace">Trace</Label>
          <Input
            key={traceId ?? ""}
            id="decision-trace"
            defaultValue={traceId ?? ""}
            placeholder="32 hex characters"
            className="font-mono text-xs"
            onBlur={(e) => update({ trace_id: e.target.value.trim().toLowerCase() })}
            onKeyDown={(e) => {
              if (e.key === "Enter") update({ trace_id: e.currentTarget.value.trim().toLowerCase() });
            }}
          />
        </div>
        {filtered ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              const next = new URLSearchParams();
              if (params.get("project_id")) next.set("project_id", projectId);
              replace(next);
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {projects.isPending || (Boolean(projectId) && decisions.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading decisions">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : (projects.error ?? decisions.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? decisions.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No decisions match" : "No tool call went through the gateway yet"}>
            Agents that call their tools through{" "}
            <code className="rounded bg-slate-100 px-1">/gateway/v1</code> get every call decided here.
          </EmptyState>
        ) : (
          <>
            <div className="relative overflow-x-auto">
              <table className="w-full min-w-[64rem] text-left text-sm">
                <caption className="sr-only">Gateway decisions, newest first</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    {COLUMNS.map((c) => (
                      <th key={c} scope="col" className="px-3 py-2 font-medium">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((d) => (
                    <DecisionRow key={d.id} d={d} now={now} projectId={projectId} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "decision" : "decisions"}
              </span>
              {decisions.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void decisions.fetchNextPage()}
                  disabled={decisions.isFetchingNextPage}
                >
                  {decisions.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
