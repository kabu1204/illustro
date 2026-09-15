# Plan: illustro UI/UX Redesign

Status: approved, in build. Scope: holistic redesign of `server/static` (zero build, no framework).

## 1. Architecture: split the 934-line monolith (zero build, no framework)

```
server/static/
├─ index.html   (semantic skeleton only, ~200 lines)
├─ style.css    (design tokens + all styles, new)
├─ app.js       (all logic, new — organized in commented sections)
├─ sha256.js    (untouched)
├─ sw.js        (untouched)
└─ manifest.webmanifest + icons (theme colors updated to match new bg)
```

`app.js` sections: utils/esc/debounce/toast → hash router → app state → search + autocomplete → gallery (masonry + infinite scroll) → image viewer → unified analytics (3 tabs + Chart.js) → upload (ported verbatim) → worker pill. Update the README directory-structure listing accordingly.

## 2. Design system — "fresh redesign", dark, editorial

- **Palette**: deeper ink background (`#0b0c10`-ish), elevated panels, hairline `rgba(255,255,255,.07)` borders; accent stays violet→pink gradient family (matches existing PWA icons) but used with more restraint; semantic colors: success/warn/error + rating colors (general=green, sensitive=yellow, questionable=orange, explicit=red pills).
- **Typography**: larger scale (15px UI base, 20–24px section titles, 30px+ KPI numerals with `tabular-nums`), stronger weight hierarchy, tighter letter-spacing on headings.
- **Depth & motion**: subtle shadows, 150–220ms ease transitions, card hover lift + image scale, viewer crossfade; full `prefers-reduced-motion` disable.
- Meta `theme-color` + manifest colors synced to new background.

## 3. Navigation & state — hash router (no server routing changes needed)

- `#/` gallery · `#/similar/<id>` · `#/i/<id>` viewer overlay · `#/stats` analytics · `#/upload`
- Search state lives in URL params (`?q=&rating=&tags=&excl=&sort=`) → shareable, survives refresh, Back/Forward works (including closing the viewer and undoing "Find similar"). Previous gallery scroll restored on back.

## 4. Feature work

| Area | Change |
|---|---|
| **Search autocomplete** | Debounced dropdown on the token under caret, EN+ZH, usage counts, ↑↓/Enter/Esc/click (uses existing `/api/autocomplete`) |
| **Gallery** | JS masonry: N columns by width, images distributed to shortest column using known aspect ratios (row-major append order, balanced heights, no reshuffle on load-more); `avg_color` placeholder boxes; rating pill + hover overlay (dims, top tags, quick actions: view / similar / original); similarity score badge in similar mode; skeleton shimmer during first load; empty state |
| **Infinite scroll** | IntersectionObserver sentinel + in-flight guard + end-of-results marker (replaces Load-more button) |
| **Image viewer** | Esc/backdrop close, ←→ prev/next within current result list (auto-fetches next page at end), blurred-thumb instant preview → full image crossfade, click toggles fit↔100% zoom; sidebar: filename, dims/bytes/added date, rating, color swatch, tags grouped character/general (click=filter, Shift=exclude), Find-similar + Open-original; bottom-sheet layout on mobile |
| **Sort options** | Dropdown in browse mode: Newest / Oldest / Random → small backend pass-through (`sort` param; `ORDER BY added_at DESC/ASC` / `ORDER BY RANDOM()`); tag search keeps relevance ordering |
| **Unified Analytics `#/stats`** | One overlay, 3 tabs: **Collection** (KPIs, top tags/characters, palette, orientation) · **Activity** (worker KPIs + pause/resume/process-now controls + rounds chart + benchmark + latency chart/stats — replaces the inline Monitor view) · **Duplicates** (read-only cluster browser via `/api/duplicates`, lazy thumbs, click → viewer; no keep/delete actions) |
| **Header** | Slim bar: logo, dominant search, Analytics + Upload buttons, compact **worker status pill** (dot + state + `processed/total`, click → Activity tab where the controls now live). No more separate Stats/Monitor buttons. |
| **Keyboard** | `/` focus search, Esc close any overlay, ←→ viewer nav, Enter/Tab accept autocomplete |
| **Mobile-first** | First-ever media queries: 2-col grid × ≤640px, scrollable filter bar, bottom-sheet viewer, ≥40px hit targets, safe-area-inset padding — PWA feels native |
| **Errors & feedback** | Toast system (top-right) for fetch/action failures; loading spinners in analytics/upload; empty-result messaging |
| **Upload** | All dedup/ledger/resume/wake-lock logic ported **verbatim** (longest-tested code in the file); visual restyle + copy polish only |

## 5. Backend changes (minimal, additive)

1. `illustro/search.py`: `search(..., sort="new")` — affects browse-mode ORDER BY only.
2. `server/app.py`: accept `sort` query param; add `bytes` and `added_at` to the `_img()` payload (viewer info line).
3. Nothing else: `/api/autocomplete`, `/api/duplicates`, worker/sync endpoints unchanged.

## 6. Verification

1. `node --check app.js` (syntax) if node available, else browser-less JSON/HTTP smoke.
2. Start `python -m illustro.cli serve`, `curl` all endpoints incl. `sort=random/oldest`, confirm `/` serves new HTML.
3. Manual checklist: search+autocomplete keyboard flow, infinite scroll, viewer nav/zoom, Back-button behavior, sort dropdown, analytics 3 tabs, duplicates browse, upload dry-run, mobile width (390px), PWA install unaffected.

## Explicitly NOT changing
Upload internals, service-worker share-target flow, worker/tagger/DB logic, the zero-CDN-except-Chart.js policy, feature scope (no deletion UI for duplicates — read-only).
