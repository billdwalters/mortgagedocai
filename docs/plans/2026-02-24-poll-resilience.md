# Poll Resilience — Disconnect & Stale Detection

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent silent UI freeze during long-running jobs if the API goes down or stops responding — show a "Disconnected / Stale" banner with a Retry button inside the Job Progress panel.

**Architecture:** Rewrite `startJobPolling` in `webui/app.js` to track `consecutiveFailures`, `lastOkAtMs`, and a job-state fingerprint (`lastChangeAtMs`). On failure threshold or stale timeout, reveal a pre-existing `<div id="poll-warning">` element with a retry button. On successful poll or retry, hide the banner. No API changes, no HTML changes beyond one new `<div>`.

**Tech Stack:** Vanilla JavaScript (ES5-style var/function), CSS custom properties, existing `el()` / `apiJson()` / `parsePhaseLines()` helpers.

---

### Task 1: Add `#poll-warning` element to `index.html` and `.warning` CSS to `styles.css`

**Files:**
- Modify: `webui/index.html` (after `<div id="stepper">`, inside `#progress-panel`)
- Modify: `webui/styles.css` (append at end)

**Step 1: Insert the poll-warning div into `index.html`**

Locate this block inside `#progress-panel` (around line 80–81):
```html
        <div id="stepper" class="stepper"></div>
        <details class="logs-details">
```

Insert `<div id="poll-warning" class="warning hidden"></div>` between those two lines:
```html
        <div id="stepper" class="stepper"></div>
        <div id="poll-warning" class="warning hidden"></div>
        <details class="logs-details">
```

**Step 2: Append `.warning` CSS to `webui/styles.css`**

Append after the last line of the file:
```css

/* ——— Poll disconnect / stale warning banner ——— */
.warning {
  border-left: 3px solid var(--warn);
  background: transparent;
  padding: 0.5rem 0.75rem;
  margin: 0.5rem 0;
  font-size: 0.875rem;
  color: var(--text);
}
.warning.hidden { display: none; }
.warning p { margin: 0 0 0.35rem; }
.warning button {
  display: inline-block;
  margin-top: 0.35rem;
  padding: 0.25rem 0.75rem;
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--muted);
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.8125rem;
}
.warning button:hover { opacity: 0.85; }
```

**Step 3: Verify CSS brace balance**

```bash
python3 -c "
with open('webui/styles.css') as f:
    css = f.read()
opens = css.count('{')
closes = css.count('}')
print('OK' if opens == closes else f'MISMATCH {opens}/{closes}')
"
```
Expected: `OK`

**Step 4: Commit**

```bash
git add webui/index.html webui/styles.css
git commit -m "feat(ui): add poll-warning element and warning CSS"
```

---

### Task 2: Rewrite `startJobPolling` with resilience logic

**Files:**
- Modify: `webui/app.js` — replace `startJobPolling` (lines 577–614) and add two constants near line 24

**Context:**
- `startJobPolling` currently lives at lines 577–614. It uses `setInterval` + a completely silent `.catch(function(){})`. The goal is to replace the whole function (keep identical `setJobFields` + `renderStepper` behaviour) and add failure tracking / stale detection.
- `el(id)` is the `document.getElementById` shortcut defined earlier in the file.
- `parsePhaseLines(stdout)` returns `[{name, ts}]` — defined at line 121.
- `POLL_INTERVAL_MS = 2000` is already defined at line 24.
- `selectedLoanId`, `selectedRunId`, `lastProcessedCache` are outer-scope closure vars.

**Step 1: Add two constants after `POLL_INTERVAL_MS` (line 24)**

Locate:
```javascript
  const POLL_INTERVAL_MS = 2000;
```
Insert the two new constants immediately after:
```javascript
  const POLL_INTERVAL_MS = 2000;
  const STALL_MS = 120000;   // 2 min without job-state change → stale
  const MAX_FAILURES = 10;   // consecutive poll failures → stop
```

**Step 2: Replace the entire `startJobPolling` function**

The current function spans from `function startJobPolling(jobId) {` to the closing `}` that follows `poll()` on line 614. Replace that entire function with:

```javascript
  function startJobPolling(jobId) {
    var consecutiveFailures = 0;
    var lastOkAtMs = Date.now();
    var lastChangeAtMs = Date.now();
    var lastFingerprint = null;
    var pollTimer = null;
    var stopped = false;

    function jobFingerprint(job) {
      if (!job) return "";
      var phases = parsePhaseLines(job.stdout || "");
      var lastPhase = phases.length ? phases[phases.length - 1].name : "";
      return (job.status || "") + "|" + String((job.stdout || "").length) + "|" + lastPhase +
        "|" + (job.started_at_utc || "") + "|" + (job.finished_at_utc || "");
    }

    function showPollWarning(msg) {
      var warningEl = el("poll-warning");
      if (!warningEl) return;
      warningEl.innerHTML = "";
      var p = document.createElement("p");
      var lastOkStr = new Date(lastOkAtMs).toLocaleTimeString();
      p.textContent = msg + " Last successful poll: " + lastOkStr +
        ". Consecutive failures: " + consecutiveFailures + ".";
      warningEl.appendChild(p);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Retry polling";
      btn.addEventListener("click", retryPolling);
      warningEl.appendChild(btn);
      warningEl.classList.remove("hidden");
    }

    function hidePollWarning() {
      var warningEl = el("poll-warning");
      if (warningEl) { warningEl.classList.add("hidden"); warningEl.innerHTML = ""; }
    }

    function stopPolling() {
      stopped = true;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function retryPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      consecutiveFailures = 0;
      lastOkAtMs = Date.now();
      lastChangeAtMs = Date.now();
      stopped = false;
      hidePollWarning();
      poll();
      pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    }

    function setJobFields(job) {
      if (!job) return;
      if (el("progress-status-value")) el("progress-status-value").textContent = job.status || "—";
      if (el("progress-started")) el("progress-started").textContent = (job.started_at_utc || "—");
      if (el("progress-finished")) el("progress-finished").textContent = (job.finished_at_utc || "—");
      const stdout = job.stdout != null ? job.stdout : "";
      if (el("log-stdout")) el("log-stdout").textContent = stdout || "(empty)";
      if (el("log-stderr")) el("log-stderr").textContent = (job.stderr != null ? job.stderr : "") || "(empty)";
      const err = job.error;
      const errEl = el("log-error");
      if (errEl) {
        errEl.textContent = err || "";
        errEl.hidden = !err;
      }
      const phases = parsePhaseLines(stdout);
      renderStepper(phases);
    }

    function poll() {
      if (stopped) return;
      apiJson("/jobs/" + encodeURIComponent(jobId))
        .then(function (job) {
          consecutiveFailures = 0;
          lastOkAtMs = Date.now();

          var fp = jobFingerprint(job);
          if (fp !== lastFingerprint) {
            lastChangeAtMs = Date.now();
            lastFingerprint = fp;
          }

          setJobFields(job);
          hidePollWarning();

          var status = (job && job.status) ? job.status : "";

          if (status === "PENDING" || status === "RUNNING" || status === "QUEUED") {
            if (Date.now() - lastChangeAtMs > STALL_MS) {
              showPollWarning("Job appears stalled (no progress updates for 2m).");
              stopPolling();
              return;
            }
          }

          if (status === "SUCCESS" || status === "FAIL") {
            stopPolling();
            if (status === "SUCCESS" && job.run_id && selectedLoanId) {
              selectedRunId = job.run_id;
              lastProcessedCache[selectedLoanId] = { run_id: job.run_id, generated_at_utc: job.run_id };
              if (el("overview-last-processed")) el("overview-last-processed").textContent = job.run_id;
              if (el("details-run-id")) el("details-run-id").textContent = "run_id: " + job.run_id;
            }
          }
        })
        .catch(function () {
          consecutiveFailures++;
          var sinceLastOk = Date.now() - lastOkAtMs;
          if (consecutiveFailures >= 3 || sinceLastOk > 15000) {
            showPollWarning(
              "Connection lost to server \u2014 polling paused. Check VPN/Tailscale or API service."
            );
          }
          if (consecutiveFailures >= MAX_FAILURES) {
            stopPolling();
          }
        });
    }

    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    poll();
  }
```

**Step 3: Verify JS syntax**

```bash
node --check webui/app.js && echo "OK"
```
If `node` is unavailable, verify brace/paren balance:
```bash
python3 -c "
with open('webui/app.js') as f:
    src = f.read()
print('braces:', src.count('{'), '/', src.count('}'))
print('parens:', src.count('('), '/', src.count(')'))
"
```
Expected: both pairs equal.

**Step 4: Commit**

```bash
git add webui/app.js
git commit -m "feat(ui): add disconnect/stale detection to job polling"
```

---

### Task 3: Acceptance verification

**Files:** None (verification only)

**Step 1: Start the dev server**

Use `preview_start` with the `loan-api` configuration from `.claude/launch.json`.

**Step 2: Verify server health and static assets**

```bash
curl -s http://127.0.0.1:8000/health
# Expected: {"status":"ok"}

curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/static/app.js
# Expected: 200

curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/static/styles.css
# Expected: 200
```

**Step 3: Verify HTML element and CSS classes are present**

```bash
python3 -c "
with open('webui/index.html') as f:
    html = f.read()
with open('webui/styles.css') as f:
    css = f.read()
with open('webui/app.js') as f:
    js = f.read()
checks = [
    ('poll-warning div in HTML', 'id=\"poll-warning\"' in html),
    ('warning class in HTML', 'class=\"warning hidden\"' in html),
    ('.warning CSS rule', '.warning {' in css),
    ('.warning.hidden', '.warning.hidden' in css),
    ('.warning button', '.warning button' in css),
    ('STALL_MS constant', 'const STALL_MS' in js),
    ('MAX_FAILURES constant', 'const MAX_FAILURES' in js),
    ('consecutiveFailures var', 'consecutiveFailures' in js),
    ('lastOkAtMs var', 'lastOkAtMs' in js),
    ('lastChangeAtMs var', 'lastChangeAtMs' in js),
    ('jobFingerprint fn', 'function jobFingerprint' in js),
    ('showPollWarning fn', 'function showPollWarning' in js),
    ('hidePollWarning fn', 'function hidePollWarning' in js),
    ('retryPolling fn', 'function retryPolling' in js),
    ('stopPolling fn', 'function stopPolling' in js),
    ('Retry polling button text', '\"Retry polling\"' in js),
    ('stale message', 'appears stalled' in js),
    ('connection lost message', 'Connection lost' in js),
]
all_ok = True
for label, ok in checks:
    print(('OK' if ok else 'MISSING') + ': ' + label)
    if not ok:
        all_ok = False
print()
print('ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED')
"
```
Expected: all `OK`, then `ALL CHECKS PASSED`.

**Step 4: Git log check**

```bash
git log --oneline -3
```
Expected (most recent first):
1. `feat(ui): add disconnect/stale detection to job polling`
2. `feat(ui): add poll-warning element and warning CSS`

---

## Acceptance Criteria Summary

| # | Criteria |
|---|---|
| 1 | Normal job run: completes with no warning shown |
| 2 | API down while RUNNING: warning appears within ~6s, polling stops at MAX_FAILURES (10) |
| 3 | Retry: click "Retry polling" → banner hides, polling resumes |
| 4 | Stale: job stuck for 2m → "Job appears stalled" banner, polling stops |
| 5 | `/health` returns `{"status":"ok"}` |
| 6 | `/ui/static/app.js` returns 200 |
| 7 | `/ui/static/styles.css` returns 200 |
| 8 | All 18 inline checks PASS |
