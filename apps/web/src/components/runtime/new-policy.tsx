"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Save } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import { type BodyOf, type PolicyCreated, type PolicyTestReport } from "@/lib/api/runtime";
import { newPolicyYaml, problemsOf } from "@/lib/runtime";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { DocumentEditor, ProblemList, TestReportView } from "./policy-sections";
import { useProject } from "./project-picker";

const TOOL = /^[A-Za-z0-9_.:-]{1,128}$/;

/** Write a policy document, test it, and store it as version 1 (nothing is activated). */
export function NewPolicy() {
  const router = useRouter();
  const qc = useQueryClient();
  const { params } = useUrlQuery();
  const toolParam = params.get("tool") ?? "";
  const canWrite = useCan("policy.write");
  const canTest = useCan("policy.test");
  const { projectId, projects } = useProject();
  const [document, setDocument] = useState(() => newPolicyYaml(TOOL.test(toolParam) ? toolParam : undefined));
  const [report, setReport] = useState<PolicyTestReport | null>(null);
  const createKey = useActionKey("policy-create");

  const test = useMutation({
    mutationFn: () =>
      api<PolicyTestReport>("/policies/test", {
        method: "POST",
        body: { project_id: projectId, document } satisfies BodyOf<"testPolicy">,
      }),
    onMutate: () => setReport(null),
    onSuccess: setReport,
  });
  const create = useMutation({
    mutationFn: () =>
      api<PolicyCreated>("/policies", {
        method: "POST",
        idempotencyKey: createKey.key,
        body: { project_id: projectId, document } satisfies BodyOf<"createPolicy">,
      }),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["policies"] });
      router.push(`/policies/${r.policy.id}?project_id=${projectId}`);
    },
    onSettled: (_d, error) => createKey.settle(error),
  });

  if (projects.isError) return <ErrorState error={projects.error} />;
  const error = create.error ?? test.error;
  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href={`/policies?project_id=${projectId}`} className="text-indigo-700 hover:underline">
            Policies
          </Link>
        }
        title="New policy"
        description="A policy guards one tool. Its rules are evaluated in order and the first that matches decides; its tests must all pass before a version can be activated. Saving stores version 1 and activates nothing."
      />
      {!canWrite ? (
        <div
          role="note"
          className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800"
        >
          Your role cannot write policies{canTest ? "; you can still test a draft" : ""}.
        </div>
      ) : null}
      <Card>
        <CardContent className="space-y-3 pt-4">
          <DocumentEditor id="new-policy-document" value={document} onChange={setDocument} />
          {error ? (
            <>
              <ErrorState error={error} />
              <ProblemList problems={problemsOf(error)} title="Problems in the document" />
            </>
          ) : null}
          <div className="flex flex-wrap gap-2">
            {canTest ? (
              <Button
                size="sm"
                onClick={() => test.mutate()}
                disabled={!projectId || test.isPending || !document.trim()}
              >
                <FlaskConical className="h-4 w-4" aria-hidden="true" />
                {test.isPending ? "Testing…" : "Test the draft"}
              </Button>
            ) : null}
            {canWrite ? (
              <Button
                size="sm"
                variant="primary"
                onClick={() => create.mutate()}
                disabled={!projectId || create.isPending || !document.trim()}
              >
                <Save className="h-4 w-4" aria-hidden="true" />
                {create.isPending ? "Saving…" : "Save the policy"}
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>
      {report ? <TestReportView report={report} /> : null}
    </div>
  );
}
