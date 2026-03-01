# Stale Chat / Query Answer Fix — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `pollForQueryJobAnswer` show a rich error panel on FAIL and guard against stale artifacts on SUCCESS by comparing the artifact's `mtime_utc` to `job.started_at_utc`.

**Architecture:** Two JS changes in `webui/app.js`: (1) a new `appendErrorPanel(job)` helper that renders a structured error callout in the chat, and (2) a refactored `pollForQueryJobAnswer` that returns `{ ok, answer, job }` instead of a raw string, validates artifact freshness on SUCCESS, and captures job details on FAIL. One CSS addition to `webui/styles.css`. No API changes, no HTML changes.

**Tech Stack:** Vanilla JavaScript (ES5-style inside existing IIFEs), CSS custom properties, FastAPI (existing), Python 3

---

### Task 1: Add `.chat-msg-error` CSS to `webui/styles.css`

**Files:**
- Modify: `webui/styles.css` (append after line 544)

**Step 1: Append the CSS rules**

Open `webui/styles.css` and add the following after the last line (544):

```css

/* ——— Query job error panel ——— */
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
  margin: 0.25rem 0 0;
}
```

**Step 2: Verify no CSS syntax error**

```bash
python3 -c "
import re, sys
with open('webui/styles.css') as f:
    css = f.read()
opens = css.count('{')
closes = css.count('}')
if opens != closes:
    print(f'MISMATCH: {opens} {{ vs {closes} }}')
    sys.exit(1)
print('OK')
"
```
Expected: `OK`

---

### Task 2: Add `appendErrorPanel` helper to `webui/app.js`

**Files:**
- Modify: `webui/app.js` (insert after `appendMessage` at line 733)

**Context:** `appendMessage` ends at line 733 with `}`. The new function goes directly after it, still inside the `initChat()` IIFE that starts at line 716.

**Step 1: Insert `appendErrorPanel` after line 733**

After the closing `}` of `appendMessage` (line 733), insert:

```javascript
    function appendErrorPanel(job) {
      if (!messagesEl) return;
      var div = document.createElement("div");
      div.className = "chat-msg chat-msg-error";
      var inner = document.createElement("div");
      inner.className = "chat-msg-content";
      if (!job) {
        var noInfo = document.createElement("p");
        noInfo.textContent = "Job failed \u2014 no details available.";
        inner.appendChild(noInfo);
      } else {
        var statusLine = "Status: " + (job.status || "UNKNOWN");
        var phases = parsePhaseLines(job.stdout || "");
        var lastPhase = phases.length ? phases[phases.length - 1] : null;
        if (lastPhase) {
          statusLine += "\u2003Last phase: " + lastPhase.name + "  " + lastPhase.ts;
        }
        var header = document.createElement("p");
        header.textContent = statusLine;
        inner.appendChild(header);
        if (job.error) {
          var errorEl = document.createElement("p");
          errorEl.textContent = job.error;
          inner.appendChild(errorEl);
        }
        if (job.stderr) {
          var stderrLabel = document.createElement("p");
          stderrLabel.textContent = "stderr";
          inner.appendChild(stderrLabel);
          var stderrPre = document.createElement("pre");
          stderrPre.textContent = job.stderr;
          inner.appendChild(stderrPre);
        }
        if (job.stdout) {
          var stdoutLines = job.stdout.split("\n");
          var last200 = stdoutLines.slice(-200).join("\n");
          var details = document.createElement("details");
          var summary = document.createElement("summary");
          summary.textContent = "stdout (last " + Math.min(stdoutLines.length, 200) + " lines)";
          details.appendChild(summary);
          var stdoutPre = document.createElement("pre");
          stdoutPre.textContent = last200;
          details.appendChild(stdoutPre);
          inner.appendChild(details);
        }
      }
      div.appendChild(inner);
      messagesEl.appendChild(div);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
```

**Step 2: Verify JS syntax by loading in Node**

```bash
node --check webui/app.js && echo "OK"
```
Expected: `OK`

If `node` is not available:
```bash
python3 -c "
import subprocess, sys
r = subprocess.run(['node', '--check', 'webui/app.js'], capture_output=True, text=True)
print(r.stdout or r.stderr or 'OK')
sys.exit(r.returncode)
"
```

**Step 3: Commit**

```bash
git add webui/styles.css webui/app.js
git commit -m "feat(ui): add appendErrorPanel and chat-msg-error CSS"
```

---

### Task 3: Refactor `pollForQueryJobAnswer` to return `{ ok, answer, job }`

**Files:**
- Modify: `webui/app.js` lines 813–848

**Step 1: Replace `pollForQueryJobAnswer` (lines 813–848) with the new implementation**

The old function (lines 813–848) resolves a raw string or `null`. Replace the **entire function body** with:

```javascript
    function pollForQueryJobAnswer(jobId, profile, runId) {
      return new Promise(function (resolve) {
        var attempts = 0;
        var maxAttempts = 120;
        function poll() {
          attempts++;
          apiJson("/jobs/" + encodeURIComponent(jobId)).then(function (job) {
            var status = (job && job.status) ? job.status : "";
            if (status === "SUCCESS") {
              var base = getBaseUrl();
              var tenant = getTenantId();
              var artifactsUrl = base + "/tenants/" + encodeURIComponent(tenant) +
                "/loans/" + encodeURIComponent(selectedLoanId) +
                "/runs/" + encodeURIComponent(runId) + "/artifacts";
              function fetchAnswer() {
                var jsonUrl = base + "/tenants/" + encodeURIComponent(tenant) +
                  "/loans/" + encodeURIComponent(selectedLoanId) +
                  "/runs/" + encodeURIComponent(runId) + "/artifacts/" +
                  encodeURIComponent(profile) + "/answer.json";
                apiFetch(jsonUrl).then(function (r) { return r.json(); }).then(function (obj) {
                  var text = (obj && obj.answer) ? obj.answer : (obj ? JSON.stringify(obj) : null);
                  resolve({ ok: true, answer: text, job: job });
                }).catch(function () {
                  resolve({ ok: false, answer: null, job: Object.assign({}, job, { error: "Could not fetch answer artifact." }) });
                });
              }
              apiJson(artifactsUrl).then(function (artifacts) {
                var profileEntry = (artifacts && artifacts.profiles ? artifacts.profiles : []).find(function (p) { return p.name === profile; });
                var fileEntry = profileEntry ? (profileEntry.files || []).find(function (f) { return f.name === "answer.json"; }) : null;
                var mtime = fileEntry ? fileEntry.mtime_utc : null;
                var startedAt = job.started_at_utc || "";
                if (mtime && startedAt && mtime < startedAt) {
                  resolve({ ok: false, answer: null, job: Object.assign({}, job, { error: "Artifact is older than this job; refusing to display." }) });
                  return;
                }
                fetchAnswer();
              }).catch(function () {
                // artifacts check failed — fetch answer anyway (fail-open)
                fetchAnswer();
              });
              return;
            }
            if (status === "FAIL" || attempts >= maxAttempts) {
              var resolvedJob = job;
              if (attempts >= maxAttempts && status !== "FAIL") {
                resolvedJob = Object.assign({}, job, { error: (job && job.error) || "Timed out waiting for job result." });
              }
              resolve({ ok: false, answer: null, job: resolvedJob });
              return;
            }
            setTimeout(poll, POLL_INTERVAL_MS);
          }).catch(function () {
            resolve({ ok: false, answer: null, job: null });
          });
        }
        poll();
      });
    }
```

**Note on mtime comparison:** `mtime` and `started_at_utc` are both ISO 8601 UTC strings (e.g., `"2026-02-24T14:03:12Z"`). Lexicographic comparison is chronologically correct for this format. The check `mtime < startedAt` is only true when the artifact was last written before the job began — i.e., it's a leftover from a prior run.

**Step 2: Verify JS syntax**

```bash
node --check webui/app.js && echo "OK"
```
Expected: `OK`

---

### Task 4: Update `sendQuestion` to branch on `result.ok`

**Files:**
- Modify: `webui/app.js` lines 804–805

**Step 1: Replace the two-line call site**

Old (lines 804–805):
```javascript
        const answer = await pollForQueryJobAnswer(jobId, profile, runId);
        appendMessage("assistant", answer || "(No answer returned)");
```

New:
```javascript
        const result = await pollForQueryJobAnswer(jobId, profile, runId);
        if (result.ok) {
          appendMessage("assistant", result.answer || "(No answer returned)");
        } else {
          appendErrorPanel(result.job);
        }
```

**Step 2: Verify JS syntax**

```bash
node --check webui/app.js && echo "OK"
```
Expected: `OK`

**Step 3: Commit**

```bash
git add webui/app.js
git commit -m "feat(ui): fix stale chat answers — error panel on FAIL, mtime guard on SUCCESS"
```

---

### Task 5: Start dev server and run acceptance checks

**Step 1: Start the preview server**

Use the `loan-api` configuration from `.claude/launch.json` via the preview_start tool (`loan-api`).

Wait for the server to be active (HTTP 200 on `http://localhost:8000/health`).

**Step 2: Check server logs for startup errors**

Verify no import errors or tracebacks on startup.

**Step 3: Acceptance checks via curl**

```bash
# Health
curl -s http://127.0.0.1:8000/health
# Expected: {"status":"ok"}

# UI HTML loads
curl -s http://127.0.0.1:8000/ui | head -3
# Expected: <!DOCTYPE html> ...

# Static JS asset
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/static/app.js
# Expected: 200
```

**Step 4: Manual browser smoke test (optional but recommended)**

Open `http://localhost:8000/ui` in the browser. Verify:
- Page loads without console errors
- Chat section is visible (no JS crash)
- Existing loan selection / source validation still works

**Step 5: Final commit marker**

No new commit needed — Tasks 3 and 4 already committed. Verify `git log --oneline -4` shows:

```
feat(ui): fix stale chat answers — error panel on FAIL, mtime guard on SUCCESS
feat(ui): add appendErrorPanel and chat-msg-error CSS
refactor: move webui/ from scripts/webui to top-level webui/
feat: fix stale source_path caching — validate before runs/start
```

---

## Acceptance Criteria

| # | Criteria | How to verify |
|---|---|---|
| 1 | FAIL job shows error panel with red left border | Trigger a known-failing query job, observe chat |
| 2 | Error panel shows status, last PHASE, error text, stderr, stdout | Inspect rendered DOM |
| 3 | SUCCESS with fresh artifact shows answer as before | Normal query flow |
| 4 | SUCCESS with stale artifact shows "Artifact is older…" error panel | (Manual: only testable with real jobs) |
| 5 | Network error on job poll shows error panel (no crash) | (Manual: disconnect mid-poll) |
| 6 | `GET /health` returns `{"status":"ok"}` after restart | curl check above |
| 7 | `GET /ui` returns HTML | curl check above |
| 8 | `GET /ui/static/app.js` returns 200 | curl check above |
