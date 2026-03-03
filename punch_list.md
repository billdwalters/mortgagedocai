# MortgageDocAI — Punch List

Items to address. Grouped by category, roughly prioritized within each group.

---

## Web UI — High Impact

### 2. Loan summary dashboard after processing
Show a quick-glance view when a loan is selected: PASS/FAIL/UNKNOWN decision badge, DTI ratio, monthly income total, condition count. Currently you have to open raw JSON artifacts to see any of this.

### 3. Conditions checklist view
`conditions.json` has structured data (category, timing, description, citations) but is only visible as raw JSON in the artifacts tab. Display as a formatted, categorized table or checklist.

### 4. Income & DTI panel
`dti.json` and `income_analysis.json` are generated but never displayed in the UI. Show income sources, liabilities, PITIA, front-end and back-end DTI ratios in a readable card/table layout.

### 5. Decision explanation display
`decision.md` and `decision.json` are generated but not surfaced. Show the UW decision with its reasoning prominently — this is the main output a mortgage professional cares about.

### 6. Markdown rendering for answer.md and decision.md
These files are written as markdown but displayed as raw text in the artifact preview. Add a lightweight markdown renderer (e.g. markdown-it or showdown.js) for formatted display.

### 7. Document inventory
No visibility into which documents were ingested, page counts, or if expected documents are missing (pay stubs, tax returns, W-2s, etc.). `intake_manifest.json` and `processing_run.json` have this data — surface it.

---

## Web UI — Medium Impact

### ~~8. Replace alert() boxes with inline feedback~~ DONE (2026-03-03)
~~Errors currently trigger browser alert() popups (blocking, ugly). Replace with inline styled messages using the existing `.source-validation-msg` pattern.~~
Replaced all 4 `alert()` calls with `showInlineMsg()`/`clearInlineMsg()` helpers. Auto-clears after 6s. Uses existing `.source-validation-msg` CSS classes.

### 9. Show processing duration and per-step timing
Job records have `started_at_utc` and `finished_at_utc` but UI doesn't display elapsed time. PHASE markers could also show per-step duration (Intake: 5s, Process: 45s, etc.).

### 10. Artifact file metadata
API returns `size_bytes` and `mtime_utc` for each artifact file but UI ignores them. Show file size and "updated 2 minutes ago" timestamps.

### ~~11. Disable buttons during loading + spinner feedback~~ DONE (2026-03-03)
~~Refresh Loans and Process Loan buttons don't disable while working. No spinner or loading message shown during async operations.~~
View Artifacts: disables + shows "Loading..." during fetch, re-enables in finally. Chat Send: disables during processing, re-enables in finally and early-return paths. Added `.btn-secondary:disabled` and `.chat-send-row button:disabled` CSS rules.

### 12. Better empty states
If no loans found, the loan list is blank. Show "No loans found. Check your settings and connection." Same for empty artifact lists, empty chat, etc.

### 13. Show which profiles have outputs
Don't offer a profile in the chat dropdown if it hasn't been run yet for this loan/run. Check artifact index to determine available profiles.

### 14. Copy to clipboard on JSON preview
Add a copy button to artifact preview panels so users can grab JSON without selecting text.

### ~~15. Human-readable timestamps~~ DONE (2026-03-03)
~~Show "Last processed: 3 days ago" or "Feb 26, 2026 at 6:07 AM" instead of raw run_id format like `2026-02-26T060725Z`.~~
Added `formatTimestamp()` utility that parses both run_id style (`2026-02-26T060725Z`) and ISO. Applied at all 6 locations: loan list (2), overview last-processed (2), progress started/finished, summary dashboard generated_at_utc. Details run_id shows raw + formatted in parens.

---

## Web UI — Nice to Have

### 16. Run history and comparison
Show all runs for a loan, not just the latest. Allow comparing outputs between runs to see what changed.

### 17. Batch processing
Queue multiple loans for processing at once instead of one at a time.

### 18. Search and filter loans
Loan list is flat and unsorted. Add search by loan ID, sort by status/date, or filter by needs-processing.

### 19. Export / report generation
Generate a clean summary PDF or printable report from the analysis outputs.

### 20. Job queue depth indicator
Show how many jobs are queued when multiple are submitted.

### 21. Mobile responsiveness
Layout breaks on small screens; touch targets too small (should be 44x44px minimum). Chat input row wraps awkwardly on narrow viewports.

### 22. Keyboard navigation
Can't arrow through the loan list; no visible focus indicators on buttons or interactive elements.

### 23. Light mode toggle
UI is dark-only. Some users may prefer a light theme.

---

## Infrastructure / Backend

### 1. Orphaned data cleanup for closed loans

**Problem:** When a loan folder is moved out of the source root (e.g. to a "closed loans" folder), it disappears from the UI on Refresh Loans, but the processed data remains:
- **Qdrant** — embedded vectors for closed loans persist in the collection
- **TrueNAS** — chunk files (`nas_chunk`), retrieval packs, and analysis outputs (`nas_analyze`) remain on disk
- **NAS Ingest** — staged intake files (`nas_ingest`) remain on disk

**Desired:** A cleanup mechanism that identifies orphaned loan data (loans no longer present in the active source folder) and purges the associated Qdrant vectors and NAS artifacts.

**Considerations:**
- Must be safe — no accidental deletion of loans that are temporarily moved or being reorganized
- Could be a dry-run-first CLI script that lists orphans before deleting
- Qdrant deletion would filter by `loan_id` in payload
- NAS deletion would remove `tenants/<tenant>/loans/<loan_id>/` trees

### 24. Server migration prep
Document the full migration procedure for moving to the new GPU server. Verify `bootstrap_mortgagedocai.sh --install` covers everything, test on clean machine if possible.

### 25. Extraction accuracy tuning (post-GPU migration)
Once running on GPU server with faster inference: batch-process 3-5 diverse loans, review output quality, tune LLM prompts, retrieval parameters, and regex extraction patterns based on real results.
