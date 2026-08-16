# Open Code Codex UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the AutoSWE Control Plane web UI (`apps/web`) into a high-performance, dark obsidian, glassmorphic developer workspace inspired by Open Code and OpenAI Codex.

**Architecture:** Modern semantic HTML5 markup with native `<dialog>` and drawer overlays; pure CSS design system using CSS variables, glassmorphism, responsive grid/flex layouts, and SVG cubic Bezier connection lines; vanilla JS client with topological DAG rendering, live WebSocket streaming, preset goal prompt chips, and instant audit log searching.

**Tech Stack:** HTML5, Modern Vanilla CSS3, Vanilla ES6+ JavaScript, SVG Canvas Paths, WebSockets / REST API.

---

### Task 1: Re-architect HTML Markup (`apps/web/index.html`)

**Files:**
- Modify: `apps/web/index.html`

- [ ] **Step 1: Write the updated semantic HTML structure**

Replace `apps/web/index.html` with the modern Open Code Codex workspace layout:
- Topbar with brand logo, glowing badge, platform health tooltip, search bar with `⌘K` shortcut badge, and admin access button.
- Mission Control Launchpad with repository intelligence card (folder picker with git branch and commit SHA detection), workflow launch card with prompt preset quick-chips, and recent runs history bar.
- Active Mission Control Dashboard with run status banner, 4-grid telemetry HUD, multi-stage topological DAG viewport with SVG connector overlay, split-pane governed approvals & verified artifacts deck, and full-width live terminal audit stream with search & filter pills.
- Native HTML5 slide-over task inspector drawer, artifact preview modal, and operator security modal.

- [ ] **Step 2: Verify HTML syntax and element IDs**

Run: `node -e "const fs = require('fs'); const content = fs.readFileSync('apps/web/index.html', 'utf8'); console.log('Length:', content.length);"`
Expected: Valid HTML content loaded without syntax issues.

- [ ] **Step 3: Commit HTML markup changes**

```bash
git add apps/web/index.html
git commit -m "feat(web): update HTML structure for Open Code Codex UI"
```

---

### Task 2: Build Obsidian & Glassmorphism Design System (`apps/web/styles.css`)

**Files:**
- Modify: `apps/web/styles.css`

- [ ] **Step 1: Implement complete CSS design tokens and styles**

Write comprehensive vanilla CSS in `apps/web/styles.css`:
- Design tokens: Obsidian canvas (`#07090e`), slate surfaces (`#0d1117`, `#161b22`), border highlights (`rgba(255,255,255,0.08)` to `rgba(56,189,248,0.4)`), status tokens (Emerald, Cyan, Indigo, Amber, Rose).
- Typography & code styling: Inter/system-sans for UI, JetBrains Mono for telemetry, commit hashes, and logs.
- Launchpad styles: Hero cards, prompt preset chips, folder picker buttons, identity status banners.
- Dashboard telemetry HUD: 4 glassmorphic metric cards with progress meters and token split indicators.
- DAG Visualizer: Multi-stage columns, stage progress headers, task node cards with active gradient glows and status borders, SVG connection lines with flowing animated dashes.
- Side Deck: High-risk approval alert cards, SHA-256 verified artifact cards.
- Live Terminal: Dark console styling, category filter pills, live search bar, color-coded event badges, collapsible data view.
- Slide-over drawer and dialogs: Smooth backdrop blur, tabbed detail views, syntax diff highlights (+ green / - red).
- Responsive media queries for mobile, tablet, and desktop.

- [ ] **Step 2: Validate CSS syntax**

Run: `node -e "const fs = require('fs'); const content = fs.readFileSync('apps/web/styles.css', 'utf8'); console.log('CSS lines:', content.split('\n').length);"`
Expected: Clean CSS structure with > 800 lines.

- [ ] **Step 3: Commit CSS stylesheet changes**

```bash
git add apps/web/styles.css
git commit -m "feat(web): implement obsidian glassmorphic design system in styles.css"
```

---

### Task 3: Enhance Client-Side Controller, DAG Renderer & Live Stream (`apps/web/app.js`)

**Files:**
- Modify: `apps/web/app.js`

- [ ] **Step 1: Implement enhanced UI interactions and state management**

Update `apps/web/app.js`:
- Prompt presets: Clicking chips auto-fills engineering goal and baseline commit.
- Recent runs history: Caching recently viewed and launched run IDs in `localStorage` and rendering clickable pills.
- Topological DAG Layout Engine: Compute stage ranks, column placement, render task nodes with capability icons and status pills, and calculate responsive SVG cubic Bezier paths.
- Live event search & filtering: Real-time text search filter across event types and JSON payload contents.
- Diff formatting in artifact preview: Automatically format git diff additions (green `+`) and deletions (red `-`).
- Global keyboard shortcuts: `⌘K` or `/` for search, `Esc` for modals, `1-4` for timeline filter switches.
- Robust WebSocket streaming with polling fallback and auto-reconnect.

- [ ] **Step 2: Verify JavaScript syntax**

Run: `node -c apps/web/app.js`
Expected: Syntax OK (exits with code 0).

- [ ] **Step 3: Commit JS controller changes**

```bash
git add apps/web/app.js
git commit -m "feat(web): update app.js controller with DAG layout and live search"
```

---

### Task 4: End-to-End Verification & Walkthrough

**Files:**
- Test: `tests/integration/test_api_endpoints.py` (or existing API tests)

- [ ] **Step 1: Run existing automated tests to ensure API contracts remain unbroken**

Run: `pytest tests/unit tests/integration -v`
Expected: All tests pass.

- [ ] **Step 2: Verify static web files build and serve cleanly**

Run: `python -m py_compile tests/unit/test_*.py`
Expected: Exits with code 0.

- [ ] **Step 3: Document walkthrough in walkthrough.md**
