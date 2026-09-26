import { Badge, type BadgeTone } from "@/components/ui/badge";
import { KeyValue, type KeyValueItem } from "@/components/ui/key-value";
import { EmptyState } from "@/components/ui/states";
import type { ChangeItem } from "@/lib/api/control-plane";
import {
  confidenceLabel,
  itemFacts,
  itemKindLabel,
  itemSubject,
  promptDiff,
  schemaChanges,
} from "@/lib/changes";

const CHANGE_TONE: Record<string, BadgeTone> = { added: "success", removed: "danger", modified: "info" };

const DIFF_LINE: Record<string, string> = {
  "+": "bg-emerald-50 text-emerald-900",
  "-": "bg-rose-50 text-rose-900",
  " ": "text-slate-700",
};

const DIFF_WORD: Record<string, string> = { "+": "added", "-": "removed", " ": "unchanged" };

function PromptDiffView({ item }: { item: ChangeItem }) {
  const diff = promptDiff(item);
  if (!diff) return null;
  if (diff.unavailable) {
    return <p className="text-xs text-slate-600">No line diff: {diff.unavailable}</p>;
  }
  return (
    <div className="space-y-2">
      {diff.lines.length ? (
        <pre
          className="relative overflow-x-auto rounded-md border border-slate-200 text-xs leading-5"
          aria-label="Prompt diff (secrets and personal data masked)"
          data-testid="prompt-diff"
        >
          {diff.lines.map((l, i) => (
            <div key={i} className={`flex px-2 ${DIFF_LINE[l.op]}`} data-op={l.op}>
              <span className="w-4 shrink-0 select-none text-slate-500" aria-hidden="true">
                {l.op}
              </span>
              <span className="sr-only">{DIFF_WORD[l.op]}: </span>
              <span className="whitespace-pre-wrap break-words">{l.text}</span>
            </div>
          ))}
        </pre>
      ) : null}
      {diff.truncated ? <p className="text-xs text-slate-500">The diff is truncated.</p> : null}
      {diff.mentions.length ? (
        <p className="flex flex-wrap items-center gap-1 text-xs text-slate-600">
          Changed lines mention
          {diff.mentions.map((m) => (
            <Badge key={m} tone="brand" className="font-mono">
              {m}
            </Badge>
          ))}
        </p>
      ) : null}
    </div>
  );
}

function SchemaChangesView({ item }: { item: ChangeItem }) {
  const changes = schemaChanges(item);
  if (!changes.length) return null;
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full text-left text-xs" data-testid="schema-changes">
        <caption className="sr-only">Input schema changes of {item.subject}</caption>
        <thead className="text-slate-500">
          <tr>
            <th scope="col" className="py-1 pr-3 font-medium">
              Schema path
            </th>
            <th scope="col" className="py-1 pr-3 font-medium">
              Change
            </th>
            <th scope="col" className="py-1 font-medium">
              Compatibility
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {changes.map((c) => (
            <tr key={`${c.path}-${c.change}`}>
              <td className="py-1 pr-3 font-mono text-slate-800">{c.path}</td>
              <td className="py-1 pr-3 text-slate-700">{c.change}</td>
              <td className="py-1">
                {c.breaking ? <Badge tone="danger">breaking</Badge> : <Badge>compatible</Badge>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChangeItemView({ item }: { item: ChangeItem }) {
  const facts: KeyValueItem[] = itemFacts(item);
  return (
    <li
      className="space-y-2 px-4 py-3"
      data-testid="change-item"
      data-kind={item.kind}
      data-subject={item.subject}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="brand">{itemKindLabel(item.kind)}</Badge>
        <span className="font-mono text-sm text-slate-900" title={item.subject}>
          {itemSubject(item)}
        </span>
        <Badge tone={CHANGE_TONE[item.change] ?? "neutral"}>{item.change}</Badge>
        {item.breaking ? <Badge tone="danger">breaking</Badge> : null}
        <Badge title="How the change is known">{confidenceLabel(item.confidence)}</Badge>
      </div>
      <p className="text-sm text-slate-700">{item.summary}</p>
      {facts.length ? <KeyValue items={facts} className="text-xs" /> : null}
      <PromptDiffView item={item} />
      <SchemaChangesView item={item} />
    </li>
  );
}

export function ChangeItems({ items }: { items: readonly ChangeItem[] }) {
  if (items.length === 0) {
    return (
      <EmptyState title="The two versions are identical">
        Nothing the manifests describe differs between them.
      </EmptyState>
    );
  }
  return (
    <ul className="divide-y divide-slate-100" aria-label="Changes">
      {items.map((item, i) => (
        <ChangeItemView key={`${item.kind}-${item.subject}-${i}`} item={item} />
      ))}
    </ul>
  );
}
