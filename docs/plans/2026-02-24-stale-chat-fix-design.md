# Design: Fix Stale Chat / Query Answers

**Date:** 2026-02-24
**Status:** Approved
**Scope:** `webui/app.js`, `webui/styles.css` — no API or HTML changes.

---

## Problem

`pollForQueryJobAnswer` has two defects:

1. **No error details on FAIL.** When a job reaches `FAIL` status (or times out), the function resolves `null`, and `sendQuestion` displays `"(No answer returned)"` — no error text, no stderr, no stdout. The user has no way to understand what went wrong without opening the browser console or checking the server logs.

2. **No freshness guard on SUCCESS.** On `SUCCESS`, the function immediately fetches `answer.json` (or `answer.md`) without checking whether the artifact was produced by *this* job. On a slow filesystem or after a re-run, a stale artifact from a previous job can be displayed as if it were the current answer.

---

## Approach

**Option A — result object (chosen).** Refactor `pollForQueryJobAnswer` to return `{ ok, answer, job }` rather than a raw string or `null`. This gives `sendQuestion` full context to branch on success vs. failure without adding new global state.

---

## Data Flow

```
sendQuestion
  └─ startQueryJob(...)                       → jobId
  └─ pollForQueryJobAnswer(jobId, profile, runId)
        │
        ├─ status === "FAIL" or timeout
        │     └─ resolve { ok: false, answer: null, job }
        │
        ├─ status === "SUCCESS"
        │     └─ GET .../artifacts             → artifactList
        │     └─ find profile entry for answer.json
        │     └─ entry.mtime_utc >= job.started_at_utc?
        │           ├─ NO  → resolve { ok: false, answer: null,
        │           │                  job: { ...job, error: "Artifact is older than this job; refusing to display." } }
        │           └─ YES → fetch answer.json text
        │                 └─ resolve { ok: true, answer: text, job }
        │
        └─ network error
              └─ resolve { ok: false, answer: null, job: null }

sendQuestion (continued)
  └─ result.ok
        ├─ true  → appendMessage("assistant", result.answer)
        └─ false → appendErrorPanel(result.job)
```

**mtime comparison:** Both strings are `YYYY-MM-DDTHH:MM:SSZ`; lexicographic comparison is safe and equivalent to chronological order.

---

## Error Panel — `appendErrorPanel(job)`

Rendered as a chat bubble with a distinct red left-border callout, appended to `#chat-messages`.

```
┌─────────────────────────────────────────────────────────┐
│ ║  ● FAIL                                               │  ← status badge
│ ║  Last phase: SUMMARIZE  2026-02-24T14:03:12Z         │  ← last PHASE line
│ ║                                                       │
│ ║  error message text here                              │  ← job.error
│ ║                                                       │
│ ║  stderr                                               │
│ ║  ┌──────────────────────────────────────────────┐    │
│ ║  │ Traceback (most recent call last):            │    │  ← <pre> scrollable
│ ║  │   ...                                         │    │
│ ║  └──────────────────────────────────────────────┘    │
│ ║                                                       │
│ ║  ▶ stdout (last 200 lines)                           │  ← <details> collapsed
└─────────────────────────────────────────────────────────┘
```

- `job` may be `null` (network error); panel shows generic "Job failed — no details available."
- All fields (`error`, `stderr`, `stdout`) are optional; sections are omitted when empty/null.
- PHASE lines are extracted from `job.stdout` using the existing `parsePhaseLines` helper (or equivalent regex `PHASE:(\S+)\s+(\S+)`).
- `job.stdout` is truncated to the last ~200 lines before display to avoid flooding the DOM.

---

## CSS

Append to `webui/styles.css`:

```css
.chat-msg-error {
  border-left: 3px solid var(--error);
  background: transparent;
}
.chat-msg-error pre {
  background: var(--bg-alt, #1e1e1e);
  color: var(--text, #ccc);
  font-size: 0.75rem;
  padding: 0.5rem;
  border-radius: 4px;
  max-height: 12rem;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
```

---

## Files Changed

| File | Change |
|---|---|
| `webui/app.js` | Refactor `pollForQueryJobAnswer` return type; add `appendErrorPanel`; update `sendQuestion` |
| `webui/styles.css` | Add `.chat-msg-error` and `.chat-msg-error pre` rules |

No changes to `loan_api.py`, `webui/index.html`, or any server-side endpoints.

---

## Acceptance Criteria

1. On FAIL: error panel appears in chat with red left border, job status, last PHASE line, error text, and scrollable stderr `<pre>`.
2. On SUCCESS with fresh artifact: assistant message appears as before.
3. On SUCCESS with stale artifact: error panel appears with message "Artifact is older than this job; refusing to display."
4. On network error: generic error panel appears (no crash).
5. No regression: existing loans that succeed still display answers correctly.
