# ADR-0035 — One palette in two themes, chosen by a cookie

* Status: accepted · Date: 2026-09-26

## Context
The spec asks for light and dark themes (§6, the UX items of the Definition
of Done), status indicators legible in both (§97) and WCAG-conscious
contrast (§42). The web app was light only: about 1,200 colour classes
(`bg-white`, `text-slate-900`, `border-rose-200`, …) across some 100
components, plus three hex colours on the dependency graph's edges.

Adding `dark:` variants next to every colour class would double the class
lists, be forgotten in the next component, and leave no way to check that
both themes stay readable. A second reason to avoid per-component work: the
components already say what they mean (a card is `bg-white`, secondary text
is `text-slate-600`, a failure badge is `bg-rose-50 text-rose-800`); what
changes in the dark theme is what those words look like, not the words.

Choosing the theme needs care too. The Content-Security-Policy is
nonce-based (ADR-0015), so the usual inline "read localStorage, set a class
before paint" script would need the nonce on every page; without it the page
flashes light before turning dark.

## Decision
**Every colour variable holds both themes.** Tailwind v4 writes each colour
class as `var(--color-<family>-<shade>)`. `src/app/palette.css`, generated
by `scripts/theme-palette.ts` from Tailwind's own colours, redefines every
one as `light-dark(<light>, <dark>)`:

* the dark value is the same family mirrored: 50 → 950, 100 → 800,
  200 → 700, … 800 → 100, 900 → 50, 950 → white. The mirror is shifted by
  one step on the light side so that secondary text (`slate-500`) gets
  *lighter* than its mirror would make it, which keeps it at AA on dark
  surfaces;
* `white`, the surface of cards and panels, becomes slate-900 — lighter than
  the page (`slate-50` → slate-950) and darker than a subtle panel
  (`slate-100` → slate-800), so the depth of the light theme survives;
  `black` becomes white.

The page's `color-scheme` picks the half: `light`, `dark`, or `light dark`
(follow the operating system). Components keep their classes; the graph's
edges use the palette variables instead of hex, and React Flow gets the
resolved theme. The palette is wrapped in `@supports (color: light-dark(…))`:
a browser without `light-dark()` keeps Tailwind's light colours rather than
losing every colour to an invalid value.

**The choice is a cookie the server reads.** `agenttwin_theme` is `light`,
`dark` or `system` (the default); the root layout renders
`<html data-theme=…>` from it, so the first paint is already right and no
script runs before it. The switcher in the header (three toggle buttons,
`aria-pressed`) sets the attribute and the cookie. The cookie is not
HttpOnly: it is a display preference, not a credential.

**Contrast is checked, not hoped for.** A unit test reads every class string
in the components, pairs each text colour with the background it sits on
(per state: `hover:`, `data-[state=active]:` …; a text colour with no
background of its own on a card and on the page), and requires AA (4.5:1)
in the light theme and, in the dark one, AA or at least the light theme's
ratio. The end-to-end suite runs axe (WCAG 2.1 A/AA) on every page in both
themes and on a phone.

## Consequences
* A new component is themed by writing the classes it would have written
  anyway; a colour pair that is unreadable in either theme fails `make test`
  before anyone sees it.
* The mirror is a rule, not a designed dark palette: accents become pastel
  in the dark theme (a primary button is indigo-300 with dark text). That
  reads well and keeps contrast, at the cost of the saturated look some
  dark themes have.
* Two colours that only differ by a mid shade (a `*-500` dot on a `*-400`
  bar) keep their difference but not its direction; nothing in the app
  relies on it, and status never depends on colour alone (text and icon
  carry it).
* Upgrading Tailwind means regenerating `palette.css`; the unit test fails
  until it is.
