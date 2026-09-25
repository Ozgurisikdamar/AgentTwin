# ADR-0015 — Web stack, version pins and the backend-for-frontend

* Status: accepted · Date: 2026-09-24

## Context
The specification (§40) asks for Next.js, TypeScript, Tailwind CSS, accessible
primitives, TanStack Query, and "the current stable versions at implementation
time, pinned". ADR-0009 already decided that the browser never holds a bearer
token. Phase 1 needed a concrete stack and the exact shape of that server-side
proxy.

## Decision
* **Framework:** Next.js 16 (App Router, React 19, standalone output). Next 16
  replaces `middleware.ts` with `proxy.ts`; it is used only to redirect visitors
  without a session and to set the per-request CSP nonce.
* **UI:** Tailwind CSS 4 and Radix primitives (tabs, tooltip) wrapped in small
  local components in the shadcn/ui style (`src/components/ui`), lucide icons.
  No component library dependency beyond the primitives.
* **Data:** TanStack Query for all API reads (retries only for transient
  failures, polling for traces that are still receiving spans and for the live
  explorer). URL search params hold explorer filters so views are shareable.
* **Exact pins, not ranges**, in `apps/web/package.json`, with three deliberate
  holds below the newest majors:
  * TypeScript **6.0.x** — `typescript-eslint` (required by `eslint-config-next`)
    supports `<6.1`; TypeScript 7 would leave type-aware linting unsupported.
  * ESLint **9.x** — the Next.js and React Hooks plugins do not declare
    ESLint 10 support yet.
  * Playwright **1.56.1** — matches the preinstalled Chromium build
    (`chromium-1194`), so tests never download a browser; upgrading Playwright
    means upgrading that browser build with it.
  Each hold is revisited when its blocker ships a compatible release.
* **Backend-for-frontend:** the browser calls same-origin `/api/v1/*`; a route
  handler forwards to the control plane with the session token from an
  `HttpOnly; SameSite=Lax` cookie (`Secure` behind TLS). The BFF
  * rejects unsafe cross-site requests (`Sec-Fetch-Site`, falling back to
    `Origin` vs `Host`) before contacting the control plane,
  * maps only `/api/v1/*`, rejects traversal and oversized paths, caps request
    bodies at 16 MiB and forwards an allow-list of headers (never cookies),
  * clears the cookie when the control plane answers 401, and answers 502 with
    a generic message when it is unreachable.
  Login/logout are dedicated route handlers; the token never reaches client
  JavaScript.
* **Content Security Policy:** per-request nonce, `script-src 'self' 'nonce-…'
  'strict-dynamic'` (plus `'unsafe-eval'` only in `next dev`), stylesheets
  nonce-bound, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`,
  `frame-ancestors 'none'`, `upgrade-insecure-requests` behind TLS; inline
  style *attributes* are allowed for computed geometry (waterfall bars) —
  scripts stay nonce-bound.
* **Tests:** Vitest + Testing Library for units and components (jsdom), route
  handlers tested as functions with real `NextRequest` objects, Playwright
  end-to-end against the running Compose stack — never against mocks.

## Consequences
* One origin for the browser: no CORS configuration, and a stolen page script
  cannot exfiltrate a bearer token because none is readable.
* The web server is a component of the trust boundary; its handlers carry unit
  tests for every security rule above, and each e2e test fails on console
  errors or CSP violations.
* React Flow, TanStack Table and a chart library are added in the phases that
  need them (graph, release and analytics views), pinned the same way.
