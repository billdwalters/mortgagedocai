# MortgageDocAI — Punch List

Items to address. Grouped by category, roughly prioritized within each group.

---

## Web UI — High Impact

### ~~2. Loan summary dashboard after processing~~ DONE (2026-03-06)
~~Show a quick-glance view when a loan is selected: PASS/FAIL/UNKNOWN decision badge, DTI ratio, monthly income total, condition count. Currently you have to open raw JSON artifacts to see any of this.~~
Already implemented: `loadSummaryDashboard()` fires 5 parallel fetches on loan selection and renders 4 cards (Underwriting Decision with PASS/FAIL/UNKNOWN badge, DTI Analysis with bar charts, Income Breakdown with line items, Processing Details). Conditions panel renders separately with count and timing summary.

### ~~3. Conditions checklist view~~ DONE (2026-03-05)
~~`conditions.json` has structured data (category, timing, description, citations) but is only visible as raw JSON in the artifacts tab. Display as a formatted, categorized table or checklist.~~
UI was already fully implemented (`renderConditionsPanel()` in app.js, HTML, CSS). Two fixes applied: (1) added `conditions.json` to `PROFILE_FILE_NAMES` in `loan_api.py` so the API serves it; (2) wired `uw_conditions` profile into `run_loan_job.py` pipeline (was never called). Added `STEP12_UW_CONDITIONS` phase marker and stepper label.

### ~~4. Income & DTI panel~~ DONE (2026-03-06)
~~`dti.json` and `income_analysis.json` are generated but never displayed in the UI. Show income sources, liabilities, PITIA, front-end and back-end DTI ratios in a readable card/table layout.~~
Already implemented: `renderDtiCard()` and `renderIncomeCard()` render in the summary dashboard. `dti.json` and `income_analysis.json` are fetched via `fetchArtifactJson()` and displayed as formatted cards. Fixed View Artifacts double-base-URL bug that was preventing artifact preview from working.

### ~~5. Decision explanation display~~ DONE (2026-03-06)
~~`decision.md` and `decision.json` are generated but not surfaced. Show the UW decision with its reasoning prominently — this is the main output a mortgage professional cares about.~~
Already implemented: `renderDecisionCard()` renders the UW decision prominently in the summary dashboard with PASS/FAIL/UNKNOWN badge. `decision.json` is fetched and displayed. Fixed View Artifacts bug that prevented previewing `decision.md` and `decision.json` in the artifacts panel.

### ~~6. Markdown rendering for answer.md and decision.md~~ DONE (2026-03-06)
~~These files are written as markdown but displayed as raw text in the artifact preview. Add a lightweight markdown renderer (e.g. markdown-it or showdown.js) for formatted display.~~
Already implemented: `marked.min.js` v15.0.12 loaded in index.html; `renderMarkdownSafe()` sanitizes and renders markdown. The artifact preview click handler detects `.md` files and renders them via marked. Fixed View Artifacts double-base-URL bug that was causing 404s, preventing markdown preview from working.

### ~~7. Document inventory~~ DONE (2026-03-06)
~~No visibility into which documents were ingested, page counts, or if expected documents are missing (pay stubs, tax returns, W-2s, etc.). `intake_manifest.json` and `processing_run.json` have this data — surface it.~~
Already implemented: `fetchDocumentInventory()` + `renderInventoryCard()` in app.js; "Document Inventory" summary card in index.html. Fetches from `/runs/{run_id}/document_inventory` and renders in the summary dashboard.

---

## Web UI — Medium Impact

### ~~8. Replace alert() boxes with inline feedback~~ DONE (2026-03-03)
~~Errors currently trigger browser alert() popups (blocking, ugly). Replace with inline styled messages using the existing `.source-validation-msg` pattern.~~
Replaced all 4 `alert()` calls with `showInlineMsg()`/`clearInlineMsg()` helpers. Auto-clears after 6s. Uses existing `.source-validation-msg` CSS classes.

### ~~9. Show processing duration and per-step timing~~ DONE (2026-03-08)
~~Job records have `started_at_utc` and `finished_at_utc` but UI doesn't display elapsed time. PHASE markers could also show per-step duration (Intake: 5s, Process: 45s, etc.).~~
Stepper shows per-step duration from consecutive PHASE timestamps. Progress panel shows total elapsed time (live during processing, final on completion). Uses existing `formatDuration()` utility.

### ~~10. Artifact file metadata~~ DONE (2026-03-08)
~~API returns `size_bytes` and `mtime_utc` for each artifact file but UI ignores them. Show file size and "updated 2 minutes ago" timestamps.~~
Artifact file list now shows file size (via `formatBytes`) and timestamp (via `formatTimestamp`) as muted text next to each file link. Added `.artifact-meta` CSS spacing rule.

### ~~11. Disable buttons during loading + spinner feedback~~ DONE (2026-03-03)
~~Refresh Loans and Process Loan buttons don't disable while working. No spinner or loading message shown during async operations.~~
View Artifacts: disables + shows "Loading..." during fetch, re-enables in finally. Chat Send: disables during processing, re-enables in finally and early-return paths. Added `.btn-secondary:disabled` and `.chat-send-row button:disabled` CSS rules.

### ~~12. Better empty states~~ DONE (2026-03-10)
~~If no loans found, the loan list is blank. Show "No loans found. Check your settings and connection." Same for empty artifact lists, empty chat, etc.~~
Artifacts panel shows "No artifacts found for this run." when empty. Chat area shows placeholder text until first message. Loan list and summary dashboard cards already had empty states.

### ~~13. Show which profiles have outputs~~ DONE (2026-03-10)
~~Don't offer a profile in the chat dropdown if it hasn't been run yet for this loan/run. Check artifact index to determine available profiles.~~
Fetches artifact index on loan select and job completion. Profiles without outputs shown as disabled with "(not run)" label. Previous selection preserved if still available.

### ~~14. Copy to clipboard on JSON preview~~ DONE (2026-03-10)
~~Add a copy button to artifact preview panels so users can grab JSON without selecting text.~~
Copy button next to Preview heading works for JSON and markdown content. Shows "Copied!" for 2s on success. New `.btn-small` CSS class.

### ~~15. Human-readable timestamps~~ DONE (2026-03-03)
~~Show "Last processed: 3 days ago" or "Feb 26, 2026 at 6:07 AM" instead of raw run_id format like `2026-02-26T060725Z`.~~
Added `formatTimestamp()` utility that parses both run_id style (`2026-02-26T060725Z`) and ISO. Applied at all 6 locations: loan list (2), overview last-processed (2), progress started/finished, summary dashboard generated_at_utc. Details run_id shows raw + formatted in parens.

---

## Web UI — Nice to Have

### ~~16. Run history and comparison~~ DONE (2026-03-23)
~~Show all runs for a loan, not just the latest. Allow comparing outputs between runs to see what changed.~~
Enhanced `GET /runs` endpoint to return per-run metadata (status, profiles). Added `GET /runs/{run_id}/summary` (compact key outputs) and `GET /runs/compare?run_a=...&run_b=...` (side-by-side diff with deltas). UI: run selector dropdown to switch between runs, expandable run history table with checkboxes, side-by-side comparison panel showing decision/DTI/income/conditions changes with plain-language summary. 12 new tests in `test_run_history.py`.

### ~~17. Batch processing~~ DONE (2026-03-23)
~~Queue multiple loans for processing at once instead of one at a time.~~
Added `POST /tenants/{tenant_id}/batch/process` endpoint accepting up to 50 loans per request, validates source paths and enqueues jobs via existing JobService. UI: checkboxes on every loan in sidebar, batch action bar with "Process Selected", "Select Needing Processing" (auto-checks `needs_reprocess` loans), and "Clear" buttons. Shows queued/skipped count feedback. Uses existing advanced settings (offline_embeddings, top_k, etc.).

### ~~18. Search and filter loans~~ DONE (2026-03-23)
~~Loan list is flat and unsorted. Add search by loan ID, sort by status/date, or filter by needs-processing.~~
Added search input (filters by loan ID or folder name), filter buttons (All / Needs Processing / Done), and sort dropdown (ID asc/desc, newest/oldest). All client-side filtering on already-loaded data. Loan list re-renders on search/filter/sort change, preserving selected state and batch checkboxes.

### ~~19. Export / report generation~~ DONE (2026-03-23)
~~Generate a clean summary PDF or printable report from the analysis outputs.~~
Added `GET /tenants/{t}/loans/{l}/runs/{r}/report` endpoint that renders a printable HTML report via Jinja2 template. Report includes: UW decision with badge, DTI analysis with bar charts, income breakdown with borrowers and items table, conditions grouped by category with timing badges, and document inventory. Print-optimized CSS with page breaks. "Export Report" button in main actions opens report in new tab. Users can Print to PDF (Ctrl+P) for clean PDF output.

### ~~20. Job queue depth indicator~~ DONE (2026-03-23)
~~Show how many jobs are queued when multiple are submitted.~~
Added queue depth badge in header bar that polls `GET /jobs` every 15 seconds. Shows "N jobs queued" count for PENDING/RUNNING/CLAIMED jobs. Badge auto-hides when queue is empty.

### ~~21. Mobile responsiveness~~ DONE (2026-03-23)
~~Layout breaks on small screens; touch targets too small (should be 44x44px minimum). Chat input row wraps awkwardly on narrow viewports.~~
Added comprehensive `@media (max-width: 700px)` styles: sidebar becomes scrollable top section, 44px minimum touch targets on all buttons, chat input/send stack vertically, main actions stack vertically, summary/comparison grids go single-column, header compacts, run history table scrolls horizontally. Viewport meta already present.

### ~~22. Keyboard navigation~~ DONE (2026-03-23)
~~Can't arrow through the loan list; no visible focus indicators on buttons or interactive elements.~~
Added `:focus-visible` outlines on all interactive elements (accent color, 2px). Loan list items are focusable (`tabindex=0`). Arrow Up/Down navigates loan list, Enter selects loan, Space toggles batch checkbox. Keyboard handler on loan list container.

### ~~23. Light mode toggle~~ DONE (2026-03-23)
~~UI is dark-only. Some users may prefer a light theme.~~
Added `body.light` CSS class with overridden CSS variables (bg, surface, text, muted, borders). Theme toggle button in header (sun/moon icon). Preference persisted in `localStorage` as `mdai_theme`. Applies on page load before render.

---

## Infrastructure / Backend

### ~~1. Orphaned data cleanup for closed loans~~ DONE (2026-03-06)
~~Cleanup mechanism for orphaned loan data (source folder removed, NAS/Qdrant data remains).~~
Implemented: `cleanup_orphans.py` CLI (dry-run by default, `--confirm --yes` to delete), Housekeeping UI in web UI (`GET /housekeeping/orphans` scan + `POST /housekeeping/orphans/purge`), skips active jobs, caps 20/request, 10 tests. Code audit hardened: `find_active_jobs` scans per-loan job dirs, `shutil.rmtree` error handling for NAS.

### 24. Server migration prep
Document the full migration procedure for moving to the new GPU server. Verify `bootstrap_mortgagedocai.sh --install` covers everything, test on clean machine if possible.

### 25. Extraction accuracy tuning (post-GPU migration)
Once running on GPU server with faster inference: batch-process 3-5 diverse loans, review output quality, tune LLM prompts, retrieval parameters, and regex extraction patterns based on real results.
