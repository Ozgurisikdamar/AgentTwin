"use client";

import { Plus, Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import { appendIn, getIn, removeAt, setIn, type YamlPath } from "@/lib/scenario-yaml";
import { SEVERITIES } from "@/lib/simulations";

/**
 * Expectation types that take a `tool` parameter, and whether it is required.
 * Mirrors the scenario schema and the evaluators: an optional tool narrows the
 * check to one tool, leaving it out checks every tool (or, for
 * requiredEscalation, the twin's escalation tools).
 */
export const TOOL_PARAMETER: Readonly<Record<string, "required" | "optional">> = {
  toolCalled: "required",
  toolNotCalled: "required",
  toolArgs: "required",
  toolStatus: "required",
  approvalRequired: "required",
  maxToolCalls: "optional",
  maxRetries: "optional",
  requiredEscalation: "optional",
  noIrreversibleAction: "optional",
  noDuplicateSideEffect: "optional",
};

/** The label of the empty choice in a tool select. */
function anyTool(type: string): string {
  if (TOOL_PARAMETER[type] === "required") return "— choose —";
  return type === "requiredEscalation" ? "any escalation tool" : "any tool";
}

export interface ScenarioFormProps {
  text: string;
  value: Record<string, unknown>;
  onChange: (text: string) => void;
  readOnly: boolean;
  /** The name identifies a scenario: it is only set when creating one. */
  nameEditable: boolean;
  agents: readonly string[];
  twins: readonly string[];
  tools: readonly { name: string; risk: string }[];
  faultTypes: readonly string[];
  expectationTypes: readonly string[];
}

/** Options for a select, always including the current value. */
function withCurrent(options: readonly string[], current: unknown): string[] {
  const cur = typeof current === "string" ? current : "";
  return cur && !options.includes(cur) ? [cur, ...options] : [...options];
}

/**
 * A text input that edits locally and commits on blur or Enter. Used where
 * committing every keystroke would fight the user (comma lists, numbers).
 */
function DraftInput({
  id,
  value,
  onCommit,
  disabled,
  ...rest
}: {
  id: string;
  value: string;
  onCommit: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
  inputMode?: "numeric" | "text";
  "aria-describedby"?: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft !== null && draft !== value) onCommit(draft);
    setDraft(null);
  };
  return (
    <Input
      id={id}
      value={draft ?? value}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        }
      }}
      {...rest}
    />
  );
}

function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="mt-1 text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <fieldset className="space-y-3 rounded-md border border-slate-200 p-3">
      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</legend>
      {children}
      {actions ? <div className="flex gap-2">{actions}</div> : null}
    </fieldset>
  );
}

const textareaClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-indigo-200 disabled:bg-slate-50";

export function ScenarioForm(props: ScenarioFormProps) {
  const { text, value, onChange, readOnly } = props;
  const get = (path: YamlPath) => getIn(value, path);
  const str = (path: YamlPath) => {
    const v = get(path);
    return typeof v === "string" ? v : v === undefined || v === null ? "" : String(v);
  };
  const edit = (path: YamlPath, v: unknown) => onChange(setIn(text, path, v));
  const faults = (get(["spec", "faults"]) as Record<string, unknown>[] | undefined) ?? [];
  const expectations = (get(["spec", "expectations"]) as Record<string, unknown>[] | undefined) ?? [];
  const context = (get(["spec", "input", "context"]) as Record<string, unknown> | undefined) ?? {};
  const otherContext = Object.keys(context).filter((k) => k !== "customer_id" && k !== "tenant");
  const toolNames = props.tools.map((t) => t.name);
  const tags = get(["metadata", "tags"]);

  return (
    <div className="space-y-4" data-testid="scenario-form">
      <Section title="Scenario">
        <div className="grid gap-3 md:grid-cols-2">
          <Field
            id="f-name"
            label="Name"
            hint={
              props.nameEditable ? "Lowercase letters, digits, - and _." : "The name identifies the scenario."
            }
          >
            <Input
              id="f-name"
              value={str(["metadata", "name"])}
              readOnly={!props.nameEditable}
              disabled={readOnly}
              aria-describedby="f-name-hint"
              onChange={(e) => edit(["metadata", "name"], e.target.value)}
            />
          </Field>
          <Field id="f-severity" label="Severity" hint="Critical failures block a release.">
            <Select
              id="f-severity"
              value={str(["metadata", "severity"])}
              disabled={readOnly}
              aria-describedby="f-severity-hint"
              onChange={(e) => edit(["metadata", "severity"], e.target.value)}
            >
              {withCurrent(SEVERITIES, get(["metadata", "severity"])).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </Field>
          <Field id="f-tags" label="Tags" hint="Comma-separated, e.g. refunds, faults.">
            <DraftInput
              id="f-tags"
              value={Array.isArray(tags) ? tags.join(", ") : ""}
              disabled={readOnly}
              aria-describedby="f-tags-hint"
              onCommit={(v) =>
                edit(
                  ["metadata", "tags"],
                  v
                    .split(",")
                    .map((t) => t.trim())
                    .filter(Boolean),
                )
              }
            />
          </Field>
          <Field id="f-owner" label="Owner">
            <Input
              id="f-owner"
              value={str(["metadata", "owner"])}
              disabled={readOnly}
              onChange={(e) => edit(["metadata", "owner"], e.target.value)}
            />
          </Field>
        </div>
        <Field id="f-description" label="Description">
          <textarea
            id="f-description"
            rows={3}
            className={textareaClass}
            value={str(["metadata", "description"])}
            placeholder="What this scenario proves, in one or two sentences."
            disabled={readOnly}
            onChange={(e) => edit(["metadata", "description"], e.target.value)}
          />
        </Field>
      </Section>

      <Section title="Agent and input">
        <div className="grid gap-3 md:grid-cols-2">
          <Field id="f-agent" label="Agent">
            <Select
              id="f-agent"
              value={str(["spec", "agent"])}
              disabled={readOnly}
              onChange={(e) => edit(["spec", "agent"], e.target.value)}
            >
              <option value="">— none —</option>
              {withCurrent(props.agents, get(["spec", "agent"])).map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </Select>
          </Field>
          <Field id="f-twin" label="Tool twin">
            <Select
              id="f-twin"
              value={str(["spec", "twin"])}
              disabled={readOnly}
              onChange={(e) => edit(["spec", "twin"], e.target.value)}
            >
              <option value="">— none —</option>
              {withCurrent(props.twins, get(["spec", "twin"])).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field id="f-message" label="Customer message">
          <textarea
            id="f-message"
            rows={3}
            className={textareaClass}
            value={str(["spec", "input", "message"])}
            placeholder="What the customer says to the agent, word for word."
            disabled={readOnly}
            onChange={(e) => edit(["spec", "input", "message"], e.target.value)}
          />
        </Field>
        <div className="grid gap-3 md:grid-cols-2">
          <Field id="f-customer" label="Customer id (context)">
            <Input
              id="f-customer"
              value={str(["spec", "input", "context", "customer_id"])}
              disabled={readOnly}
              onChange={(e) => edit(["spec", "input", "context", "customer_id"], e.target.value)}
            />
          </Field>
          <Field id="f-tenant" label="Tenant (context)">
            <Input
              id="f-tenant"
              value={str(["spec", "input", "context", "tenant"])}
              disabled={readOnly}
              onChange={(e) => edit(["spec", "input", "context", "tenant"], e.target.value)}
            />
          </Field>
        </div>
        {otherContext.length ? (
          <p className="text-xs text-slate-500">More context in the YAML: {otherContext.join(", ")}.</p>
        ) : null}
      </Section>

      <Section
        title={`Faults (${faults.length})`}
        actions={
          readOnly ? null : (
            <Button
              size="sm"
              onClick={() =>
                onChange(
                  appendIn(text, ["spec", "faults"], {
                    target: toolNames[0] ?? "tool_name",
                    behavior: { type: props.faultTypes[0] ?? "timeout_after_mutation" },
                  }),
                )
              }
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Add fault
            </Button>
          )
        }
      >
        {faults.length === 0 ? <p className="text-xs text-slate-500">The twin behaves normally.</p> : null}
        {faults.map((f, i) => {
          const callNumber = getIn(f, ["when", "callNumber"]);
          return (
            <div
              key={i}
              className="grid items-end gap-2 rounded border border-slate-100 p-2 md:grid-cols-[1fr_1fr_8rem_auto]"
              data-testid="fault-editor"
            >
              <Field id={`f-fault-${i}-target`} label="Tool">
                <Select
                  id={`f-fault-${i}-target`}
                  value={String(f.target ?? "")}
                  disabled={readOnly}
                  onChange={(e) => edit(["spec", "faults", i, "target"], e.target.value)}
                >
                  {withCurrent(toolNames, f.target).map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id={`f-fault-${i}-type`} label="Fault">
                <Select
                  id={`f-fault-${i}-type`}
                  value={String(getIn(f, ["behavior", "type"]) ?? "")}
                  disabled={readOnly}
                  onChange={(e) => edit(["spec", "faults", i, "behavior", "type"], e.target.value)}
                >
                  {withCurrent(props.faultTypes, getIn(f, ["behavior", "type"])).map((t) => (
                    <option key={t} value={t}>
                      {t.replaceAll("_", " ")}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id={`f-fault-${i}-call`} label="On call #">
                <DraftInput
                  id={`f-fault-${i}-call`}
                  inputMode="numeric"
                  placeholder="every"
                  value={typeof callNumber === "number" ? String(callNumber) : ""}
                  disabled={readOnly}
                  onCommit={(v) => {
                    const n = Number.parseInt(v, 10);
                    edit(
                      ["spec", "faults", i, "when", "callNumber"],
                      Number.isInteger(n) && n > 0 ? n : undefined,
                    );
                  }}
                />
              </Field>
              {readOnly ? null : (
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`Remove fault ${i + 1}`}
                  onClick={() => onChange(removeAt(text, ["spec", "faults"], i))}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </Button>
              )}
            </div>
          );
        })}
      </Section>

      <Section
        title={`Expectations (${expectations.length})`}
        actions={
          readOnly ? null : (
            <Button
              size="sm"
              onClick={() =>
                onChange(
                  appendIn(text, ["spec", "expectations"], {
                    id: `expectation-${expectations.length + 1}`,
                    type: "toolCalled",
                    tool: toolNames[0] ?? "tool_name",
                  }),
                )
              }
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Add expectation
            </Button>
          )
        }
      >
        {expectations.map((x, i) => {
          const type = String(x.type ?? "");
          const takesTool = type in TOOL_PARAMETER;
          const shown = takesTool ? ["id", "type", "critical", "tool"] : ["id", "type", "critical"];
          const extra = Object.keys(x).filter((k) => !shown.includes(k));
          return (
            <div
              key={i}
              className="space-y-2 rounded border border-slate-100 p-2"
              data-testid="expectation-editor"
            >
              <div className="grid items-end gap-2 sm:grid-cols-2 lg:grid-cols-3">
                <Field id={`f-exp-${i}-id`} label="Id">
                  <Input
                    id={`f-exp-${i}-id`}
                    value={String(x.id ?? "")}
                    disabled={readOnly}
                    onChange={(e) => edit(["spec", "expectations", i, "id"], e.target.value)}
                  />
                </Field>
                <Field id={`f-exp-${i}-type`} label="Type">
                  <Select
                    id={`f-exp-${i}-type`}
                    value={type}
                    disabled={readOnly}
                    onChange={(e) => edit(["spec", "expectations", i, "type"], e.target.value)}
                  >
                    {withCurrent(props.expectationTypes, type).map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </Select>
                </Field>
                {takesTool ? (
                  <Field id={`f-exp-${i}-tool`} label="Tool">
                    <Select
                      id={`f-exp-${i}-tool`}
                      value={String(x.tool ?? "")}
                      disabled={readOnly}
                      onChange={(e) => edit(["spec", "expectations", i, "tool"], e.target.value)}
                    >
                      <option value="">{anyTool(type)}</option>
                      {withCurrent(toolNames, x.tool).map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                  </Field>
                ) : null}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-slate-500">
                  {extra.length ? `Also set in the YAML: ${extra.join(", ")}.` : null}
                </p>
                <div className="flex items-center gap-1">
                  <label className="flex h-8 items-center gap-2 px-1 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-indigo-600"
                      checked={x.critical === true}
                      disabled={readOnly}
                      onChange={(e) =>
                        edit(["spec", "expectations", i, "critical"], e.target.checked || undefined)
                      }
                    />
                    Critical
                  </label>
                  {readOnly ? null : (
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={`Remove expectation ${String(x.id ?? i + 1)}`}
                      onClick={() => onChange(removeAt(text, ["spec", "expectations"], i))}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </Section>
    </div>
  );
}
