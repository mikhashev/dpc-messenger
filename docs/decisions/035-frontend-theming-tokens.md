---
adr: 035
title: "Unify the split-brain UI under a token layer before adding a dark theme"
status: accepted
date: 2026-08-03
deciders: [Mike]
consulted: [Ark, CC, Fable 5, GLM 5.2]
informed: []
depends_on: []
related: [ADR-003]
supersedes: []
session: S62
---

## Context and Problem Statement

The task as posed was "add a dark theme to the DPC Messenger frontend". Four independent
audits — Ark, Fable 5, GLM 5.2 and CC — converged on the same correction: **the app is not
light. It is a light/dark hybrid, and neither theme is complete.**

Thirteen of 47 `.svelte` files already render dark, in two unrelated palettes. Twenty-eight
CSS custom-property names are already *consumed* in four files and defined nowhere. One
component implements dark mode on its own via `prefers-color-scheme`, keyed to the OS rather
than to any app state. Six different blues compete for the role of accent colour.

So the work is not "paint 9,000 lines of CSS dark". It is: introduce a token layer, reconcile
what is already there into it, and only then does a theme switch become a small addition.
The framing matters because it changes what Phase 1 must contain — see Consequences.

Evidence for every claim above is frozen in the Appendix with its date and command.

## Decision Drivers

- **Minimal dependencies.** The project has no CSS framework, no PostCSS config, no theming
  package, and states this as a constraint. Any solution adding a permanent build dependency
  starts at a disadvantage.
- **Svelte style scoping.** 34 files carry scoped `<style>` blocks. Any mechanism based on
  *selectors* fights the scope hash in every file; custom properties inherit through it for
  free.
- **Offline-first desktop.** The client runs without network and without the core service.
  Nothing on the first-paint path may depend on a socket or a CDN.
- **Three webviews, one codebase.** WebView2, WKWebView and WebKitGTK differ in
  `prefers-color-scheme` reliability and in native-control rendering. The styling mechanism
  must not depend on those differences; only a small resolver may.
- **The light theme must be provably unchanged.** There is no visual-regression harness
  (Vitest only — see Appendix), so the guarantee has to be structural: a token whose light
  value equals the literal it replaces is a no-op by construction.

## Decision

**Adopt one semantic tier of CSS custom properties, defined in a single global
`theme.css`, overridden under `[data-theme="dark"]` on `<html>`, with a preference store
placed where this repository already keeps stores.**

Concretely, and each point is a decision, not a suggestion:

1. **Mechanism.** Tokens on `:root`; dark values under `:root[data-theme="dark"]`. Components
   reference `var(--token)` and never branch on theme. `prefers-color-scheme` is an *input to
   the resolver*, never a styling mechanism.
2. **One semantic tier**, roughly 40 tokens — not a primitive→semantic→component pyramid.
   At 47 components the indirection costs comprehension and buys nothing.
3. **Canonicalize the 28 names already in the tree** (`--text-primary`, `--border-color`,
   `--bg-tertiary`, …) rather than inventing a parallel vocabulary. This makes 158 existing
   `var()` references correct instead of orphaned.
4. **Location from the repository, primitive from the framework.** The module lives at
   `src/lib/services/theme.svelte.ts` and is re-exported from `coreService.ts` — that is where
   this project keeps state, and `src/lib/stores/` does not exist, so all three reviewer
   proposals invented a directory. But the state itself uses **runes (`$state`/`$derived`)**,
   not a `writable` store, because [the Svelte 5 documentation](https://svelte.dev/docs/svelte/stores)
   directs new code to runes and reserves stores for "complex asynchronous data streams" or
   cases needing manual control over updates and subscriptions. A theme preference is neither.
   **Known cost, accepted deliberately:** this is the first `.svelte.ts` module in the
   repository, so the codebase gains a second state idiom — the eleven existing services are
   `writable` and are consumed as `$store` in markup, whereas this one is read as
   `theme.effective`. Nothing existing is migrated; the divergence is the price of not adding
   a twelfth instance of the pattern the framework has moved away from.
5. **Anti-FOUC via a synchronous, non-module inline script** in `app.html`, plus a two-line
   background guard. The existing script there is `type="module"` and therefore deferred —
   the new one must not be merged into it. The Tauri CSP already carries `'unsafe-inline'`.
6. **`color-scheme: light|dark` per theme block**, so native selects, scrollbars, checkboxes
   and autofill follow the theme instead of rendering light inside a dark app.
7. **Font tokens are laid down in the same pass as colour tokens** (`--font-ui`,
   `--font-mono`, `--font-size-*`), even if only one value set ships initially.
8. **Audit the four `var()`-consuming files in Phase 1, before tokens are defined** — see
   Consequences/Negative, this is a live behaviour change, not a dormant one.
9. **A throwaway codemod is acceptable; a build dependency is not.** A one-off Node script
   applying a hand-authored `{hex → token}` map may perform the mechanical bulk of the ~1,200
   substitutions. It runs once, is committed or discarded, and never enters the build
   pipeline. The map — the actual difficulty — is authored by hand either way.
10. **Keep the 28 existing token names as they are**, inconsistent as they are (`--text-primary`
    unprefixed, `--yellow` colour-named, `--bg-tertiary` semantic). Renaming them would churn
    158 references for cosmetics. Colour-named legacy tokens (`--yellow`, `--green`, `--blue`,
    `--red`) are kept as **aliases pointing at the semantic tokens** during the transition and
    deleted once their references are migrated.
11. **One `localStorage` key, `dpc.theme`, and the `dpc.` prefix becomes the convention for
    new keys.** The project currently holds eight ad-hoc keys in eight styles; without an
    explicit rule the next addition is a ninth style.
12. **Colour theme and type scale are orthogonal axes and must stay so.** `[data-theme]`
    carries colour; any future large-type or accessibility mode is a *separate* attribute
    (e.g. `[data-font-size]`), never a third theme. The font-size tokens of Decision 7 make
    this possible; nothing here should be read as licensing a `[data-theme="senior"]`.

### Rationale

Custom properties are the only mechanism that crosses Svelte's scope hash without
`:global()` surgery, a build plugin, or wrapper selectors — and the codebase has already
voted for it 158 times. Everything else on the table either duplicates the style surface
(two stylesheets, per-component media queries), adds a permanent dependency for work that
must be done by hand anyway (PostCSS, theming packages), or requires a webview floor we do
not control (`light-dark()`, `oklch()`).

The one-tier choice is where this ADR departs from GLM 5.2, who proposed raw-palette →
semantic-role indirection. That is standard practice and the right call at a different scale:
two tiers pay off when a palette is re-skinned wholesale across hundreds of components, or
when several products share one palette. Here the dark palette is already fixed by precedent
in the tree and the component count is 47 — the second tier adds a level of indirection to
every debugging session without a benefit anyone can point at.

The state module is where this ADR splits the question in two, and each half is answered by a
different authority. **Location** departs from all three reviewer proposals — an effect in
`+layout.svelte`, `src/lib/stores/theme.ts`, `src/lib/styles/theme.svelte.ts` — none of which
matches how this project actually stores state; that answer came from asking the code graph
instead of reasoning from convention. **Primitive** departs from this ADR's own first draft,
which chose `writable` for consistency with eleven sibling services without checking what the
framework recommends. It recommends runes. Convention is a good tie-breaker and a poor
substitute for reading the documentation of the thing you are using.

## Considered Options

- **A. Custom properties + `data-theme` attribute** — chosen.
- **B. Two parallel style sets** (`.light .foo` / `.dark .foo`, or two stylesheets).
- **C. PostCSS plugin layer** (`postcss-dark-theme-class` or a build-time hex→var transform).
- **D. CSS `light-dark()` / `oklch()` / `color-mix()`.**
- **E. Adopt Tailwind or a component library with a dark variant.**
- **F. `filter: invert(1) hue-rotate(180deg)`.**

### Pros and Cons of the Options

#### A. Custom properties + `data-theme`
- Good: inherits through Svelte scoping with no per-file changes; zero dependencies; one
  attribute flip, one style recalc; identical behaviour on all three webviews; substitution
  is provably neutral for the light theme.
- Neutral: still requires the manual mapping of 242 distinct literals onto ~40 tokens — that
  work exists under every option.
- Bad: the mapping pass touches ~34 files and cannot be fully automated (~15% of literals
  need a human call on which semantic role they belong to).

#### B. Two parallel style sets
- Good: conceptually simple; no token vocabulary to design.
- Bad: `ThinkingBlock.svelte` is the in-repo cost demo — 47 of its 129 style lines are
  duplicated dark styling, ~36%. Extrapolated across ~8,800 style lines that is roughly
  3,000 lines of drift-prone duplication, every future visual edit performed twice, and —
  because of scoping — a `:global([data-theme="dark"])` prefix on every dark selector.

#### C. PostCSS plugin layer
- Good: automates the mechanical half.
- Bad: a permanent build dependency and a new failure mode, in exchange for the last 10% of
  the work; the colour→token map (the actual difficulty) still has to be authored by hand.
- Note: a **throwaway** codemod script that applies a hand-authored map once, outside the
  build, is not this option and is acceptable.

#### D. `light-dark()` / `oklch()` / `color-mix()`
- Good: one line per token; modern and terse.
- Bad: requires a webview floor that WebKitGTK on stable Linux and older WKWebView do not
  guarantee, for an app that ships no browser of its own. Also binds exactly two themes into
  every declaration, so it cannot express "this surface is dark in both themes" — which is
  precisely what the agent/terminal surfaces need.

#### E. Tailwind or a component library
- Bad: a 47-component rewrite to obtain one feature; contradicts the stated
  minimal-dependency constraint.

#### F. `filter: invert()`
- Bad: destroys avatars, image previews, gradients and shadows, and inverts the 13 already-dark
  files into light ones. Not shippable; listed because it is the reflexive suggestion.

## Consequences

**Positive**
- One override site for the whole application; a future additional *colour* theme (high
  contrast, for instance) is one more `[data-theme="…"]` block and nothing else. A large-type
  or accessibility mode is **not** a theme — it is a second axis, per Decision 12.
- 158 existing `var()` references become correct instead of orphaned.
- The two stray dark palettes collapse into one, ending the split-brain state whether or not
  a dark theme ever ships.
- `ThinkingBlock.svelte` loses ~47 duplicated lines.

**Negative**
- **Defining tokens changes the light theme's rendering today, in four files.** Of the 158
  `var()` references, **47 carry no fallback**. A declaration such as `color: var(--text-primary)`
  with no definition is invalid and is discarded by the browser — those elements currently
  inherit instead. The moment `--text-primary` exists, 47 declarations start applying for the
  first time. This is the single most likely source of "we only added dark and light broke".
  It is why the audit of those four files is Phase 1 work and not a Phase 3 leftover.
- Tinted surfaces (the amber context panel, the violet peer-context panel) do not invert
  mechanically and need designed dark counterparts. The green own-message bubble leaves this
  list — per Q7 it moves to the accent.
- 72 `box-shadow` declarations are near-invisible on dark; elevation has to be re-expressed as
  surface lightness plus a 1px border.
- **Thirteen files need light variants invented, not derived** (Q2). They have never had one.
  This is design work, not substitution, and it is the largest single cost in the plan — it is
  what makes Phase 3 the heaviest phase rather than a tail.
- **The light-theme guarantee has no automated backstop** (Q8). No harness is built; Mike
  reviews screenshots as work lands. The structural claim — "a token whose light value equals
  the literal it replaced is a no-op" — therefore holds only as far as the per-file discipline
  holds. That discipline is now load-bearing rather than tidy.

**Neutral**
- The migration is mechanical per file and reviewable per file, but it is ~1,200 substitutions;
  it will span multiple sessions.

## Confirmation

- [ ] `theme.css` is the only file in `dpc-client/ui/src` containing colour literals; a CI grep
      fails the build on any `#hex` or `rgb()/rgba()` outside it (allowlisting HTML entities).
- [ ] For every migrated file, the token's light value equals the literal it replaced. The two
      permitted departures — merging near-duplicate values, and adjusting a pair that was hard to
      read (Q5) — are each enumerated in the commit message. Never silent: with no harness, the
      commit log is the only place a deliberate change is distinguishable from a mistake.
- [ ] The four files that already consume `var()` are re-verified visually in the **light**
      theme after tokens are defined, and the 47 no-fallback references are each resolved to an
      intended colour rather than left to whatever the token happens to be.
- [ ] Cold start with the dark preference shows no white flash on Windows, Linux and macOS.
- [ ] Native `<select>` popups, scrollbars and autofill render dark under the dark theme
      (i.e. `color-scheme` is in effect), verified on all three OSes.
- [ ] Manual theme choice overrides the OS on all three OSes; `system` mode follows a live OS
      change on at least Windows and macOS. Linux may degrade to "wrong default", never to
      "broken UI".
- [ ] `ThinkingBlock.svelte` contains no `@media (prefers-color-scheme: …)` block.
- [ ] No component contains a `[data-theme="dark"]` or `:global(.dark …)` override; all theme
      branching lives in `theme.css`.
- [ ] The colour-named legacy aliases (`--yellow`, `--green`, `--blue`, `--red`) are gone from
      `theme.css` once their references are migrated — an alias that outlives the transition is
      a second vocabulary.
- [ ] Exactly one `localStorage` key is added, and it is `dpc.theme`.
- [ ] No `[data-theme]` value encodes anything but colour; type scale is a separate attribute
      if and when it exists.
- [ ] If a codemod was used, it is not referenced from `package.json` scripts, `vite.config`,
      or any CI step.
- [ ] Every one of the thirteen formerly-dark files renders in **both** themes; none of them
      keeps a hardcoded dark surface (Q2). No `--term-*` family exists.
- [ ] No occurrence of `#dcf8c6` remains; own-message surfaces derive from the accent (Q7).
- [ ] One file per commit through the migration, each commit naming any deliberate colour
      merges it performs. With no automated harness (Q8) this is the only structural guard the
      light theme has, so a commit spanning several files is itself a defect.
- [ ] The CSP in `tauri.conf.json` no longer allowlists `fonts.googleapis.com` or
      `fonts.gstatic.com`, and the font loads from `static/` with the network unreachable.
- [ ] `theme.svelte.ts` is the only state module using runes; no existing `writable` service was
      converted as a side effect of this work.

## Scope

- `dpc-client/ui/src/lib/styles/theme.css` — new; all colour, shadow and font tokens.
  **`src/lib/styles/` is a new subdirectory** — `src/lib/` currently holds `components/`,
  `panels/`, `services/`, `utils/` and nothing style-related. Noted so the next reader does not
  hunt for an existing styles location.
- `dpc-client/ui/src/lib/services/theme.svelte.ts` — new; runes (`$state` preference,
  `$derived` effective theme), `localStorage` key `dpc.theme`, `matchMedia` change listener,
  `getCurrentWindow().setTheme()` guarded so a per-OS no-op cannot break the switch.
  `onThemeChanged` is available as a secondary signal if Linux `matchMedia` proves unreliable.
  The `.svelte.ts` extension is required for runes outside a component and is the first such
  file in the repository.
- `dpc-client/ui/src/lib/coreService.ts` — one re-export line, matching the existing pattern.
- `dpc-client/ui/src/app.html` — synchronous non-module bootstrap script + 2-line background
  guard. Must not be merged into the existing `type="module"` link-interceptor script.
- `dpc-client/ui/src/routes/+layout.svelte` — one `import` of `theme.css`.
- `dpc-client/ui/src/lib/components/ThemeToggle.svelte` — new; light/system/dark control.
- `dpc-client/ui/src/lib/panels/panels.css` — 261 lines; global by accident (see Appendix),
  therefore high leverage. In Phase 3 the `@import` inside `ChatPanel.svelte`'s `<style>` is
  replaced by an explicit global import from the layout, so the file's scope stops being a
  side effect of Vite's resolution order.
- 34 `.svelte` files with `<style>` blocks — literal → token substitution.
- `dpc-client/ui/src-tauri/tauri.conf.json` — three changes, all in Phase 1:
  - **`backgroundColor` on the main window.** Verified present in our version (see Appendix), so
    no hedge: the webview frame is painted before the bootstrap script runs, and on Linux this is
    the *only* defence against the flash, because `theme` is not implemented there.
  - **`visible: false` plus show-on-ready** as the fallback if a flash survives on Windows 11 —
    the documented community workaround. `visible` defaults to `true` today.
  - **Remove `https://fonts.googleapis.com` / `https://fonts.gstatic.com` from the CSP**, in the
    same commit that adds the local font. The allowance is used by nothing and is unused attack
    surface once the font ships locally.
- `dpc-client/ui/static/fonts/` — new; self-hosted `woff2`, loaded via `@font-face` in
  `theme.css`. `static/` rather than a Vite import because a hashed filename buys nothing for a
  font, and a network stylesheet is impossible for an offline-first app.

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| Phase 0 — all eight questions answered by Mike 2026-08-03; ADR accepted | Done | — |
| Font pass — PT Sans, self-hosted, **separate commit before the colour work** | Pending | — |
| Phase 1 — `theme.css`, store, toggle, bootstrap, `color-scheme` | Pending | — |
| Phase 1 — audit of the four `var()` files (47 no-fallback references) | Pending | — |
| Phase 1 proof slice — `panels.css` + `+page.svelte` | Pending | — |
| Phase 2 — core surfaces (Sidebar, ChatMessageList, FirewallEditor, …) | Pending | — |
| Phase 3 — light variants designed for the 13 formerly-dark files (largest item, per Q2) | Pending | — |
| Phase 3 — long tail, shadows, gradients, `ThinkingBlock` | Pending | — |
| Phase 3 — `panels.css` import moved from `ChatPanel` `<style>` to the layout | Pending | — |
| Phase 4 — CI colour guard | Pending | — |

## Phase 0 — questions and their answers

Answered by Mike, 2026-08-03. Recorded verbatim in effect, with the consequence of each spelled
out, because two of them changed the plan.

- **Q1 — Accent. Answered: a pair, not one colour.** Dark `#89b4fa`, light `#1976d2`. Together
  39 of the existing occurrences already sit on the chosen values; the remaining five blues
  (`#007acc`, `#2196f3`, `#007bff`, `#5a67d8`, `#0366d6` — 55 occurrences) fold into `--accent`,
  `--link` or a status token. A pair rather than a single value because a blue saturated enough
  to carry contrast on white vibrates on a dark ground, and the reverse.
- **Q2 — Dark islands. Answered: theme-aware. This is the opposite of what the draft
  recommended, and it grows the work.** The whole application is to look like one application;
  the thirteen currently-dark files gain real light variants. Consequences: the always-dark
  `--term-*` token family is **not** created; roughly 2,500 style lines that have never had a
  light variant now need one **designed**, not derived; Phase 3 becomes the largest phase rather
  than a tail. The draft's recommendation (keep them dark, IDE-terminal aesthetic) was an
  argument from cost, and cost lost to coherence — correctly, since a product that looks like
  two products is a defect the migration exists to remove.
- **Q3 — Dark palette. Answered: Catppuccin Mocha** as the official dark base. The nine files
  already using it stay; the five VS Code-dark files fold into it.
- **Q4 — Interface font. Answered: PT Sans, separate commit, before the colour work.** Self-hosted
  `woff2` under `static/`; no network stylesheet, since the app must render offline. Once the
  font is local, the Google Fonts entry in the Tauri CSP — allowlisted today and used by
  nothing — is removed rather than left as unused surface.
- **Q7 — Own-message bubble. Answered: move to the accent.** The WhatsApp-derived green
  (`#dcf8c6`) is dropped; own messages take an accent-derived surface in both themes. Removes
  the hardest tinted surface from the Phase 2 design load.
- **Q8 — Visual regression. Answered: neither option offered — Mike supplies the screenshots.**
  No snapshot harness is built and no per-phase manual matrix is imposed on the migrator;
  verification is Mike reviewing screens as work lands. **This makes the per-file substitution
  discipline the only structural guard that remains**, so it stops being a nicety: one file per
  commit, every token's light value equal to the literal it replaced, deliberate merges named in
  the commit message. Recorded plainly because the ADR's central guarantee now rests on that
  discipline alone.

- **Q5 — Contrast floor. Answered: the migration may adjust.** Where a colour pair is hard to
  read today — muted grey on white, and the amber and violet panels with coloured text on
  coloured ground — the migration is allowed to darken or lighten it enough to be legible rather
  than reproducing an unreadable original. Consequence: the light theme is **no longer
  pixel-identical everywhere**, so "we changed nothing" stops being the blanket claim. Each such
  adjustment is named in its commit, exactly like a token merge; the difference between a
  deliberate fix and an accidental regression has to be visible in the history, because no
  harness will draw it (Q8).
- **Q6 — Persistence and placement. Answered.** The choice is remembered — `localStorage` under
  `dpc.theme` is sufficient, and **no backend mirror will be built**. The control goes in the
  left sidebar alongside the existing status controls, i.e. `Sidebar.svelte`.

## Open Questions

None blocking. Phase 0 is closed; the remaining unknowns are per-OS runtime behaviours that only
running the thing will settle (native title bar on Linux, cold-start flash on each platform),
and they are covered by the Confirmation checklist rather than by a decision.

## Authors

- **Mike** — Decision, task framing
- **Ark** — First analysis, reviewer prompt, synthesis of the external reviews
- **Fable 5**, **GLM 5.2** — Independent external audits (`ideas/dark-theme-*.md`)
- **CC** — Independent census, verification of the reviewers' figures, this ADR

## References

- `ideas/dark-theme-fable5.md` — external audit, 2026-08-03
- `ideas/dark-theme-glm52.md` — external audit, 2026-08-03
- `docs/decisions/003-frontend-stores.md` — existing frontend store strategy
- `docs/decisions/TEMPLATE.md` — ADR structure
- `D:\GameDev\spider-solitaire-yandex` — a shipped project using this exact pattern
  (theme classes overriding custom properties), read at source 2026-08-03. Useful as evidence,
  **not as a model to copy wholesale** — see the caveats below.

### On the spider-solitaire precedent

Read directly: `docs/Designing Solitaire for Seniors (55+)_ Color Palettes and UI_UX Best
Practices.md`, `css/variables.css`, `css/themes.css`.

**What it validates.** The palette there is *derived from a cited research document*, not
chosen by taste — high contrast for reduced contrast sensitivity, saturated colour reserved for
accents, a 16px floor, sans-serif, and PT Sans chosen explicitly for Cyrillic coverage. Five
theme classes override custom properties on `body`; the mechanism works in production.

**What does not transfer.** The premise is an audience aged 55+. Its central colour finding —
blue perception declines with age, so blue is a poor choice for contrast-critical roles — is
age-specific and must not be used to settle Q1 for DPC. Two things do transfer regardless of
audience: an accent must be **desaturated and lightened for a dark ground** (the reason a
Catppuccin-family blue reads on dark where a raw Bootstrap blue would vibrate), and the 16px /
sans-serif floor is general readability. PT Sans's *Cyrillic* justification transfers exactly,
since this UI is Russian-language.

**Three corrections to how this precedent was described in review.** Recorded because the
project's own reviews repeated them and a future reader would otherwise inherit them:
1. There is no `.theme-green` class — green is the `:root` default. Five classes exist, applied
   to `body`, not `html`.
2. Colour and size are **not** fully orthogonal there. A separate axis exists only for card
   geometry (`.card-size-large`); all three senior themes redefine `--font-size-*` and
   `--line-height-*` **inside the theme class**. The scale is duplicated three times and there
   is no way to get large type on the default theme. This makes spider-solitaire the
   *counter-example* that motivates Decision 12, not a precedent for it.
3. `themes.css` applies a global `body { transition: background-color … }` — precisely the
   theme-switch transition this ADR's sources advise against.

---

## Appendix — frozen census

Per the discipline adopted for ADR-034: the numbers below are **decision evidence**. They are
dated, carry the instrument that produced them, and are never edited in place — corrections go
beside them. Live metrics belong in `backlog.md`, not here.

**Measured 2026-08-03, branch `dev` @ `940ea6b9`, working tree clean under `dpc-client/ui`.**
Instrument: `grep -rE` over `dpc-client/ui/src` restricted to `*.svelte`, `*.css`, `*.html`,
excluding `node_modules`, `build`, `.svelte-kit`, `target`.

| Measure | Value |
|---|---|
| `.svelte` files / with a `<style>` block | 47 / 34 |
| Style-block lines / `panels.css` lines | 8,767 / 261 |
| Hex literal occurrences / distinct values | 1,207 / 242 |
| `rgb()`/`rgba()` occurrences | 133 — of which 19 are `rgba(255,255,255,…)`, 52 `rgba(0,0,0,…)` |
| `box-shadow` / gradients | 72 / 18 |
| `var(--…)` references / distinct names / files | **158 / 28 / 4** |
| — of those, without a fallback | **47** (currently invalid declarations) |
| Custom-property **definitions** anywhere in `ui/` | **0** |
| `prefers-color-scheme` | 1 file (`ThinkingBlock.svelte`) |
| Files carrying dark palettes | 13 (9 + 5, one file carrying both) |
| Candidate accents | 25 / 24 / 14 / 13 / 11 / 3 occurrences across six blues; the `panels.css` accent is the rarest at 3 |
| `font-family` declarations | 18 monospace, 16 `inherit`, 9 other monospace stacks, **1 sans-serif** |
| Hardcoded `font-size` | 422 |
| `localStorage` usage | 8 files, 8 ad-hoc keys, no central settings store |
| Test tooling | Vitest only; no Playwright in `package.json` |
| Tauri CSP | contains `'unsafe-inline'` — an inline bootstrap script is permitted |
| Tauri window config, as written today | 9 keys; **neither `theme` nor `backgroundColor` is set** → a dark cold start flashes white |
| Tauri `WindowConfig`, as offered by our version | 57 fields. `backgroundColor` exists (default `None`; on Windows the alpha channel is ignored for the window layer, and for the webview layer on Win7, and on Win8+ when alpha ≠ 0). `theme` exists (default `None` = system) and is documented **"Only implemented on Windows and macOS 10.14+"** — i.e. **not on Linux**, where `backgroundColor` is therefore the only defence against the flash. `visible` defaults to `true`. Instrument: `node_modules/@tauri-apps/cli/config.schema.json` (CLI 2.9.2) cross-checked against `tauri-utils 2.8.1` in the cargo registry — the prose reference truncates before `WindowConfig`, the schema does not |
| Svelte state primitive | docs direct new code to runes; stores are reserved for complex async streams or manual subscription control. Repository holds 11 `writable` services consumed as `$store` and **0** `.svelte.ts` modules — see Decision 4 for the split verdict |
| Svelte scoped styles | scoping is a class hash (`svelte-xyz`); scoped selectors gain a specificity increase of **0-1-0**. The docs page says nothing about custom properties or about `@import` inside `<style>` — see the provenance note below |
| `@tauri-apps/api` | declared `^2.9.1`, **installed 2.9.1**; `setTheme(theme?: Theme \| null)` present at `node_modules/@tauri-apps/api/window.d.ts:1164`, `onThemeChanged` at `:1307`. Presence measured; **per-OS runtime behaviour not tested** — a no-op on Linux would leave the native title bar light and must not break the switch |
| SvelteKit mode | `ssr = false`; first paint is `app.html` alone |
| Google Fonts in CSP | allowlisted, but **no** `@font-face`, `.woff` or `fonts.googleapis` reference exists — the permission is vestigial |

**Correction to a figure in circulation.** Two of the analyses in `ideas/` and the synthesis
built on them state **202** `var()` usages. The measured value is **158**. Five independent
slices agree (`*.svelte` alone; `*.svelte`+`*.css`+`*.html`; the whole `ui/` tree minus build
artefacts; per-file counts summed; the union of distinct names). The per-name breakdown printed
inside `dark-theme-fable5.md` §0.2 itself sums to 158 — 22+22+14+14+13+10+10+6+5+5+5+4+4+3+3,
plus five names at 2 and eight at 1. **The reviewer's data was right; the headline number was
not.** The distinction matters because 158, not 202, is the size of the Phase 1 audit.

**Provenance note — two claims in this ADR do not rest on Svelte's documentation, and should
not be cited as if they did.**

1. *"Custom properties defined on `:root` reach scoped component styles."* True, and load-bearing
   for the whole approach — but it follows from CSS inheritance, not from anything Svelte
   states. The scoped-styles page does not mention custom properties at all. Svelte's scoping is
   a class added to selectors, and a class cannot interrupt inheritance; that is the reason, and
   it is a CSS reason.
2. *"`@import` inside a component `<style>` lands global."* The docs are silent here too. The
   evidence is `panels.css`'s own header comment plus the observed behaviour of the built
   bundle. Recorded as observation, not as specification — a future Vite or Svelte release could
   change it without breaking any documented promise.

Both were checked because a reviewer described the first as documented. It is not; the
behaviour is right and the citation would have been wrong.

**Tooling note.** The local code graph (Orbit) indexes `.svelte` as `language: unknown` and
produces **0** definitions for it, as it does for `.css` and `.html`; only `.ts` is parsed
(959 definitions under `ui/src`). It cannot answer questions about styling or the component
graph, and re-indexing does not help — the limitation is the parser, not the snapshot. It *was*
decisive for one question: where this repository keeps its stores. Recorded so the next person
does not re-derive it.

---

## Revision history

**Round 1 — internal review, 2026-08-03.** Reviewed by Ark and Warren against the first draft.
Ten changes applied; recorded here because an undocumented reversal comes back.

| # | Change | Found by |
|---|---|---|
| 1 | `setTheme()` availability was asserted from the declared version. Now measured in the installed package with file and line, and narrowed: presence is verified, per-OS runtime behaviour is not. `onThemeChanged` found in the same file and added as the Linux fallback signal. | Ark, confirmed by Warren |
| 2 | `src/lib/styles/` marked explicitly as a new subdirectory. The draft criticised three reviewers for inventing directories for the store, then placed `theme.css` in an invented one without saying so. | Ark |
| 3 | "the second tier would be design-system cosplay" replaced with a cost/benefit statement. The dismissed argument is standard practice at a larger scale; the draft's tone misrepresented it. | Ark, confirmed by Warren |
| 4 | Throwaway codemod promoted from an aside in Option C to Decision 9, with the build-dependency prohibition attached. | Ark, echoing Fable 5 |
| 5 | Spider-solitaire added to References — as evidence with three recorded caveats, not as endorsement. | Warren |
| 6 | Colour theme and type scale made an explicit orthogonality rule (Decision 12); the ambiguous "a future third theme (… senior large-type)" in Consequences corrected. | Warren |
| 7 | Q8 raised from open question to a decision required before Phase 2. Warren's supporting arithmetic (an assumed 2% substitution error rate) was **not** adopted — the rate has never been measured, and inventing one is the habit ADR-034 exists to prevent. | Warren, partially |
| 8 | Token naming settled as Decision 10: keep the 28 existing names, alias the colour-named legacy ones during transition. | Ark and Warren |
| 9 | `panels.css` import fate settled: moved to an explicit global import in Phase 3. | Ark and Warren |
| 10 | `localStorage` key changed to `dpc.theme` and the `dpc.` prefix adopted as the convention for new keys. | Ark |

**Round 2 — Phase 0 answered, 2026-08-03.** Mike answered Q1–Q4, Q7 and Q8. Two answers changed
the plan rather than confirming it, and are recorded as such:

- **Q2 went against the draft's recommendation.** The draft argued for keeping the thirteen dark
  files dark, on cost. Mike chose theme-aware coherence. The `--term-*` token family is dropped,
  ~2,500 style lines gain designed light variants, and Phase 3 becomes the heaviest phase. The
  ADR now says so in Consequences and Implementation Status rather than leaving the old estimate
  standing.
- **Q8 was answered outside the offered options.** Neither a snapshot harness nor a per-phase
  manual matrix: Mike reviews screenshots as work lands. The consequence is stated plainly —
  the per-file substitution discipline is now the only structural guard, and a multi-file commit
  is therefore itself a defect. Promoted from prose into the Confirmation checklist.

Q5 was returned unanswered because it was asked in jargon; it has been restated in plain terms.
Q6's answer settled persistence and left toggle placement open.

**Round 3 — checked against the upstream documentation, 2026-08-03.** Prompted by Mike asking
whether the plan actually follows the Svelte and Tauri recommendations. Until this round it did
not, in one place, and nobody had looked.

| # | Finding | Source |
|---|---|---|
| 1 | **Decision 4 violated Svelte's own guidance.** The draft chose `writable` on the strength of repository convention, without checking that Svelte 5 directs new code to runes and reserves stores for complex async streams. Fable's runes proposal was right on this axis. Corrected: location stays (`services/`, re-exported), primitive becomes runes, and the resulting second state idiom is recorded as an accepted cost rather than glossed over. | `svelte.dev/docs/svelte/stores` |
| 2 | **`backgroundColor` and `theme` verified against our installed version**, not the website. The hedge "if the installed Tauri minor supports it" is removed, and the Linux caveat stops being an inference: `theme` is documented as Windows/macOS-only, which is why `backgroundColor` is mandatory rather than nice-to-have. | CLI 2.9.2 config schema, `tauri-utils 2.8.1` |
| 3 | **A review claim that scoped-style inheritance of custom properties is "documented" was corrected** before it could enter the ADR. The behaviour holds; the citation does not exist. Provenance note added covering this and the `@import` claim. | `svelte.dev/docs/svelte/scoped-styles` |
| 4 | Three structural requirements (`adapter-static` + SPA fallback, `ssr = false`, `frontendDist`) were verified independently rather than accepted from the review that raised them. All three already held. | repository |

The shape of finding 1 is worth keeping: a convention inside the repository was allowed to
answer a question that belonged to the framework's documentation, and the mistake survived two
review rounds because both reviewers were reasoning from the same repository.

**Correction carried forward from the draft's own research.** The draft cited
spider-solitaire second-hand, through a summary, rather than reading it. Reading it at source
produced the three corrections now in References — including one that inverts the argument it
was cited for. The lesson is the one this project keeps relearning: a summary of a source is
not the source, and a durable document should not contain claims that were never checked
against the artefact they describe.
