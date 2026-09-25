# ADR-0030 — The web draws a bounded neighbourhood with React Flow, in columns by distance

* Status: accepted · Date: 2026-09-25

## Context
Golden path step 5 (spec §137) reads: "Open Change Impact". The reader should
then see prompt → refund-agent → refund_payment → payments service, and the
scenarios the change affects.

People open the graph with one question: what does this touch, and how do we
know? A force-directed picture of a whole project answers neither. It moves on
every load, hides direction, and grows without bound. ADR-0015 left the
choice of a graph library to the phase that needs one.

## Decision
* **React Flow (`@xyflow/react` 12.12.0), pinned**, like the rest of the web
  stack. It renders and handles interaction: pan, zoom, selection and
  keyboard. We compute positions ourselves, and the library adds no layout
  dependency. The canvas is loaded on the client only (`next/dynamic`, no
  SSR). Its inline styles are allowed by the page CSP (`style-src-attr
  'unsafe-inline'`); scripts stay nonce-only.
* **Only a bounded neighbourhood is drawn.** The page asks the graph service
  for a view around a focus component (ADR-0026). The query takes a depth
  (default 2, or 4 when opened from a change set) and component kinds, and is
  limited to 150 components. The summary tells the reader how much of the
  project is shown: "Showing X of the project's Y components and E of T
  relationships, D steps from …". Filters on how a relationship is known
  (observed, declared) and on tool risk tier are applied in the browser, to
  the view already loaded. A filter then removes anything no longer connected
  to the focus, so the page never shows islands it cannot explain.
* **A deterministic layered layout.** Columns are set by distance from the
  focus, in either direction; unreachable components go after the last
  column. Inside a column, components are sorted by:
  1. kind, agent side first;
  2. where their neighbours in the previous column sit (fewer crossings);
  3. label;
  4. id.

  The same view always gives the same picture. The layout is a pure function
  with unit tests.
* **How a relationship is known is visible, and never shown as certain when
  it is not.**
  - An edge seen in traffic is drawn green.
  - A declared edge is drawn grey.
  - An inferred edge is drawn dashed amber.

  Every edge carries an accessible name that reads as a sentence, such as
  "refund_payment writes payments-db (declared)". Components are buttons
  named by kind and label. The panel lists "Acts on" and "Used by" as
  sentences, each with its confidence and evidence.
* **The blast radius is the same view, marked.** Opened from a change set,
  the page enters from the change's first seed. It outlines affected
  components by severity. By default it shows only the blast radius; a
  checkbox shows everything around it.
* **Manual mapping is edited where it is read.** A person with `graph.write`
  can edit a tool's manual dependencies in the component panel. The rows
  prefilled are the tool's outgoing relationships with `MANUAL` evidence.
  Saving replaces them with `PUT /graph/mappings/{tool}`, using an
  idempotency key per action. Other evidence is never edited from the UI.

## Consequences
* On the demo, the change set's "Show on the graph" link opens the blast
  radius four steps from the prompt, with `refund_payment` marked critical.
  A viewer sees the same picture and no mapping form. A Playwright test
  covers the mapping round trip for an engineer.
* jsdom cannot measure React Flow's handles, so edges are not rendered in
  unit tests. Unit tests check the elements handed to React Flow (`toFlow`:
  names, handles, styles). Playwright checks the rendered picture on the
  running stack.
* 150 components is a readability limit, not a hard one. A larger project
  is explored by changing the focus and the depth, never by loading the
  whole graph. The service caps a view at 500 components.
* A different layout (orthogonal routing, grouping by service) can replace
  `layoutGraph` without changing the data contract.
