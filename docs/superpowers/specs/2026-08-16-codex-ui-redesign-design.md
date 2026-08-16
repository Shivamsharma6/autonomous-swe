# AutoSWE Control Plane - UI Redesign Specification (Open Code Codex Aesthetic)

## 1. Executive Summary

This document specifies the comprehensive redesign of the **AutoSWE Control Plane UI** (`apps/web`). The objective is to elevate the web interface to match the aesthetic, ergonomics, and high-density telemetry of modern state-of-the-art AI developer tools (such as Open Code, OpenAI Codex, Linear, Windsurf, and Claude Code).

The redesign delivers:
- **Obsidian & Zinc Aesthetic**: A refined dark palette with glassmorphic layers, high-contrast borders, glowing status beacons, and crisp typography.
- **Mission Control Launchpad**: Streamlined project registration and workflow launch with local repository intelligence (automatic HEAD and commit detection), prompt preset cards, and recent runs history.
- **Dynamic Topological DAG Canvas**: Multi-stage graph visualizer with SVG Bezier curves, animated running pulses, task capability badges, dependency counts, and stage progress bars.
- **Governed Approvals Deck**: High-visibility safety cards with risk badges, call hashes, expiration countdowns, and streamlined one-click decision flows.
- **Verified Artifacts & Code Diff Viewer**: Content-addressed artifact browser with syntax-highlighted diffs (+/- line indicators), copy actions, and instant file downloads.
- **Live Terminal & Immutable Audit Stream**: Real-time event log with category filter pills, instant text search, auto-scroll toggle, and collapsible JSON payloads.
- **Task Inspector Drawer & Dialogs**: Native HTML5 `<dialog>` modal and slide-over inspector drawer with smooth backdrop blur, rich metadata grid, and tabbed task event correlation.

---

## 2. Design System & Visual Tokens

### 2.1 Color Palette & Theme
- **Canvas / Background**: `--bg-canvas: #07090e;` (Deep Obsidian)
- **Base Surface**: `--bg-surface: #0d1117;` (Dark Slate)
- **Elevated Surface**: `--bg-surface-elevated: #161b22;` (Raised Card Surface)
- **Interactive Surface / Hover**: `--bg-surface-hover: #1f2631;` (Card Hover)
- **Input Background**: `--bg-input: #0a0e14;` (Sunken Field Surface)
- **Glass Frosted Backdrop**: `rgba(13, 17, 23, 0.82)` with `backdrop-filter: blur(16px)`

### 2.2 Borders & Accents
- **Subtle Border**: `--border-subtle: rgba(255, 255, 255, 0.08);`
- **Medium Border**: `--border-medium: rgba(255, 255, 255, 0.14);`
- **Bright / Focus Border**: `--border-bright: rgba(56, 189, 248, 0.5);`
- **Glow Accents**:
  - Emerald Pulse: `#10b981` (Run Success / Platform Ready)
  - Electric Cyan: `#06b6d4` / `#38bdf8` (Active Task / Streaming)
  - Indigo / Violet: `#6366f1` / `#8b5cf6` (Architect & Planning)
  - Amber: `#f59e0b` (Expiring Approvals / Warning)
  - Rose: `#f43f5e` (Failed Task / Danger / Rejected)

### 2.3 Typography & Metrics
- **UI Font**: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;`
- **Code / Telemetry Font**: `"JetBrains Mono", "SF Mono", Menlo, Monaco, Consolas, monospace;`
- **Letter Spacing**: `-0.02em` for headlines, `0.05em` for uppercase tags and pills.

---

## 3. UI Component Specifications

### 3.1 Top Navigation Bar (Codex Header)
- **Brand Group**: AutoSWE logo with gradient glow badge, version tag, and live environment indicator (`Production`).
- **Global Command Search Bar**: Centered search field (`/` or `⌘K` to focus) with run ID quick lookup and instant clear.
- **Live Platform Health Widget**: Connected status dot with hover tooltip detailing dependencies (PostgreSQL, Redis, Sandbox Manager, LLM Provider, UAMS).
- **Operator Security Key Button**: Direct access to configure bearer token with connected indicator.

### 3.2 Mission Control Launchpad (Initiation Section)
- **Header Banner**: Sleek title and tagline with quick capability pills (`Dynamic DAG`, `Sandboxed Docker`, `Governed Approvals`).
- **Two-Column Form Grid**:
  1. **Repository Setup Card**: Project name, source path, repository browse button (folder picker with branch and git HEAD SHA auto-detection), default branch, and registered project badge.
  2. **Workflow Launch Card**: Engineering Goal textarea with preset quick-chips (e.g. *Bugfix & Tests*, *Feature Development*, *Security Hardening*, *Refactor*), baseline git commit SHA input (with auto-fill button), and glowing "Launch Agentic Run" button.
- **Recent Runs Strip**: Quick-access chips for recently opened or launched runs saved in local storage.

### 3.3 Active Run Dashboard
- **Run Status Header**:
  - State pill badge (`PLANNING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`).
  - Engineering goal headline with run ID badge and copy button.
  - Live stream indicator with animated glowing dot (WebSocket / Polling status).
  - Refresh button and "New Run" shortcut.
- **Telemetry HUD Grid (4 Glass Cards)**:
  1. **Workflow State**: State title, active stage, and elapsed duration counter.
  2. **DAG Progress**: Active plan revision, completed vs. total task count, and visual progress bar.
  3. **Model Tokens**: Total tokens consumed, breakdown of prompt input vs. completion output.
  4. **Model Cost**: Authoritatively tracked USD spend formatted to 4 decimal places.

### 3.4 Dynamic Task DAG Visualizer
- **Topological Stage Columns**: Grouped by stage (`Stage 1: Discovery & Research`, `Stage 2: Architecture & Planning`, `Stage 3: Implementation`, `Stage 4: Verification & Test`, `Stage 5: Review & Finalization`).
- **Stage Column Progress**: Header with stage name, task count pill, and completion percentage.
- **Task Cards**:
  - Capability tag (`ARCHITECT`, `CODER`, `TESTER`, `SECURITY`, `REVIEWER`).
  - Status indicator with animated gradient border on `RUNNING` tasks.
  - Task title with concise summary.
  - Dependency pills showing prerequisite task IDs.
  - Hover elevation with click action to open slide-over inspector.
- **Dynamic Bezier Connectors**: SVG layer rendering smooth cubic Bezier curves between parent and child task nodes with active animated dash array for in-flight dependencies.

### 3.5 Governed Approvals & Verified Artifacts (Side Panels)
- **Governed Approvals Deck**:
  - Prominent pending count badge.
  - Warning card displaying tool name, high/critical risk badge, call hash snippet, and expiration countdown.
  - One-click **Approve** (primary emerald) and **Reject** (danger rose) buttons with operator prompt.
- **Verified Artifacts Explorer**:
  - List of generated artifacts with media type tags (`DIFF`, `TEST_REPORT`, `METRICS`, `CODE`).
  - Formatted file size and SHA-256 integrity hash badge.
  - Action buttons: Quick preview modal and direct file download.

### 3.6 Live Terminal & Immutable Audit Stream
- **Category Filter Pills**: `All Events`, `Tasks`, `Tools`, `Approvals`, `Agent Reasoning`.
- **Live Search Field**: Real-time filtering by event type or payload text.
- **Terminal Feed**: Monospaced log stream with timestamps, highlighted event types, and pretty-printed expandable JSON payloads.
- **Auto-Scroll Toggle**: Floating toggle to lock/unlock auto-scrolling to newest events.

### 3.7 Slide-Over Task Inspector & Modals
- **Task Inspector Drawer**:
  - Smooth right slide-in with frosted backdrop.
  - Metadata grid: Task ID, State, Assigned Capability, Priority, Dependencies, Plan Revision.
  - Markdown goal / specification view.
  - Correlated task event log.
- **Artifact Diff Viewer Modal**:
  - Syntax-highlighted code/diff viewer with line numbering and green `+` / red `-` indicators.
  - Download object button.
- **Operator Auth Modal**:
  - Clean modal for `AUTOSWE_ADMIN_TOKEN` entry and session storage persistence.

---

## 4. Architecture & Implementation Plan

### 4.1 Files to Modify
- `apps/web/index.html`: Modern semantic HTML5 markup, accessible landmarks, dialogs, SVG icons, and telemetry layout.
- `apps/web/styles.css`: Complete redesign of design tokens, dark obsidian theme, typography, animations, DAG SVG connections, glassmorphic cards, and responsive media queries.
- `apps/web/app.js`: Enhanced client-side state management, preset prompt chips, recent runs cache, copy utilities, search filtering, diff formatting, and robust WebSocket / polling resilience.

### 4.2 Compatibility & Safety
- Full compatibility with all existing backend endpoints (`/api/v1/projects`, `/api/v1/runs`, `/api/v1/tasks`, `/api/v1/approvals`, `/api/v1/artifacts`, `/api/v1/events`, `/health/ready`).
- No external JS frameworks required; zero npm dependency overhead; instant browser rendering.
- Strict CSP compliance (no external scripts or unsafe eval).

---

## 5. Verification Plan
- Verify page renders smoothly in browser with dark theme, glass surfaces, and responsive breakpoints.
- Test repository folder picker with automatic branch and HEAD commit detection.
- Test launching a run and verifying DAG layout, Bezier connections, and live event updates.
- Test approval decision flow and artifact diff inspection.
