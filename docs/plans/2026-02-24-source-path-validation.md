# Source Path Validation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix stale source_path caching risk — UI must validate source_path against the server before submitting a run, and warn on stale cached paths.

**Architecture:** (A) Fix existing API endpoint response schema to match spec. (B) Add HTML element + CSS classes. (C) Add JS validation helper + wire into Process Loan submit and selectLoan.

**Tech Stack:** Python/FastAPI (loan_api.py), plain HTML/CSS/JS (no build).

---

## Pre-flight: What already exists

- `POST /tenants/{tenant_id}/loans/{loan_id}/source_path/validate` is at `loan_api.py:859–908`.
- `ValidateSourcePathRequest` model exists at `loan_api.py:392–394`.
- CSS vars `--success`, `--error`, `--warn` are already defined in `styles.css:9–11`.
- No inline validation error element exists in `index.html`.
- No pre-submit validation exists in `app.js`.

---

## Task 1: Rewrite API endpoint response schema

**Files:**
- Modify: `scripts/loan_api.py:859–908`

The existing body must be replaced entirely. The function signature (`@app.post` decorator + `def validate_source_path`) stays.

**Step 1: Replace the function body**

Replace lines 861–908 (the entire body of `validate_source_path`) with:

```python
    """Validate a proposed source_path: exists, is_dir, within SOURCE_LOANS_ROOT."""
    raw = (body.source_path or "").strip() if body.source_path is not None else ""
    if not raw:
        return {"ok": False, "source_path": raw, "resolved_path": None, "reason": "empty_source_path", "mtime_utc": None}
    try:
        p = Path(raw).resolve()
    except Exception:
        return {"ok": False, "source_path": raw, "resolved_path": None, "reason": "invalid_path", "mtime_utc": None}
    # Security: must be within SOURCE_LOANS_ROOT. Do NOT return resolved_path if outside.
    try:
        root_resolved = SOURCE_LOANS_ROOT.resolve()
        p.relative_to(root_resolved)
        within_root = True
    except (ValueError, OSError):
        within_root = False
    if not within_root:
        return {"ok": False, "source_path": raw, "resolved_path": None, "reason": "outside_source_root", "mtime_utc": None}
    resolved_path = str(p)
    if not p.exists():
        return {"ok": False, "source_path": raw, "resolved_path": resolved_path, "reason": "not_found", "mtime_utc": None}
    if not p.is_dir():
        return {"ok": False, "source_path": raw, "resolved_path": resolved_path, "reason": "not_a_directory", "mtime_utc": None}
    try:
        mtime_utc = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        mtime_utc = None
    return {"ok": True, "source_path": raw, "resolved_path": resolved_path, "reason": None, "mtime_utc": mtime_utc}
```

**Step 2: Syntax check**

```bash
python3 -m py_compile scripts/loan_api.py && echo "OK"
```
Expected: `OK`

**Step 3: Smoke-test the three acceptance cases (from the server)**

```bash
# success (use a real loan folder that exists under /mnt/source_loans)
curl -s -X POST http://localhost:8000/tenants/peak/loans/123/source_path/validate \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"source_path":"/mnt/source_loans/5-Borrowers TBD/<real-folder>"}' | python3 -m json.tool
# Expected: ok=true, mtime_utc present, resolved_path non-null

# not_found
curl -s -X POST http://localhost:8000/tenants/peak/loans/123/source_path/validate \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"source_path":"/mnt/source_loans/5-Borrowers TBD/DOES_NOT_EXIST_12345"}' | python3 -m json.tool
# Expected: ok=false, reason="not_found"

# security: outside root
curl -s -X POST http://localhost:8000/tenants/peak/loans/123/source_path/validate \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"source_path":"/etc"}' | python3 -m json.tool
# Expected: ok=false, reason="outside_source_root", resolved_path=null
```

**Step 4: Commit**

```bash
git add scripts/loan_api.py
git commit -m "fix: update validate_source_path response schema to spec (source_path/resolved_path/reason/mtime_utc)"
```

---

## Task 2: Add HTML feedback element and CSS classes

**Files:**
- Modify: `scripts/webui/index.html` (after `.main-actions` div, line ~65–68)
- Modify: `scripts/webui/styles.css` (append near end of file)

**Step 1: Add validation message element to index.html**

After the `<div class="main-actions">` closing `</div>` (the one containing "Process Loan" and "View Artifacts" buttons), add:

```html
        <p id="source-validation-msg" class="source-validation-msg" hidden></p>
```

The block should look like:
```html
        <div class="main-actions">
          <button type="button" id="process-loan-btn" class="btn-cta">Process Loan</button>
          <button type="button" id="view-artifacts-btn" class="btn-secondary">View Artifacts</button>
        </div>
        <p id="source-validation-msg" class="source-validation-msg" hidden></p>
```

**Step 2: Add CSS classes to styles.css**

Append at the end:

```css
/* ——— Source path validation feedback ——— */
.source-validation-msg { font-size: 0.8125rem; margin: 0.25rem 0 0; }
.source-validation-ok   { color: var(--success); }
.source-validation-error { color: var(--error); }
.source-validation-warn  { color: var(--warn); }
```

**Step 3: Commit**

```bash
git add scripts/webui/index.html scripts/webui/styles.css
git commit -m "feat: add source-validation-msg element and CSS classes for path validation feedback"
```

---

## Task 3: Add JS validation helper and wire into UI

**Files:**
- Modify: `scripts/webui/app.js`

### 3a — Add helper functions (insert after `setSourcePathForLoan` function, around line 264)

Add these three functions:

```javascript
  /** Show a validation message near the Process Loan button. type: "ok" | "error" | "warn" */
  function showSourceValidationMsg(msg, type) {
    var msgEl = el("source-validation-msg");
    if (!msgEl) return;
    msgEl.textContent = msg;
    msgEl.className = "source-validation-msg source-validation-" + type;
    msgEl.hidden = false;
  }

  function clearSourceValidationMsg() {
    var msgEl = el("source-validation-msg");
    if (msgEl) { msgEl.hidden = true; msgEl.textContent = ""; }
  }

  async function validateSourcePath(tenant, loanId, sourcePath) {
    var url = "/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loanId) + "/source_path/validate";
    var res = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_path: sourcePath }),
    });
    if (!res.ok) throw new Error(res.status + " " + await res.text());
    return await res.json();
  }
```

### 3b — Clear validation msg on path change (in `initSourcePath`, after `input.addEventListener("change", ...)` around line 393)

Inside the `input.addEventListener("change", ...)` handler, add `clearSourceValidationMsg();` at the top of the callback:

```javascript
    if (input) {
      input.addEventListener("change", function () {
        clearSourceValidationMsg();         // ← add this line
        const path = input.value.trim();
        ...
```

### 3c — Background-validate cached path on selectLoan (in `selectLoan`, in the `else` branch where `storedPath` is used, around lines 353–360)

After `if (display) display.textContent = storedPath || "(select a loan from source list)";`, add:

```javascript
      if (storedPath) {
        var _tenant = getTenantId();
        var _lid = loanId;
        validateSourcePath(_tenant, _lid, storedPath)
          .then(function (v) {
            if (!v.ok) showSourceValidationMsg("Cached source folder is stale. Please re-select.", "warn");
          })
          .catch(function () {}); // silent on network error
      }
```

Also add `clearSourceValidationMsg();` at the very top of `selectLoan` (before any other logic) so switching loans clears old messages:

```javascript
  async function selectLoan(loanId) {
    clearSourceValidationMsg();   // ← add
    selectedLoanId = loanId;
    ...
```

### 3d — Validate before submit in Process Loan click handler (in `initProcessLoan`, around lines 454–503)

After the empty `sourcePath` check and before the `const tenant = getTenantId();` line, add a validation block. The existing structure:

```javascript
      if (!sourcePath) {
        alert("Set the source folder (click Change…) and try again.");
        return;
      }
      const tenant = getTenantId();
      const body = { ... };
      ...
      try {
        const res = await apiFetch("/tenants/...runs/start", { ... });
```

Becomes:

```javascript
      if (!sourcePath) {
        alert("Set the source folder (click Change…) and try again.");
        return;
      }
      const tenant = getTenantId();
      // Validate source_path before submitting to prevent stale/invalid paths
      clearSourceValidationMsg();
      try {
        const validation = await validateSourcePath(tenant, selectedLoanId, sourcePath);
        if (!validation.ok) {
          showSourceValidationMsg("Source folder not found or invalid: " + (validation.reason || "unknown"), "error");
          return;
        }
        showSourceValidationMsg("Source folder OK (mtime: " + (validation.mtime_utc || "unknown") + ")", "ok");
      } catch (valErr) {
        showSourceValidationMsg("Could not validate source folder: " + (valErr.message || valErr), "error");
        return;
      }
      const body = { ... };
      ...
      try {
        const res = await apiFetch("/tenants/...runs/start", { ... });
```

**Step 2: Syntax check**

Open the UI in browser at `http://<server>:8000/ui` and open DevTools console — confirm no JS errors on load.

Or from the server:
```bash
node --check scripts/webui/app.js 2>&1 && echo "OK"
```

**Step 3: Commit**

```bash
git add scripts/webui/app.js
git commit -m "feat: validate source_path before runs/start; warn on stale cached path"
```

---

## Task 4: Final acceptance verification

**Step 1: Python syntax check**
```bash
python3 -m py_compile scripts/loan_api.py && echo "loan_api OK"
```

**Step 2: JS syntax check**
```bash
node --check scripts/webui/app.js && echo "app.js OK"
```

**Step 3: API acceptance tests (run from server or via Tailscale)**

```powershell
# From Windows PowerShell via Tailscale (replace HOST and KEY):
$h = @{ "X-API-Key" = $env:API_KEY; "Content-Type" = "application/json" }
$base = "http://10.10.10.190:8000"

# 1) Success case
$r = Invoke-RestMethod "$base/tenants/peak/loans/123/source_path/validate" -Method POST -Headers $h -Body '{"source_path":"/mnt/source_loans/5-Borrowers TBD/<real-folder>"}'
$r.ok        # must be True
$r.mtime_utc # must be non-null UTC ISO Z string

# 2) Not found
$r2 = Invoke-RestMethod "$base/tenants/peak/loans/123/source_path/validate" -Method POST -Headers $h -Body '{"source_path":"/mnt/source_loans/5-Borrowers TBD/DOES_NOT_EXIST_9999"}'
$r2.ok      # must be False
$r2.reason  # must be "not_found"

# 3) Security: outside root
$r3 = Invoke-RestMethod "$base/tenants/peak/loans/123/source_path/validate" -Method POST -Headers $h -Body '{"source_path":"/etc"}'
$r3.ok            # must be False
$r3.reason        # must be "outside_source_root"
$r3.resolved_path # must be null / $null
```

**Step 4: UI manual check**
1. Load `/ui`, select a loan with a valid source path → no warning shown.
2. Manually edit the source path input to a nonexistent path, click "Process Loan" → inline error appears, no job submitted.
3. Manually edit a loan's localStorage key to a stale path, reload, select that loan → warning "Cached source folder is stale. Please re-select." appears.
4. Valid path → "Source folder OK (mtime: ...)" shown, job submits normally.

---

## Invariants confirmed

- No existing endpoint paths changed.
- No existing response schemas changed (JobRecord untouched).
- No new external dependencies.
- `resolved_path` is `null` when path is outside `SOURCE_LOANS_ROOT`.
- localStorage keys unchanged.
- No refactoring of unrelated code.
