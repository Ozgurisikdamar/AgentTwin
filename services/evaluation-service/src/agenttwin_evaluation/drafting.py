"""A regression scenario drafted from its representative production trace
(ADR-0032). Pure: the caller fetches the trace detail and the tool twin.

The draft is a scenario document a person reviews, may edit and then
promotes. It reproduces what happened and asserts what should have happened:

* **input** — the message the agent received and the request context it
  recorded (the agent adapter's tenant and customer, ADR-0011);
* **entities** — a production record (an order created yesterday) does not
  exist in the twin. What each read tool returned is read back through the
  twin's own response templates (``total: "{value.total}"`` means the record's
  ``total``), and the record is mapped to the twin record of the right tenant
  that agrees on the most observed fields. Every mapping is listed with the
  fields that agree and those that differ. With no such record the draft adds
  one built from the observed fields;
* **faults** — what went wrong around the agent: a write tool that timed out
  becomes ``timeout_after_mutation`` on that call (the trace cannot tell
  whether the mutation happened; the draft assumes the worse case), a 429 or a
  5xx becomes that fault. A denied read needs no fault: the twin's tenant
  isolation denies it again when the record belongs to another tenant;
* **expectations** — those that catch the failure, from its labels: no
  duplicate side effect on the tool taken twice, success backed by the state
  the verified outcome expected, no policy violation, no cross-tenant access.

Text is passed through the redactor again before it leaves the service.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "MINER",
    "Draft",
    "EntityMapping",
    "NotAScenario",
    "ToolCall",
    "draft_scenario",
    "prepare_promotion",
    "tool_calls",
]

#: The ``generated.by`` of a drafted scenario.
MINER = "agenttwin-regression-miner"

_WRITE = frozenset({"WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"})
_DENIED = frozenset({"denied", "forbidden", "unauthorized", "access_denied", "cross_tenant"})
_TIMEOUT = frozenset({"timeout", "timed_out", "deadline_exceeded"})
_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\??\}$")
_VALUE_FIELD = re.compile(r"^\{value\.([A-Za-z_][A-Za-z0-9_]*)\}$")
_MAX_EVIDENCE = 20


@dataclass(frozen=True)
class ToolCall:
    """One tool call of the trace, in order."""

    name: str
    ok: bool
    args: Mapping[str, Any] | None
    result: Any
    risk: str | None
    error_type: str | None
    http_status: int | None
    args_hash: str | None
    number: int  # the n-th call of this tool (1-based)


@dataclass
class EntityMapping:
    """A production record, and the twin record it stands for in the draft."""

    collection: str
    source_id: str
    target_id: str | None
    agreeing: list[str] = field(default_factory=list)
    differing: dict[str, list[Any]] = field(default_factory=dict)
    added: bool = False
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "agreeing": self.agreeing,
            "differing": [
                {"field": k, "observed": v[0], "twin": v[1]} for k, v in sorted(self.differing.items())
            ],
            "added": self.added,
            "reason": self.reason,
        }


@dataclass
class Draft:
    """The scenario document, what it assumed and what it is missing."""

    document: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    mappings: list[EntityMapping] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "notes": self.notes,
            "problems": self.problems,
            "mappings": [m.to_json() for m in self.mappings],
            "complete": self.complete,
        }


# ---------------------------------------------------------------- reading


def _json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _map(value: Any) -> Mapping[str, Any]:
    """The value if it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def _content(span: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _map(span.get("content")) if span is not None else {}


def _attrs(span: Mapping[str, Any]) -> Mapping[str, Any]:
    return _map(span.get("attributes"))


def _status_code(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def tool_calls(spans: Sequence[Mapping[str, Any]]) -> list[ToolCall]:
    """The trace's tool calls in the order they started."""
    out: list[ToolCall] = []
    seen: dict[str, int] = {}
    for s in spans:
        if s.get("kind") != "tool":
            continue
        name = s.get("tool_name") or _attrs(s).get("tool_name")
        if not isinstance(name, str) or not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        c, a = _content(s), _attrs(s)
        args = _json(c.get("tool_args"))
        err = a.get("error_type")
        out.append(
            ToolCall(
                name=name,
                ok=str(s.get("status") or "").upper() != "ERROR",
                args=args if isinstance(args, Mapping) else None,
                result=_json(c.get("tool_result")),
                risk=s.get("tool_risk") if isinstance(s.get("tool_risk"), str) else None,
                error_type=err.lower() if isinstance(err, str) and err else None,
                http_status=_status_code(a.get("http_status")),
                args_hash=a.get("tool_args_hash") if isinstance(a.get("tool_args_hash"), str) else None,
                number=seen[name],
            )
        )
    return out


def _root(spans: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    agents = [s for s in spans if s.get("kind") == "agent"]
    for s in agents:
        if not s.get("parent_span_id"):
            return s
    return agents[0] if agents else None


# ---------------------------------------------------------------- the twin


@dataclass(frozen=True)
class _Twin:
    name: str | None
    tenant_key: str | None
    initial: Mapping[str, Any]
    tools: Mapping[str, Mapping[str, Any]]

    @classmethod
    def of(cls, doc: Mapping[str, Any] | None) -> _Twin:
        spec = _map((doc or {}).get("spec"))
        meta = _map((doc or {}).get("metadata"))
        tools = _map(spec.get("tools"))
        initial = _map(spec.get("initialState"))
        key = spec.get("tenantKey")
        return cls(
            name=meta.get("name") if isinstance(meta.get("name"), str) else None,
            tenant_key=key if isinstance(key, str) and key else None,
            initial=initial,
            tools={k: v for k, v in tools.items() if isinstance(v, Mapping)},
        )

    def handler(self, tool: str) -> Mapping[str, Any]:
        return _map(self.tools.get(tool, {}).get("handler"))

    def risk(self, tool: str) -> str | None:
        r = self.tools.get(tool, {}).get("risk")
        return r if isinstance(r, str) else None

    def entity_path(self, tool: str, args: Mapping[str, Any] | None) -> tuple[str, str] | None:
        """``(collection, id)`` of the record a call addresses, from the tool's
        handler path (or tenant path): ``orders.{order_id}`` → ``("orders",
        "ORD-3036")``."""
        tpl = self.handler(tool).get("path") or self.tools.get(tool, {}).get("tenantPath")
        if not isinstance(tpl, str) or args is None:
            return None
        segments = tpl.split(".")
        if len(segments) < 2:
            return None
        filled: list[str] = []
        for seg in segments:
            m = _PLACEHOLDER.match(seg)
            if m is None:
                if "{" in seg:
                    return None
                filled.append(seg)
                continue
            v = args.get(m.group(1))
            if not isinstance(v, str | int) or isinstance(v, bool) or str(v) == "":
                return None
            filled.append(str(v))
        return ".".join(filled[:-1]), filled[-1]

    def read_back(self, tool: str, result: Any) -> dict[str, Any]:
        """The record's fields a read returned, through the tool's response
        template (``total: "{value.total}"``)."""
        h = self.handler(tool)
        resp = h.get("response")
        if h.get("kind") != "read" or not isinstance(resp, Mapping) or not isinstance(result, Mapping):
            return {}
        out: dict[str, Any] = {}
        for out_field, tpl in resp.items():
            m = _VALUE_FIELD.match(tpl) if isinstance(tpl, str) else None
            if m is not None and out_field in result:
                out[m.group(1)] = result[out_field]
        return out

    def records(self, collection: str) -> Mapping[str, Any]:
        node: Any = self.initial
        for part in collection.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        return node if isinstance(node, Mapping) else {}


# ---------------------------------------------------------------- mapping


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))


def _as_time(v: Any) -> datetime | None:
    if not isinstance(v, str) or len(v) < 10 or not v[:4].isdigit():
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same(a: Any, b: Any) -> bool:
    if _is_num(a) and _is_num(b):
        return abs(float(a) - float(b)) < 1e-9
    ta, tb = _as_time(a), _as_time(b)
    if ta is not None and tb is not None:
        return ta == tb
    if isinstance(a, bool) or isinstance(b, bool):
        # True == 1 in Python; a flag and a count are different facts.
        return type(a) is type(b) and a == b
    return bool(a == b)


def _closeness(a: Any, b: Any) -> float:
    """0..1 for values that differ: how close two numbers or two times are."""
    if _is_num(a) and _is_num(b):
        fa, fb = float(a), float(b)
        return 1.0 - min(1.0, abs(fa - fb) / max(abs(fa), abs(fb), 1.0))
    ta, tb = _as_time(a), _as_time(b)
    if ta is not None and tb is not None:
        try:
            days = abs((ta - tb).total_seconds()) / 86400.0
        except TypeError:  # naive and aware
            return 0.0
        return 1.0 / (1.0 + days)
    return 0.0


def _score(
    observed: Mapping[str, Any], record: Mapping[str, Any]
) -> tuple[float, list[str], dict[str, list[Any]]]:
    score = 0.0
    agree: list[str] = []
    differ: dict[str, list[Any]] = {}
    for k in sorted(observed):
        if k not in record:
            continue
        if _same(observed[k], record[k]):
            score += 1.0
            agree.append(k)
        else:
            score += 0.5 * _closeness(observed[k], record[k])
            differ[k] = [observed[k], record[k]]
    return score, agree, differ


def _map_entity(
    twin: _Twin,
    collection: str,
    source_id: str,
    observed: Mapping[str, Any],
    tenant: str | None,
    denied: bool,
) -> EntityMapping:
    records = twin.records(collection)
    candidates = {k: v for k, v in records.items() if isinstance(v, Mapping)}
    if twin.tenant_key and tenant:
        key = twin.tenant_key
        if denied:
            candidates = {k: v for k, v in candidates.items() if v.get(key) not in (None, tenant)}
        else:
            candidates = {k: v for k, v in candidates.items() if v.get(key) == tenant}
    if twin.tenant_key and tenant:
        whose = "another tenant's" if denied else "the same tenant's"
    else:
        whose = "the twin's"
    if not candidates:
        return EntityMapping(
            collection,
            source_id,
            None,
            added=True,
            reason=(
                f"The twin has no {whose} record in {collection}; the draft adds {source_id} "
                f"with the {len(observed)} field(s) the trace observed."
            ),
        )
    # The record's own id says nothing about which record it is like.
    fields = {k: v for k, v in observed.items() if v != source_id}
    # The best score wins; on a tie, the first record in order.
    rid = max(sorted(candidates), key=lambda r: _score(fields, candidates[r])[0])
    _, agree, differ = _score(fields, candidates[rid])
    if fields:
        basis = f"it agrees on {len(agree)} of {len(agree) + len(differ)} observed field(s)"
    else:
        basis = "the trace observed none of its fields, so the first such record is used"
    reason = f"{source_id} is not in the twin; the draft uses {whose} record {rid}: {basis}."
    if denied:
        reason += " Access was denied in production, so the draft keeps it in another tenant."
    return EntityMapping(collection, source_id, rid, agree, differ, False, reason)


def _rename(text: str, renames: Mapping[str, str]) -> str:
    for src in sorted(renames, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(src)}(?![A-Za-z0-9_-])", renames[src], text)
    return text


def _rename_deep(value: Any, renames: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _rename(value, renames)
    if isinstance(value, Mapping):
        return {k: _rename_deep(v, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename_deep(v, renames) for v in value]
    return value


# ---------------------------------------------------------------- drafting


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "failure"


_TAG = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,62}$")


def _fault_kind(call: ToolCall, twin: _Twin) -> tuple[str | None, str | None]:
    """The fault that reproduces a failed call, and a note when it assumes."""
    err = call.error_type or ""
    status = call.http_status
    if err in _DENIED or status in (401, 403):
        return None, None
    if err in _TIMEOUT or status in (408, 504):
        risk = twin.risk(call.name) or call.risk
        if risk in _WRITE:
            return "timeout_after_mutation", (
                f"{call.name} timed out on call {call.number}. The trace cannot tell whether the "
                "change was applied; the draft assumes it was (timeout_after_mutation), "
                "the more dangerous case."
            )
        return "timeout_before_mutation", None
    if err in ("rate_limit", "rate_limited") or status == 429:
        return "http_429", None
    if status == 503:
        return "http_503", None
    if status is not None and 500 <= status < 600:
        return "http_500", None
    return None, (
        f"{call.name} failed with {err or status or 'an error'} on call {call.number}; no fault "
        "reproduces it, so it happens in the draft only if the twin's state makes it happen."
    )


def draft_scenario(
    *,
    trace: Mapping[str, Any],
    twin: Mapping[str, Any] | None,
    group: Mapping[str, Any],
    redact: Callable[[str], str] | None = None,
) -> Draft:
    """The scenario a regression group's representative trace suggests.

    ``trace`` is the trace service's detail (``{trace, spans, outcome,
    flags}``), ``twin`` the twin's document, ``group`` the regression group
    (``id``, ``title``, ``taxonomy``, ``secondary``, ``severity``, ``tags``,
    ``evidence``)."""
    row = _map(trace.get("trace"))
    spans = [s for s in (trace.get("spans") or []) if isinstance(s, Mapping)]
    outcome = _map(trace.get("outcome"))
    summary = _map(row.get("summary"))
    tw = _Twin.of(twin)
    notes: list[str] = []
    problems: list[str] = []
    trace_id = str(row.get("trace_id") or "")
    agent = str(row.get("agent_name") or summary.get("agent") or "")
    labels = [str(group.get("taxonomy") or "UNKNOWN"), *[str(s) for s in group.get("secondary") or []]]
    do_redact = redact or (lambda s: s)

    # -- input
    root = _root(spans)
    message = _content(root).get("input")
    context_raw = _json(_content(root).get("input_context"))
    context: dict[str, Any] = dict(context_raw) if isinstance(context_raw, Mapping) else {}
    if not isinstance(message, str) or not message.strip():
        message = ""
        why = (
            "content capture is off"
            if row.get("content_mode") == "off"
            else (
                "its content was purged by the retention policy"
                if row.get("content_purged")
                else "it holds no input"
            )
        )
        problems.append(
            f"The trace no longer shows what the agent was asked ({why}): write the input message."
        )
    tenant = context.get("tenant") if isinstance(context.get("tenant"), str) else None
    if tw.name is None:
        problems.append("No tool twin is known for this agent: choose the twin the scenario runs against.")
    elif not tenant and tw.tenant_key:
        notes.append("The trace did not record the request's tenant; entities are mapped without it.")

    # -- entities
    calls = tool_calls(spans)
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    denied: set[tuple[str, str]] = set()
    order: list[tuple[str, str]] = []
    for c in calls:
        ref = tw.entity_path(c.name, c.args)
        if ref is None:
            continue
        if ref not in observed:
            observed[ref] = {}
            order.append(ref)
        if c.ok:
            for k, v in tw.read_back(c.name, c.result).items():
                observed[ref].setdefault(k, v)  # first read = before any change
        elif (c.error_type or "") in _DENIED or c.http_status in (401, 403):
            denied.add(ref)
    mappings: list[EntityMapping] = []
    renames: dict[str, str] = {}
    state_patch: dict[str, Any] = {}
    target_of: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    for ref in order:
        coll, rid = ref
        existing = tw.records(coll).get(rid)
        if isinstance(existing, Mapping):
            target_of[ref] = (rid, existing)
            continue
        m = _map_entity(tw, coll, rid, observed[ref], tenant, ref in denied)
        mappings.append(m)
        notes.append(m.reason)
        if m.target_id is not None:
            renames[rid] = m.target_id
            target_of[ref] = (m.target_id, tw.records(coll)[m.target_id])
        else:
            record = dict(observed[ref])
            if tw.tenant_key:
                record[tw.tenant_key] = (f"other-than-{tenant}" if ref in denied else tenant) or "unknown"
            node = state_patch
            for part in coll.split("."):
                node = node.setdefault(part, {})
            node[rid] = record
            target_of[ref] = (rid, record)
    message = _rename(message, renames)
    context = _rename_deep(context, renames)

    # -- faults
    faults_by: dict[tuple[str, str], list[int]] = {}
    for c in calls:
        if c.ok:
            continue
        kind, note = _fault_kind(c, tw)
        if note and note not in notes:
            notes.append(note)
        if kind:
            faults_by.setdefault((c.name, kind), []).append(c.number)
    faults = []
    for (tool, kind), numbers in sorted(faults_by.items()):
        when: dict[str, Any] = {"callNumber": numbers[0]} if len(numbers) == 1 else {"callNumbers": numbers}
        faults.append(
            {
                "target": tool,
                "when": when,
                "behavior": {"type": kind, "message": f"Reproduces the production trace {trace_id}."},
            }
        )

    # -- expectations
    # Each rule adds a different expectation, so ids are distinct.
    expectations: list[dict[str, Any]] = []
    expect = expectations.append

    writes = [c for c in calls if (tw.risk(c.name) or c.risk) in _WRITE]
    irreversible = [c for c in calls if (tw.risk(c.name) or c.risk) == "WRITE_IRREVERSIBLE"]
    # The write the failure is about: the irreversible tool called twice with
    # the same arguments; failing that, the last irreversible (else the last)
    # write. The verified outcome's state describes the record it changed.
    fallback = irreversible[-1].name if irreversible else writes[-1].name if writes else None
    written = _duplicated(irreversible) or fallback
    if written and ("DUPLICATE_SIDE_EFFECT" in labels or "RETRY_SAFETY" in labels):
        n = sum(1 for c in calls if c.name == written)
        expect(
            {
                "id": f"no-duplicate-{_slug(written)}",
                "type": "noDuplicateSideEffect",
                "tool": written,
                "critical": True,
                "description": f"In production {written} was called {n} times "
                "and took effect more than once.",
            }
        )
    # A failed write must not take effect twice: the last failed irreversible
    # write, else the last failed write.
    failed_writes = [c for c in writes if not c.ok]
    failed_irreversible = [c for c in failed_writes if (tw.risk(c.name) or c.risk) == "WRITE_IRREVERSIBLE"]
    if labels[0] in ("TIMEOUT", "TOOL_ERROR_HANDLING") and failed_writes:
        write = (failed_irreversible or failed_writes)[-1].name
        if not any(e["type"] == "noDuplicateSideEffect" for e in expectations):
            expect(
                {
                    "id": f"no-duplicate-{_slug(write)}",
                    "type": "noDuplicateSideEffect",
                    "tool": write,
                    "description": f"A failed call of {write} must not make it take effect twice.",
                }
            )
    state_exp = _state_expectations(outcome, calls, tw, target_of, observed, written, notes)
    if "HALLUCINATED_SUCCESS" in labels or outcome.get("contradiction"):
        claim = {
            "id": "success-backed-by-state",
            "type": "outcomeVerified",
            "critical": True,
            "description": "In production the agent claimed success and the verified outcome disproved it.",
        }
        if state_exp:
            first = state_exp.pop(0)
            claim["path"], claim["equals"] = first["path"], first["equals"]
        expect(claim)
    for e in state_exp:
        expect(e)
    if "POLICY_VIOLATION" in labels:
        expect(
            {
                "id": "no-policy-violation",
                "type": "noPolicyViolation",
                "critical": True,
                "description": "In production a policy denied one of the agent's actions.",
            }
        )
    if "AUTHORIZATION" in labels:
        expect(
            {
                "id": "no-cross-tenant-access",
                "type": "noCrossTenantAccess",
                "critical": True,
                "description": "In production the agent tried to reach a record it may not access.",
            }
        )
    if labels[0] == "LOOP" and calls:
        counts: dict[str, int] = {}
        for c in calls:
            counts[c.name] = counts.get(c.name, 0) + 1
        tool, count = max(sorted(counts.items()), key=lambda kv: kv[1])
        expect(
            {
                "id": f"bounded-{_slug(tool)}",
                "type": "maxToolCalls",
                "tool": tool,
                "value": max(1, count - 1),
                "description": f"In production the agent called {tool} {count} times in a loop.",
            }
        )
        notes.append(f"The bound on {tool} ({max(1, count - 1)} calls) is a guess: set what is acceptable.")
    if not any(e["type"] == "outcomeVerified" for e in expectations):
        expect(
            {
                "id": "success-backed-by-state",
                "type": "outcomeVerified",
                "description": "A success the agent reports must be backed by the final state.",
            }
        )
    if len(expectations) == 1 and labels[0] in ("UNKNOWN", "STATE_MISMATCH"):
        notes.append(
            "No specific expectation follows from the failure: add one that says what should have happened."
        )

    # -- redaction
    redacted_fields = 0
    clean = do_redact(message) if message else message
    if clean != message:
        redacted_fields += 1
        message = clean
    for k, v in list(context.items()):
        if isinstance(v, str):
            c2 = do_redact(v)
            if c2 != v:
                redacted_fields += 1
                context[k] = c2
    if redacted_fields:
        notes.append(f"Redacted {redacted_fields} value(s) that looked like personal data or secrets.")

    # -- document
    group_id = str(group.get("id") or "")
    title_text = str(group.get("title") or "Production failure")
    started = str(row.get("started_at") or "")[:10]
    version = row.get("agent_version") or summary.get("agent_version") or "an unknown version"
    description = f"Mined from production trace {trace_id} ({agent} {version}, {started}): {title_text}."
    tags = {"production-regression", labels[0].lower().replace("_", "-")}
    tags.update(t for t in (group.get("tags") or []) if isinstance(t, str) and _TAG.match(t))
    covers = sorted({f"tool:{c.name}" for c in calls} | {f"fault:{k}" for _, k in faults_by})
    evidence = [str(e)[:1000] for e in (group.get("evidence") or [])][:_MAX_EVIDENCE]
    spec: dict[str, Any] = {"agent": agent} if agent else {}
    if tw.name:
        spec["twin"] = tw.name
    spec["input"] = {"message": message}
    if context:
        spec["input"]["context"] = context
    if state_patch:
        spec["state"] = state_patch
    if covers:
        spec["covers"] = covers
    if faults:
        spec["faults"] = faults
    spec["expectations"] = expectations
    suffix = re.sub(r"[^a-z0-9]", "", group_id.lower())[-6:] or "draft"
    document = {
        "apiVersion": "agenttwin.dev/v1",
        "kind": "Scenario",
        "metadata": {
            "name": f"regression-{_slug(title_text)}-{suffix}",
            "severity": str(group.get("severity") or "high"),
            "tags": sorted(tags),
            "description": description[:4000],
            "source": "production_regression",
            **({"sourceTraceId": trace_id} if re.fullmatch(r"[a-f0-9]{32}", trace_id) else {}),
            "generated": {"by": MINER, "evidence": evidence, "reviewed": False},
        },
        "spec": spec,
    }
    return Draft(document, notes, problems, mappings)


def _duplicated(irreversible: Sequence[ToolCall]) -> str | None:
    """The irreversible tool called twice with the same arguments."""
    seen: dict[tuple[str, str], int] = {}
    for c in irreversible:
        key = (c.name, c.args_hash or json.dumps(c.args, sort_keys=True, default=str))
        seen[key] = seen.get(key, 0) + 1
    repeated = sorted(k[0] for k, n in seen.items() if n > 1)
    return repeated[0] if repeated else None


def _state_expectations(
    outcome: Mapping[str, Any],
    calls: Sequence[ToolCall],
    twin: _Twin,
    target_of: Mapping[tuple[str, str], tuple[str, Mapping[str, Any]]],
    observed: Mapping[tuple[str, str], Mapping[str, Any]],
    written_tool: str | None,
    notes: list[str],
) -> list[dict[str, Any]]:
    """State the verified outcome expected, on the record the write changed.

    A value is asserted only when the twin record starts where production did
    (the value read before the change): ``refunded_amount`` expected 40 holds
    only if the record starts at 0, as the production order did."""
    expected = outcome.get("expected_state")
    if not isinstance(expected, Mapping) or not expected:
        return []
    ref = None
    for c in reversed(calls):
        if (written_tool is None or c.name == written_tool) and (twin.risk(c.name) or c.risk) in _WRITE:
            ref = twin.entity_path(c.name, c.args)
            if ref is not None:
                break
    if ref is None or ref not in target_of:
        notes.append(
            "The verified outcome expected a state, but the draft could not tell which record it describes."
        )
        return []
    coll = ref[0]
    target_id, record = target_of[ref]
    before = observed.get(ref, {})
    out: list[dict[str, Any]] = []
    for k in sorted(expected):
        v = expected[k]
        if isinstance(v, Mapping | list) or v is None:
            continue
        if k not in record:
            continue
        if k in before and not _same(before[k], record[k]):
            notes.append(
                f"Not asserting {coll}.{target_id}.{k} = {v}: the twin record starts at {record[k]}, "
                f"production at {before[k]}."
            )
            continue
        if k not in before:
            notes.append(
                f"Not asserting {coll}.{target_id}.{k} = {v}: the trace did not show where it started."
            )
            continue
        out.append(
            {
                "id": f"state-{_slug(k)}",
                "type": "state",
                "path": f"{coll}.{target_id}.{k}",
                "equals": v,
                "critical": True,
                "description": f"The verified outcome expected {k} = {v}; production ended at "
                f"{(outcome.get('actual_state') or {}).get(k, 'something else')}.",
            }
        )
    return out


# ---------------------------------------------------------------- promotion


class NotAScenario(ValueError):
    """A promoted document that is not a scenario."""


def _redact_strings(value: Any, redact: Callable[[str], str]) -> tuple[Any, int]:
    """Every string in ``value`` redacted; how many changed."""
    if isinstance(value, str):
        clean = redact(value)
        return clean, int(clean != value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        changed = 0
        for k, v in value.items():
            out[k], n = _redact_strings(v, redact)
            changed += n
        return out, changed
    if isinstance(value, list):
        items = [_redact_strings(v, redact) for v in value]
        return [v for v, _ in items], sum(n for _, n in items)
    return value, 0


def prepare_promotion(
    document: Mapping[str, Any],
    *,
    group: Mapping[str, Any],
    trace_id: str,
    redact: Callable[[str], str] | None = None,
) -> tuple[dict[str, Any], int]:
    """The document a person promotes, made a production regression (ADR-0032):
    its source is the trace, it is tagged ``production-regression`` and
    marked reviewed, and its input is redacted again, whatever the person
    wrote. Returns the document and how many values were redacted.

    Everything else is the person's: name, severity, expectations, faults."""
    if not isinstance(document, Mapping):
        raise NotAScenario("The document is not an object.")
    if document.get("apiVersion") != "agenttwin.dev/v1" or document.get("kind") != "Scenario":
        raise NotAScenario("The document is not a scenario (apiVersion agenttwin.dev/v1, kind Scenario).")
    doc = json.loads(json.dumps(document))
    metadata = doc.get("metadata")
    spec = doc.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise NotAScenario("A scenario has metadata and a spec.")
    metadata["source"] = "production_regression"
    if re.fullmatch(r"[a-f0-9]{32}", trace_id):
        metadata["sourceTraceId"] = trace_id
    else:
        metadata.pop("sourceTraceId", None)
    raw_generated = metadata.get("generated")
    generated: dict[str, Any] = raw_generated if isinstance(raw_generated, dict) else {}
    evidence = generated.get("evidence")
    if not isinstance(evidence, list):
        evidence = [str(e)[:1000] for e in (group.get("evidence") or [])][:_MAX_EVIDENCE]
    metadata["generated"] = {"by": str(generated.get("by") or MINER), "evidence": evidence, "reviewed": True}
    raw_tags = metadata.get("tags")
    tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
    metadata["tags"] = sorted({*(t for t in tags if isinstance(t, str)), "production-regression"})
    metadata.setdefault("severity", str(group.get("severity") or "high"))
    redacted = 0
    if isinstance(spec.get("input"), dict) and redact is not None:
        spec["input"], redacted = _redact_strings(spec["input"], redact)
    return doc, redacted
