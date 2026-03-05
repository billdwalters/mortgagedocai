/**
 * MortgageDocAI — Client Mode v1 (Loan Processor UI)
 * Plain HTML/CSS/JS; no build. Uses existing API; X-API-Key from localStorage/settings.
 */
(function () {
  "use strict";

  // Guard: file:// is not a supported origin. Show banner and stop.
  if (window.location.protocol === "file:") {
    document.body.innerHTML =
      '<div style="font-family:sans-serif;padding:2rem;background:#1e1b4b;color:#fbbf24;' +
      'border:2px solid #fbbf24;border-radius:8px;max-width:600px;margin:4rem auto;text-align:center">' +
      '<strong style="font-size:1.2rem">&#9888; file:// is not supported</strong><br><br>' +
      'This UI must be opened from the API server:<br><br>' +
      '<code style="background:#312e81;padding:4px 10px;border-radius:4px">http://&lt;host&gt;:8000/ui</code>' +
      '</div>';
    return;
  }

  const STORAGE_API_KEY = "mortgagedocai_api_key";
  const STORAGE_TENANT = "mortgagedocai_tenant_id";
  const STORAGE_BASE_URL = "mortgagedocai_base_url";
  const SOURCE_PATH_PREFIX = "mortgagedocai_source_path_";
  const POLL_INTERVAL_MS = 2000;
  const STALL_MS = 900000;   // 15 min without job-state change → stale (CPU-only servers are slow)
  const MAX_FAILURES = 10;   // consecutive poll failures → stop

  /** Run ID heuristic: looks like timestamp (contains T, ends with Z) or matches YYYY-MM-DDTHH... */
  function isLikelyRunId(id) {
    if (!id || typeof id !== "string") return false;
    const t = id.trim();
    if (t.indexOf("T") >= 0 && t.endsWith("Z")) return true;
    return /^\d{4}-\d{2}-\d{2}T/.test(t);
  }

  /** Friendly labels for PHASE names (stepper) */
  const PHASE_LABELS = {
    INTAKE: "Intake",
    PROCESS: "Process",
    STEP13_GENERAL: "Retrieval",
    STEP12_UW_CONDITIONS: "UW Conditions",
    STEP13_INCOME: "Income Retrieval",
    STEP12_INCOME_ANALYSIS: "Income Analysis",
    STEP12_UW_DECISION: "UW Decision",
    DONE: "Done",
    FAIL: "Failed",
  };

  const STEPPER_ORDER = [
    "INTAKE", "PROCESS", "STEP13_GENERAL", "STEP12_UW_CONDITIONS",
    "STEP13_INCOME", "STEP12_INCOME_ANALYSIS", "STEP12_UW_DECISION",
    "DONE", "FAIL",
  ];

  function el(id) {
    return document.getElementById(id);
  }

  function getBaseUrl() {
    const inp = el("base-url");
    const v = (inp && inp.value) ? inp.value.trim() : (localStorage.getItem(STORAGE_BASE_URL) || "");
    return v ? v.replace(/\/$/, "") : window.location.origin;
  }

  function getApiKey() {
    const inp = el("api-key");
    return (inp && inp.value) ? String(inp.value) : (localStorage.getItem(STORAGE_API_KEY) || "");
  }

  function getTenantId() {
    const inp = el("tenant-id");
    const v = (inp && inp.value) ? inp.value.trim() : (localStorage.getItem(STORAGE_TENANT) || "peak");
    return v || "peak";
  }

  function setConnectionIndicator(status) {
    const ind = el("connection-indicator");
    if (!ind) return;
    if (status === "ok") {
      ind.textContent = "Connected";
      ind.className = "connection-indicator connection-ok";
    } else if (status === "unauthorized") {
      ind.textContent = "Unauthorized";
      ind.className = "connection-indicator connection-unauthorized";
    } else {
      ind.textContent = "Offline";
      ind.className = "connection-indicator connection-offline";
    }
  }

  function showUnauthorized(show) {
    if (show) setConnectionIndicator("unauthorized");
    else setConnectionIndicator(lastHealthStatus);
  }

  let lastHealthStatus = "pending";

  async function apiFetch(path, options) {
    const base = getBaseUrl();
    const url = (path.startsWith("/") ? base + path : base + "/" + path);
    const headers = Object.assign({}, (options && options.headers) || {});
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;
    const res = await fetch(url, Object.assign({}, options, { headers }));
    if (res.status === 401) {
      showUnauthorized(true);
      throw new Error("Unauthorized (401)");
    }
    showUnauthorized(false);
    return res;
  }

  async function apiJson(path, options) {
    const res = await apiFetch(path, options);
    const text = await res.text();
    if (!res.ok) throw new Error(res.status + " " + (text || res.statusText));
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (_) {
      throw new Error("Invalid JSON: " + text.slice(0, 200));
    }
  }

  function parsePhaseLines(stdout) {
    if (!stdout || typeof stdout !== "string") return [];
    const lines = [];
    stdout.split("\n").forEach(function (line) {
      const t = line.trim();
      if (t.indexOf("PHASE:") === 0) {
        const rest = t.slice(6).trim();
        const space = rest.indexOf(" ");
        const name = space >= 0 ? rest.slice(0, space) : rest;
        const ts = space >= 0 ? rest.slice(space + 1).trim() : "";
        lines.push({ name: name, ts: ts });
      }
    });
    return lines;
  }

  // ——— Settings ———
  (function initSettings() {
    const panel = el("settings-panel");
    const toggle = el("settings-toggle");
    if (toggle && panel) {
      toggle.addEventListener("click", function () {
        panel.hidden = !panel.hidden;
        if (!panel.hidden) {
          if (el("base-url")) el("base-url").value = localStorage.getItem(STORAGE_BASE_URL) || "";
          if (el("api-key")) el("api-key").value = localStorage.getItem(STORAGE_API_KEY) || "";
          if (el("tenant-id")) el("tenant-id").value = localStorage.getItem(STORAGE_TENANT) || "peak";
        }
      });
    }
    if (el("settings-save")) {
      el("settings-save").addEventListener("click", function () {
        try {
          const base = (el("base-url") && el("base-url").value) ? el("base-url").value.trim() : "";
          const key = (el("api-key") && el("api-key").value) ? el("api-key").value : "";
          const tenant = (el("tenant-id") && el("tenant-id").value) ? el("tenant-id").value.trim() : "peak";
          if (base) localStorage.setItem(STORAGE_BASE_URL, base);
          if (key) localStorage.setItem(STORAGE_API_KEY, key);
          if (tenant) localStorage.setItem(STORAGE_TENANT, tenant);
          setConnectionIndicator(lastHealthStatus);
          loadOllamaModels();
        } catch (_) {}
      });
    }
    if (el("settings-test")) {
      el("settings-test").addEventListener("click", async function () {
        const resultEl = el("settings-test-result");
        if (resultEl) resultEl.hidden = true;
        try {
          const data = await apiJson("/health");
          lastHealthStatus = (data && data.status === "ok") ? "ok" : "fail";
          setConnectionIndicator(lastHealthStatus);
          if (resultEl) {
            resultEl.textContent = "OK — Connected.";
            resultEl.hidden = false;
            resultEl.classList.remove("error");
          }
        } catch (e) {
          if (resultEl) {
            resultEl.textContent = "Error: " + (e.message || e);
            resultEl.hidden = false;
            resultEl.classList.add("error");
          }
        }
      });
    }
  })();

  // ——— Health (background) ———
  async function loadHealth() {
    try {
      const data = await apiJson("/health");
      lastHealthStatus = (data && data.status === "ok") ? "ok" : "fail";
      setConnectionIndicator(lastHealthStatus);
    } catch (_) {
      lastHealthStatus = "fail";
      setConnectionIndicator("fail");
    }
  }

  // ——— Last processed cache (per loan) ———
  const lastProcessedCache = {};

  async function getLastProcessedForLoan(loanId) {
    if (lastProcessedCache[loanId]) return lastProcessedCache[loanId];
    const tenant = getTenantId();
    try {
      const runsData = await apiJson("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loanId) + "/runs");
      const runIds = (runsData && Array.isArray(runsData.run_ids)) ? runsData.run_ids : [];
      const filtered = runIds.filter(isLikelyRunId);
      filtered.sort().reverse();
      for (let i = 0; i < filtered.length; i++) {
        const runId = filtered[i];
        try {
          const art = await apiJson("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loanId) + "/runs/" + encodeURIComponent(runId) + "/artifacts");
          const jm = (art && art.job_manifest) ? art.job_manifest : {};
          if (jm.exists && jm.status === "SUCCESS") {
            let generated = runId;
            try {
              const manifestRes = await apiFetch("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loanId) + "/runs/" + encodeURIComponent(runId) + "/job_manifest");
              if (manifestRes.ok) {
                const manifest = await manifestRes.json();
                if (manifest && manifest.generated_at_utc) generated = manifest.generated_at_utc;
              }
            } catch (_) {}
            const entry = { run_id: runId, generated_at_utc: generated };
            lastProcessedCache[loanId] = entry;
            return entry;
          }
        } catch (_) {}
      }
    } catch (_) {}
    return null;
  }

  /** Paths under nas_analyze or nas_chunk are output paths, not source document paths. Treat as invalid. */
  function isInvalidSourcePath(path) {
    if (!path || typeof path !== "string") return true;
    const p = path.trim();
    return p.indexOf("nas_analyze") >= 0 || p.indexOf("nas_chunk") >= 0;
  }

  function getSourcePathForLoan(loanId) {
    const tenant = getTenantId();
    const key = SOURCE_PATH_PREFIX + tenant + "_" + loanId;
    try {
      const stored = localStorage.getItem(key) || "";
      if (stored && isInvalidSourcePath(stored)) {
        localStorage.removeItem(key);
        return "";
      }
      return stored;
    } catch (_) {
      return "";
    }
  }

  function setSourcePathForLoan(loanId, path) {
    const tenant = getTenantId();
    const key = SOURCE_PATH_PREFIX + tenant + "_" + loanId;
    try {
      if (path) localStorage.setItem(key, path);
      else localStorage.removeItem(key);
    } catch (_) {}
  }

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

  // ——— Inline feedback (replaces alert() boxes) ———
  var _inlineMsgTimer = null;
  function showInlineMsg(msg, type) {
    var msgEl = el("inline-msg");
    if (!msgEl) return;
    if (_inlineMsgTimer) { clearTimeout(_inlineMsgTimer); _inlineMsgTimer = null; }
    msgEl.textContent = msg;
    msgEl.className = "source-validation-msg source-validation-" + type;
    msgEl.hidden = false;
    _inlineMsgTimer = setTimeout(function () { clearInlineMsg(); }, 6000);
  }
  function clearInlineMsg() {
    var msgEl = el("inline-msg");
    if (msgEl) { msgEl.hidden = true; msgEl.textContent = ""; }
    if (_inlineMsgTimer) { clearTimeout(_inlineMsgTimer); _inlineMsgTimer = null; }
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

  // ——— State ———
  let selectedLoanId = null;
  let selectedRunId = null;
  let currentJobId = null;
  /** After refresh: loan_id -> source_loans item (source_path, last_processed_utc, etc.) */
  let sourceLoanItemsByLoanId = {};
  let refreshLoansInFlight = false;

  // ——— Loans list (source-of-truth from GET /tenants/{tenant}/source_loans) ———
  async function refreshLoans() {
    if (refreshLoansInFlight) return;
    refreshLoansInFlight = true;
    const btn = el("refresh-loans");
    if (btn) { btn.disabled = true; btn.textContent = "Refreshing\u2026"; }
    const listEl = el("loan-list");
    if (!listEl) {
      refreshLoansInFlight = false;
      if (btn) { btn.disabled = false; btn.textContent = "Refresh Loans"; }
      return;
    }
    listEl.innerHTML = "";
    selectedLoanId = null;
    selectedRunId = null;
    sourceLoanItemsByLoanId = {};
    hideMainOverview();
    updateProcessLoanButton();
    try {
      const data = await apiJson("/tenants/" + encodeURIComponent(getTenantId()) + "/source_loans");
      const items = (data && Array.isArray(data.items)) ? data.items : [];
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        sourceLoanItemsByLoanId[it.loan_id] = it;
        const li = document.createElement("li");
        li.className = "loan-item";
        li.dataset.loanId = it.loan_id;
        const lastText = (it.last_processed_utc != null && it.last_processed_utc !== "") ? formatTimestamp(it.last_processed_utc) : "Never";
        const badge = it.needs_reprocess ? "Needs Processing" : "Up to date";
        const badgeClass = it.needs_reprocess ? "loan-badge needs-processing" : "loan-badge up-to-date";
        li.innerHTML =
          "<span class=\"loan-id\">" + escapeHtml(it.loan_id) + "</span>" +
          "<span class=\"loan-folder-name small\">" + escapeHtml(it.folder_name || "") + "</span>" +
          "<span class=\"loan-meta small\">Source: " + escapeHtml(formatTimestamp(it.source_last_modified_utc || "")) + "</span>" +
          "<span class=\"loan-meta small\">Processed: " + escapeHtml(lastText) + "</span>" +
          "<span class=\"" + badgeClass + "\">" + escapeHtml(badge) + "</span>";
        li.addEventListener("click", function () {
          selectLoan(it.loan_id);
          document.querySelectorAll(".loan-item").forEach(function (n) { n.classList.remove("selected"); });
          li.classList.add("selected");
        });
        listEl.appendChild(li);
      }
      if (items.length === 0) {
        const li = document.createElement("li");
        li.className = "loan-item muted";
        li.textContent = "No source loans found.";
        listEl.appendChild(li);
      }
    } catch (e) {
      const li = document.createElement("li");
      li.className = "loan-item error";
      li.textContent = "Error: " + (e.message || e);
      listEl.appendChild(li);
    } finally {
      refreshLoansInFlight = false;
      if (btn) { btn.disabled = false; btn.textContent = "Refresh Loans"; }
    }
  }

  var _jobActive = false;

  function updateProcessLoanButton() {
    const btn = el("process-loan-btn");
    if (btn) {
      btn.disabled = !selectedLoanId || _jobActive;
      btn.textContent = _jobActive ? "Processing…" : "Process Loan";
    }
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  function hideMainOverview() {
    const main = el("main-overview");
    if (main) main.hidden = true;
    hideSummaryDashboard();
  }

  async function selectLoan(loanId) {
    clearSourceValidationMsg();
    selectedLoanId = loanId;
    selectedRunId = null;
    const item = sourceLoanItemsByLoanId[loanId];
    const main = el("main-overview");
    if (main) main.hidden = false;
    if (el("overview-loan-id")) el("overview-loan-id").textContent = "Loan " + loanId;
    const pathInput = el("source-path-input");
    const display = el("source-display");
    if (item && item.source_path) {
      if (pathInput) pathInput.value = item.source_path;
      if (display) display.textContent = item.source_path;
      setSourcePathForLoan(loanId, item.source_path);
      if (el("overview-last-processed")) el("overview-last-processed").textContent = (item.last_processed_utc != null && item.last_processed_utc !== "") ? formatTimestamp(item.last_processed_utc) : "Never";
      if (item.last_processed_run_id) selectedRunId = item.last_processed_run_id;
    } else {
      const storedPath = getSourcePathForLoan(loanId);
      if (pathInput) pathInput.value = storedPath || "";
      if (display) display.textContent = storedPath || "(select a loan from source list)";
      if (storedPath) {
        var _tenant = getTenantId();
        var _lid = loanId;
        validateSourcePath(_tenant, _lid, storedPath)
          .then(function (v) {
            if (!v.ok) showSourceValidationMsg("Cached source folder is stale. Please re-select.", "warn");
          })
          .catch(function () {});
      }
      if (el("overview-last-processed")) el("overview-last-processed").textContent = "Unknown";
      const last = await getLastProcessedForLoan(loanId);
      if (last) selectedRunId = last.run_id;
    }
    if (pathInput) pathInput.placeholder = "/mnt/source_loans/.../Folder [Loan 123]";
    const detailsRun = el("details-run-id");
    if (detailsRun) detailsRun.textContent = selectedRunId ? "run_id: " + selectedRunId + " (" + formatTimestamp(selectedRunId) + ")" : "";
    updateProcessLoanButton();
    loadSummaryDashboard(loanId, selectedRunId);
  }

  // ——— Source path edit and browse ———
  (function initSourcePath() {
    const editWrap = el("source-edit-wrap");
    const changeBtn = el("source-change-btn");
    const showPathBtn = el("source-show-path-btn");
    const input = el("source-path-input");
    const display = el("source-display");
    const browseBtn = el("source-browse-btn");
    const browsePanel = el("source-browse-panel");
    const browseBaseEl = el("source-browse-base");
    const folderListEl = el("source-folder-list");
    const browseCloseBtn = el("source-browse-close");

    if (changeBtn && editWrap && input) {
      changeBtn.addEventListener("click", function () {
        editWrap.hidden = !editWrap.hidden;
        if (browsePanel) browsePanel.hidden = true;
        if (!editWrap.hidden) input.focus();
      });
    }
    if (showPathBtn && display && input) {
      showPathBtn.addEventListener("click", function () {
        display.textContent = input.value || "(not set)";
      });
    }
    if (input) {
      input.addEventListener("change", function () {
        clearSourceValidationMsg();
        const path = input.value.trim();
        if (selectedLoanId) setSourcePathForLoan(selectedLoanId, path);
        if (display) display.textContent = path || "(not set — click Browse…)";
      });
    }

    if (browseBtn && browsePanel && folderListEl) {
      browseBtn.addEventListener("click", async function () {
        const base = (input && input.value) ? input.value.trim() : "/mnt/source_loans";
        browsePanel.hidden = false;
        if (browseBaseEl) browseBaseEl.textContent = "Base: " + base;
        folderListEl.innerHTML = "";
        try {
          const data = await apiJson("/browse/source?base=" + encodeURIComponent(base));
          const basePath = (data.base || base).replace(/\/$/, "");
          (data.folders || []).forEach(function (folder) {
            const li = document.createElement("li");
            li.textContent = folder;
            li.dataset.folder = folder;
            li.dataset.base = basePath;
            li.addEventListener("click", function () {
              const fullPath = basePath + "/" + folder;
              if (input) input.value = fullPath;
              if (selectedLoanId) setSourcePathForLoan(selectedLoanId, fullPath);
              if (display) display.textContent = fullPath;
              browsePanel.hidden = true;
            });
            folderListEl.appendChild(li);
          });
          if (folderListEl.children.length === 0) {
            const li = document.createElement("li");
            li.className = "muted";
            li.textContent = "No subfolders found.";
            folderListEl.appendChild(li);
          }
        } catch (e) {
          const li = document.createElement("li");
          li.className = "error";
          li.textContent = "Error: " + (e.message || e);
          folderListEl.appendChild(li);
        }
      });
    }
    if (browseCloseBtn && browsePanel) {
      browseCloseBtn.addEventListener("click", function () {
        browsePanel.hidden = true;
      });
    }
  })();

  // ——— Process Loan ———
  (function initProcessLoan() {
    const btn = el("process-loan-btn");
    if (!btn) return;
    btn.addEventListener("click", async function () {
      clearInlineMsg();
      if (!selectedLoanId) {
        showInlineMsg("Select a loan first.", "error");
        return;
      }
      const pathInput = el("source-path-input");
      const sourcePath = (pathInput && pathInput.value) ? pathInput.value.trim() : "";
      if (!sourcePath) {
        showInlineMsg("Set the source folder (click Change\u2026) and try again.", "error");
        return;
      }
      const tenant = getTenantId();
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
      const body = {
        source_path: sourcePath,
        run_id: null,
        run_llm: false,
        offline_embeddings: true,
        top_k: 80,
        max_per_file: 12,
        max_dropped_chunks: 5,
        expect_rp_hash_stable: true,
        smoke_debug: false,
      };
      const adv = el("adv-offline-embeddings");
      if (adv && adv.checked !== undefined) body.offline_embeddings = adv.checked;
      const topK = el("adv-top-k");
      if (topK && topK.value) body.top_k = parseInt(topK.value, 10) || 80;
      const maxPf = el("adv-max-per-file");
      if (maxPf && maxPf.value) body.max_per_file = parseInt(maxPf.value, 10) || 12;
      const maxDrop = el("adv-max-dropped-chunks");
      if (maxDrop && maxDrop.value) body.max_dropped_chunks = parseInt(maxDrop.value, 10) || 5;
      const rpStable = el("adv-expect-rp-hash-stable");
      if (rpStable) body.expect_rp_hash_stable = rpStable.checked;
      const smoke = el("adv-smoke-debug");
      if (smoke) body.smoke_debug = smoke.checked;
      try {
        const res = await apiFetch("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const text = await res.text();
          throw new Error(res.status + " " + (text || res.statusText));
        }
        const data = await res.json();
        currentJobId = data.job_id || null;
        selectedRunId = data.run_id || selectedRunId;
        if (selectedRunId && selectedLoanId) lastProcessedCache[selectedLoanId] = { run_id: selectedRunId, generated_at_utc: selectedRunId };
        if (el("details-run-id")) el("details-run-id").textContent = data.run_id ? "run_id: " + data.run_id + " (" + formatTimestamp(data.run_id) + ")" : "";
        _jobActive = true;
        updateProcessLoanButton();
        hideSummaryDashboard();
        showProgressPanel();
        startJobPolling(data.job_id);
      } catch (err) {
        showInlineMsg("Error: " + (err.message || err), "error");
      }
    });
  })();

  function showProgressPanel() {
    const panel = el("progress-panel");
    if (panel) panel.hidden = false;
  }

  function renderStepper(phases) {
    const container = el("stepper");
    if (!container) return;
    const seen = {};
    phases.forEach(function (p) { seen[p.name] = true; });
    const lastPhase = phases.length ? phases[phases.length - 1].name : null;
    let html = "";
    STEPPER_ORDER.forEach(function (name) {
      const label = PHASE_LABELS[name] || name;
      const done = name === "FAIL" ? lastPhase === "FAIL" : seen[name];
      const current = lastPhase === name;
      let cls = current ? "stepper-step current" : (done ? "stepper-step done" : "stepper-step");
      if (current && name === "FAIL") cls += " stepper-fail";
      html += "<span class=\"" + cls + "\" title=\"" + escapeHtml(name) + "\">" + escapeHtml(label) + "</span>";
    });
    container.innerHTML = html;
  }

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
      if (el("progress-started")) el("progress-started").textContent = formatTimestamp(job.started_at_utc || "—");
      if (el("progress-finished")) el("progress-finished").textContent = formatTimestamp(job.finished_at_utc || "—");
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

          var status = (job && job.status) ? job.status : "";

          if (status === "PENDING" || status === "RUNNING" || status === "QUEUED") {
            var stalledMs = Date.now() - lastChangeAtMs;
            if (stalledMs > STALL_MS) {
              var mins = Math.round(stalledMs / 60000);
              showPollWarning("Job has not changed in " + mins + "m \u2014 still polling. Step11 (embed) can take 15\u201330 min on CPU-only servers.");
              // Keep polling — don't stop. The job may still be running a long step.
            }
          }

          setJobFields(job);
          if (!stopped && (Date.now() - lastChangeAtMs <= STALL_MS)) { hidePollWarning(); }

          if (status === "SUCCESS" || status === "FAIL") {
            stopPolling();
            _jobActive = false;
            updateProcessLoanButton();
            if (status === "SUCCESS" && job.run_id && selectedLoanId) {
              selectedRunId = job.run_id;
              lastProcessedCache[selectedLoanId] = { run_id: job.run_id, generated_at_utc: job.run_id };
              if (el("overview-last-processed")) el("overview-last-processed").textContent = formatTimestamp(job.run_id);
              if (el("details-run-id")) el("details-run-id").textContent = "run_id: " + job.run_id + " (" + formatTimestamp(job.run_id) + ")";
              loadSummaryDashboard(selectedLoanId, job.run_id);
            }
          }
        })
        .catch(function () {
          consecutiveFailures++;
          var sinceLastOk = Date.now() - lastOkAtMs;
          if (consecutiveFailures >= MAX_FAILURES) {
            showPollWarning(
              "Connection lost to server \u2014 polling stopped. Check VPN/Tailscale or API service."
            );
            stopPolling();
            _jobActive = false;
            updateProcessLoanButton();
          } else if (consecutiveFailures >= 3 || sinceLastOk > 15000) {
            showPollWarning(
              "Connection issues detected \u2014 retrying in background. Check VPN/Tailscale or API service."
            );
          }
        });
    }

    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    poll();
  }

  // ——— Markdown rendering helper ———
  function renderMarkdownSafe(md) {
    if (typeof marked === "undefined" || !marked.parse) return null;
    var html = marked.parse(md, { gfm: true, breaks: true });
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    tmp.querySelectorAll("script, iframe").forEach(function (el) { el.remove(); });
    tmp.querySelectorAll("*").forEach(function (node) {
      Array.from(node.attributes).forEach(function (attr) {
        if (attr.name.toLowerCase().startsWith("on")) node.removeAttribute(attr.name);
      });
    });
    return tmp.innerHTML;
  }

  // ——— View Artifacts ———
  (function initViewArtifacts() {
    const btn = el("view-artifacts-btn");
    const panel = el("artifacts-panel");
    const indexEl = el("artifacts-index");
    const previewWrap = el("artifact-preview-wrap");
    const previewContent = el("artifact-preview-content");
    const previewMarkdown = el("artifact-preview-markdown");
    if (!btn || !panel || !indexEl) return;
    btn.addEventListener("click", async function () {
      clearInlineMsg();
      const runId = selectedRunId || (selectedLoanId && lastProcessedCache[selectedLoanId] && lastProcessedCache[selectedLoanId].run_id);
      if (!selectedLoanId || !runId) {
        showInlineMsg("No run selected. Process a loan first or select a loan that has been processed.", "error");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Loading\u2026";
      panel.hidden = false;
      if (previewWrap) previewWrap.hidden = true;
      indexEl.innerHTML = "";
      try {
        const data = await apiJson("/tenants/" + encodeURIComponent(getTenantId()) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/artifacts");
        const base = getBaseUrl();
        const rp = data.retrieval_pack || {};
        const jm = data.job_manifest || {};
        let html = "";
        if (rp.exists) {
          html += "<p><a class=\"file-link\" href=\"" + base + "/tenants/" + encodeURIComponent(getTenantId()) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/retrieval_pack\" target=\"_blank\" rel=\"noopener\">Retrieval pack</a></p>";
        }
        if (jm.exists) {
          html += "<p><a class=\"file-link\" href=\"" + base + "/tenants/" + encodeURIComponent(getTenantId()) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/job_manifest\" target=\"_blank\" rel=\"noopener\">Job manifest</a></p>";
        }
        const profiles = (data.profiles || []).slice();
        profiles.sort(function (a, b) { return (a.name || "").localeCompare(b.name || ""); });
        profiles.forEach(function (prof) {
          html += "<div class=\"profile-block\"><strong>" + escapeHtml(prof.name || "") + "</strong> ";
          const files = (prof.files || []).slice();
          files.sort(function (a, b) { return (a.name || "").localeCompare(b.name || ""); });
          files.forEach(function (f) {
            if (!f.exists) return;
            const url = base + "/tenants/" + encodeURIComponent(getTenantId()) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/artifacts/" + encodeURIComponent(prof.name) + "/" + encodeURIComponent(f.name);
            const label = f.name === "answer.md" ? "Answer" : f.name === "answer.json" ? "Answer (JSON)" : f.name === "citations.jsonl" ? "Citations" : f.name;
            html += "<a class=\"file-link\" data-url=\"" + url.replace(/"/g, "&quot;") + "\" data-filename=\"" + (f.name || "").replace(/"/g, "&quot;") + "\" href=\"#\">" + escapeHtml(label) + "</a> ";
          });
          html += "</div>";
        });
        indexEl.innerHTML = html;
        indexEl.querySelectorAll(".file-link[data-url]").forEach(function (a) {
          a.addEventListener("click", function (e) {
            e.preventDefault();
            const url = a.getAttribute("data-url");
            const filename = a.getAttribute("data-filename") || "";
            if (!url || !previewContent || !previewWrap) return;
            // Reset both containers
            previewContent.textContent = "";
            previewContent.hidden = false;
            if (previewMarkdown) { previewMarkdown.innerHTML = ""; previewMarkdown.hidden = true; }
            apiFetch(url).then(function (r) { return r.text(); }).then(function (text) {
              if (filename.endsWith(".md")) {
                var rendered = renderMarkdownSafe(text);
                if (rendered !== null && previewMarkdown) {
                  previewContent.hidden = true;
                  previewMarkdown.innerHTML = rendered;
                  previewMarkdown.hidden = false;
                } else {
                  previewContent.textContent = text;
                }
              } else if (filename.endsWith(".json")) {
                try {
                  previewContent.textContent = JSON.stringify(JSON.parse(text), null, 2);
                } catch (_) {
                  previewContent.textContent = text;
                }
              } else {
                previewContent.textContent = text;
              }
              previewWrap.hidden = false;
            }).catch(function (err) {
              previewContent.textContent = "Error: " + (err.message || err);
              previewContent.hidden = false;
              if (previewMarkdown) previewMarkdown.hidden = true;
              previewWrap.hidden = false;
            });
          });
        });
      } catch (e) {
        indexEl.innerHTML = "<p class=\"muted\">Error: " + escapeHtml(e.message || e) + "</p>";
      } finally {
        btn.disabled = false;
        btn.textContent = "View Artifacts";
      }
    });
  })();

  // ——— Summary Dashboard ———
  var _dashboardRenderSeq = 0;

  function formatUSD(v) {
    if (v == null || isNaN(v)) return "—";
    return "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function formatPct(v) {
    if (v == null || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(1) + "%";
  }
  function formatDuration(sec) {
    if (sec == null || isNaN(sec)) return "—";
    var s = Number(sec);
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60);
    var rem = s - m * 60;
    return m + "m " + rem.toFixed(0) + "s";
  }

  /** Parse run_id-style (2026-02-26T060725Z) or ISO timestamps and return locale string. */
  function formatTimestamp(raw) {
    if (raw == null || raw === "" || raw === "—" || raw === "Never" || raw === "Unknown") return raw || "—";
    var s = String(raw).trim();
    // run_id format: 2026-02-26T060725Z → 2026-02-26T06:07:25Z
    var m = s.match(/^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
    if (m) s = m[1] + "T" + m[2] + ":" + m[3] + ":" + m[4] + "Z";
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(raw);
    return d.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function fetchArtifactJson(tenant, loan, run, profile, filename) {
    var path = "/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loan) +
      "/runs/" + encodeURIComponent(run) + "/artifacts/" + encodeURIComponent(profile) + "/" + encodeURIComponent(filename);
    return apiFetch(path).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).catch(function () { return null; });
  }

  function fetchRunManifest(tenant, loan, run) {
    var path = "/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(loan) +
      "/runs/" + encodeURIComponent(run);
    return apiJson(path).catch(function () { return null; });
  }

  function renderDecisionCard(d, conditionsData) {
    if (!d) return "<p class=\"muted\">Not available</p>";
    var dp = d.decision_primary || {};
    var status = (dp.status || "UNKNOWN").toUpperCase();
    var cls = status === "PASS" ? "pass" : status === "FAIL" ? "fail" : "unknown";
    var conf = d.confidence != null ? " (" + formatPct(d.confidence) + " conf)" : "";
    var program = (d.ruleset && d.ruleset.program) ? d.ruleset.program.replace(/_/g, " ") : "";
    var html = "<div class=\"summary-decision-row\">" +
      "<span class=\"summary-decision-badge " + cls + "\">" + escapeHtml(status) + "</span>" +
      "<span class=\"summary-decision-meta\">" + escapeHtml(program) + conf + "</span>" +
      "</div>";

    // DTI summary from inputs
    var inp = d.inputs || {};
    var dtiP = inp.dti_primary || {};
    if (dtiP.back_end != null) {
      var thresh = (d.ruleset && d.ruleset.thresholds && d.ruleset.thresholds.max_back_end_dti) || null;
      var dtiStr = "Back-end DTI: " + formatPct(dtiP.back_end);
      if (thresh != null) dtiStr += " (max " + formatPct(thresh) + ")";
      html += "<p style=\"margin:0.35rem 0 0;font-size:0.8125rem;color:var(--muted)\">" + escapeHtml(dtiStr) + "</p>";
    }

    // Condition count
    var conds = (conditionsData && Array.isArray(conditionsData.conditions)) ? conditionsData.conditions : [];
    if (conds.length > 0) {
      var catCounts = {};
      conds.forEach(function (c) {
        var cat = c.category || "Other";
        catCounts[cat] = (catCounts[cat] || 0) + 1;
      });
      var breakdownParts = [];
      Object.keys(catCounts).forEach(function (cat) {
        breakdownParts.push(catCounts[cat] + " " + cat);
      });
      html += "<div class=\"summary-condition-count\">" +
        "<span class=\"count-badge\">" + conds.length + " condition" + (conds.length !== 1 ? "s" : "") + "</span>" +
        "<span class=\"count-breakdown\">" + escapeHtml(breakdownParts.join(", ")) + "</span>" +
        "</div>";
      html += "<button type=\"button\" class=\"summary-conditions-link\" onclick=\"document.getElementById('conditions-panel').scrollIntoView({behavior:'smooth'})\">" +
        "View Conditions (" + conds.length + ")</button>";
    }

    // Rules table
    var reasons = dp.reasons || [];
    if (reasons.length > 0) {
      html += "<table class=\"summary-rules-table\"><thead><tr><th>Rule</th><th>Value</th><th>Threshold</th><th>Result</th></tr></thead><tbody>";
      reasons.forEach(function (r) {
        var rCls = (r.status || "").toUpperCase() === "PASS" ? "rule-pass" : "rule-fail";
        html += "<tr><td>" + escapeHtml(r.rule || "") + "</td>" +
          "<td>" + formatPct(r.value) + "</td>" +
          "<td>" + formatPct(r.threshold) + "</td>" +
          "<td class=\"" + rCls + "\">" + escapeHtml((r.status || "").toUpperCase()) + "</td></tr>";
      });
      html += "</tbody></table>";
    }

    // Missing inputs
    var missing = dp.missing_inputs || [];
    if (missing.length > 0) {
      html += "<p style=\"margin:0.5rem 0 0;font-size:0.8125rem;color:var(--warn)\">Missing: " + escapeHtml(missing.join(", ")) + "</p>";
    }
    return html;
  }

  function _dtiBar(label, value, maxThreshold) {
    if (value == null) return "";
    var pct = Math.min(Number(value) * 100, 100);
    var max = maxThreshold ? Number(maxThreshold) * 100 : 50;
    var widthPct = Math.min((pct / max) * 100, 100);
    var fillCls = (maxThreshold && value > maxThreshold) ? "over" : "ok";
    return "<div class=\"summary-dti-row\">" +
      "<span class=\"summary-dti-label\">" + escapeHtml(label) + "</span>" +
      "<div class=\"summary-dti-bar-track\">" +
        "<div class=\"summary-dti-bar-fill " + fillCls + "\" style=\"width:" + widthPct.toFixed(1) + "%\"></div>" +
      "</div>" +
      "<span class=\"summary-dti-value\">" + formatPct(value) + "</span>" +
      "</div>";
  }

  function renderDtiCard(dti) {
    if (!dti) return "<p class=\"muted\">Not available</p>";
    var html = "";
    html += _dtiBar("Front-end", dti.front_end_dti, 0.28);
    html += _dtiBar("Back-end", dti.back_end_dti, 0.45);
    if (dti.front_end_dti_combined != null) {
      html += _dtiBar("Front (comb)", dti.front_end_dti_combined, 0.28);
    }
    if (dti.back_end_dti_combined != null) {
      html += _dtiBar("Back (comb)", dti.back_end_dti_combined, 0.45);
    }
    html += "<div style=\"margin-top:0.5rem\">";
    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">PITIA</span><span class=\"summary-financial-value\">" + formatUSD(dti.housing_payment_used) + "</span></div>";
    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Debt</span><span class=\"summary-financial-value\">" + formatUSD(dti.monthly_debt_total) + "</span></div>";
    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Income</span><span class=\"summary-financial-value\">" + formatUSD(dti.monthly_income_total) + "</span></div>";
    if (dti.monthly_income_combined != null) {
      html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Income (comb)</span><span class=\"summary-financial-value\">" + formatUSD(dti.monthly_income_combined) + "</span></div>";
    }
    html += "</div>";

    // Missing inputs
    var missing = dti.missing_inputs || [];
    if (missing.length > 0) {
      html += "<p style=\"margin:0.5rem 0 0;font-size:0.8125rem;color:var(--warn)\">Missing: " + escapeHtml(missing.join(", ")) + "</p>";
    }
    return html;
  }

  function renderIncomeCard(inc) {
    if (!inc) return "<p class=\"muted\">Not available</p>";
    var html = "";

    // Totals
    var incTotal = (inc.monthly_income_total && inc.monthly_income_total.value != null) ? inc.monthly_income_total.value : null;
    var incCombined = (inc.monthly_income_total_combined && inc.monthly_income_total_combined.value != null) ? inc.monthly_income_total_combined.value : null;
    var liabTotal = (inc.monthly_liabilities_total && inc.monthly_liabilities_total.value != null) ? inc.monthly_liabilities_total.value : null;
    var pitia = (inc.proposed_pitia && inc.proposed_pitia.value != null) ? inc.proposed_pitia.value : null;

    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Income</span><span class=\"summary-financial-value\">" + formatUSD(incTotal) + "/mo</span></div>";
    if (incCombined != null) {
      html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Combined</span><span class=\"summary-financial-value\">" + formatUSD(incCombined) + "/mo</span></div>";
    }
    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">Liabilities</span><span class=\"summary-financial-value\">" + formatUSD(liabTotal) + "/mo</span></div>";
    html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">PITIA</span><span class=\"summary-financial-value\">" + formatUSD(pitia) + "</span></div>";

    // Income items
    var items = inc.income_items || [];
    if (items.length > 0) {
      html += "<h4 style=\"margin:0.5rem 0 0.25rem;font-size:0.75rem;color:var(--muted);text-transform:uppercase\">Sources</h4>";
      items.forEach(function (it) {
        var amt = it.amount != null ? formatUSD(it.amount) : "—";
        var freq = it.frequency ? " /" + it.frequency.replace("monthly", "mo").replace("annual", "yr") : "";
        html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">" + escapeHtml(it.description || "Unknown") + "</span><span class=\"summary-financial-value\">" + amt + freq + "</span></div>";
      });
    }

    // Liability items
    var liabs = inc.liability_items || [];
    if (liabs.length > 0) {
      html += "<h4 style=\"margin:0.5rem 0 0.25rem;font-size:0.75rem;color:var(--muted);text-transform:uppercase\">Liabilities</h4>";
      liabs.forEach(function (it) {
        html += "<div class=\"summary-financial-row\"><span class=\"summary-financial-label\">" + escapeHtml(it.description || "Unknown") + "</span><span class=\"summary-financial-value\">" + formatUSD(it.payment_monthly) + "/mo</span></div>";
      });
    }
    return html;
  }

  function renderRunCard(manifest) {
    if (!manifest) return "<p class=\"muted\">Not available</p>";
    var status = (manifest.status || "UNKNOWN").toUpperCase();
    var sCls = status === "SUCCESS" ? "success" : "fail";
    var html = "<div style=\"display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap;margin-bottom:0.5rem\">" +
      "<span class=\"summary-status-badge " + sCls + "\">" + escapeHtml(status) + "</span>" +
      "<span class=\"muted\" style=\"font-size:0.8125rem\">" + escapeHtml(formatTimestamp(manifest.generated_at_utc || "")) + "</span>" +
      "</div>";

    // Output files available
    var outputs = manifest.outputs || {};
    var outLines = [];
    Object.keys(outputs).forEach(function (k) {
      if (outputs[k]) {
        var label = k.replace(/_json$/, "").replace(/_/g, " ");
        outLines.push(label);
      }
    });
    if (outLines.length > 0) {
      html += "<p style=\"margin:0;font-size:0.8125rem\"><strong>Outputs:</strong> " + escapeHtml(outLines.join(", ")) + "</p>";
    }

    // Options summary
    var opts = manifest.options || {};
    var optParts = [];
    if (opts.run_llm) optParts.push("LLM");
    if (opts.offline_embeddings) optParts.push("Offline Embed");
    if (opts.top_k) optParts.push("top_k=" + opts.top_k);
    if (opts.max_per_file) optParts.push("max_per_file=" + opts.max_per_file);
    if (optParts.length > 0) {
      html += "<p style=\"margin:0.25rem 0 0;font-size:0.8125rem;color:var(--muted)\">" + escapeHtml(optParts.join(" · ")) + "</p>";
    }

    // Error info
    if (manifest.error) {
      html += "<p style=\"margin:0.5rem 0 0;font-size:0.8125rem;color:var(--error)\">" + escapeHtml(manifest.error) + "</p>";
    }
    if (manifest.failed_step) {
      html += "<p style=\"margin:0.25rem 0 0;font-size:0.8125rem;color:var(--error)\">Failed at: " + escapeHtml(manifest.failed_step) + "</p>";
    }
    return html;
  }

  function loadSummaryDashboard(loanId, runId) {
    var dashEl = el("summary-dashboard");
    if (!dashEl) return;
    if (!loanId || !runId) {
      dashEl.hidden = true;
      hideConditionsPanel();
      return;
    }
    var seq = ++_dashboardRenderSeq;
    var tenant = getTenantId();

    // Fire all 5 fetches in parallel
    Promise.allSettled([
      fetchArtifactJson(tenant, loanId, runId, "uw_decision", "decision.json"),
      fetchArtifactJson(tenant, loanId, runId, "income_analysis", "dti.json"),
      fetchArtifactJson(tenant, loanId, runId, "income_analysis", "income_analysis.json"),
      fetchRunManifest(tenant, loanId, runId),
      fetchArtifactJson(tenant, loanId, runId, "uw_conditions", "conditions.json"),
    ]).then(function (results) {
      // Guard: user may have switched loans
      if (seq !== _dashboardRenderSeq) return;
      if (selectedLoanId !== loanId) return;

      var decision   = results[0].status === "fulfilled" ? results[0].value : null;
      var dti        = results[1].status === "fulfilled" ? results[1].value : null;
      var income     = results[2].status === "fulfilled" ? results[2].value : null;
      var manifest   = results[3].status === "fulfilled" ? results[3].value : null;
      var conditions = results[4].status === "fulfilled" ? results[4].value : null;

      // If every fetch returned null, hide entire dashboard
      if (!decision && !dti && !income && !manifest) {
        dashEl.hidden = true;
        hideConditionsPanel();
        return;
      }

      var decBody = el("summary-decision-body");
      var dtiBody = el("summary-dti-body");
      var incBody = el("summary-income-body");
      var runBody = el("summary-run-body");

      if (decBody) decBody.innerHTML = renderDecisionCard(decision, conditions);
      if (dtiBody) dtiBody.innerHTML = renderDtiCard(dti);
      if (incBody) incBody.innerHTML = renderIncomeCard(income);
      if (runBody) runBody.innerHTML = renderRunCard(manifest);

      dashEl.hidden = false;

      // Populate conditions panel
      renderConditionsPanel(conditions);
    });
  }

  function hideSummaryDashboard() {
    var dashEl = el("summary-dashboard");
    if (dashEl) dashEl.hidden = true;
    hideConditionsPanel();
  }

  // ——— Conditions Panel ———
  var _TIMING_BADGE_CLASS = {
    "Prior to Docs": "prior-docs",
    "Prior to Closing": "prior-closing",
    "Post Closing": "post-closing",
  };

  function hideConditionsPanel() {
    var panel = el("conditions-panel");
    if (panel) panel.hidden = true;
  }

  function renderConditionsPanel(conditionsData) {
    var panel = el("conditions-panel");
    var summaryEl = el("conditions-summary");
    var listEl = el("conditions-list");
    if (!panel || !summaryEl || !listEl) return;

    var conds = (conditionsData && Array.isArray(conditionsData.conditions)) ? conditionsData.conditions : [];
    if (conds.length === 0) {
      panel.hidden = true;
      return;
    }

    // Summary row: total + timing breakdown
    var timingCounts = {};
    conds.forEach(function (c) {
      var t = c.timing || "Unknown";
      timingCounts[t] = (timingCounts[t] || 0) + 1;
    });
    var summaryHtml = "<span class=\"conditions-total-badge\">" + conds.length + " Condition" + (conds.length !== 1 ? "s" : "") + "</span>";
    var timingOrder = ["Prior to Docs", "Prior to Closing", "Post Closing", "Unknown"];
    timingOrder.forEach(function (t) {
      if (timingCounts[t]) {
        summaryHtml += "<span class=\"conditions-timing-summary\">" + escapeHtml(t) + ": " + timingCounts[t] + "</span>";
      }
    });
    summaryEl.innerHTML = summaryHtml;

    // Group by category (preserve pipeline sort order)
    var categoryOrder = ["Verification", "Assets", "Income", "Credit", "Property", "Title", "Insurance", "Compliance", "Other"];
    var groups = {};
    conds.forEach(function (c) {
      var cat = c.category || "Other";
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(c);
    });

    var listHtml = "";
    categoryOrder.forEach(function (cat) {
      var items = groups[cat];
      if (!items || items.length === 0) return;

      listHtml += "<div class=\"conditions-category-group\">";
      listHtml += "<h4 class=\"conditions-category-heading\">" + escapeHtml(cat) + " (" + items.length + ")</h4>";

      items.forEach(function (c) {
        var timing = c.timing || "Unknown";
        var badgeCls = _TIMING_BADGE_CLASS[timing] || "unknown-timing";

        // Extract filenames from source documents
        var sourceNames = [];
        if (c.source && Array.isArray(c.source.documents)) {
          c.source.documents.forEach(function (doc) {
            var fp = doc.file_relpath || "";
            var name = fp.split("/").pop() || fp;
            if (name && sourceNames.indexOf(name) === -1) sourceNames.push(name);
          });
        }

        listHtml += "<div class=\"conditions-item\">";
        listHtml += "<div class=\"conditions-item-desc\">" + escapeHtml(c.description || "") +
          (sourceNames.length > 0 ? "<div class=\"conditions-source\">" + escapeHtml(sourceNames.join(", ")) + "</div>" : "") +
          "</div>";
        listHtml += "<span class=\"conditions-timing-badge " + badgeCls + "\">" + escapeHtml(timing) + "</span>";
        listHtml += "</div>";
      });

      listHtml += "</div>";
    });

    listEl.innerHTML = listHtml;
    panel.hidden = false;
  }

  // ——— Ollama models dropdown (populated from server) ———
  async function loadOllamaModels() {
    const selectEl = el("chat-llm-model");
    if (!selectEl) return;
    try {
      const data = await apiJson("/ollama/models");
      const models = (data && Array.isArray(data.models)) ? data.models : [];
      selectEl.innerHTML = "";
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "(use server default)";
      selectEl.appendChild(empty);
      models.forEach(function (name) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        selectEl.appendChild(opt);
      });
      if (models.length > 0 && !selectEl.value) {
        selectEl.selectedIndex = 1;
      }
    } catch (e) {
      selectEl.innerHTML = "<option value=\"\">Ollama unavailable</option>";
    }
  }

  // ——— Chat ———
  (function initChat() {
    const messagesEl = el("chat-messages");
    const inputEl = el("chat-input");
    const sendBtn = el("chat-send");
    const profileSelect = el("chat-profile");
    const llmInput = el("chat-llm-model");

    function appendMessage(role, text) {
      if (!messagesEl) return;
      const div = document.createElement("div");
      div.className = "chat-msg chat-msg-" + role;
      const inner = document.createElement("div");
      inner.className = "chat-msg-content";
      inner.textContent = text;
      div.appendChild(inner);
      messagesEl.appendChild(div);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

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

    async function getLatestSuccessRunId() {
      if (!selectedLoanId) return null;
      const cached = lastProcessedCache[selectedLoanId];
      if (cached && cached.run_id) return cached.run_id;
      await getLastProcessedForLoan(selectedLoanId);
      const c = lastProcessedCache[selectedLoanId];
      return c ? c.run_id : null;
    }

    async function sendQuestion() {
      const question = (inputEl && inputEl.value) ? inputEl.value.trim() : "";
      if (!question) return;
      if (sendBtn) sendBtn.disabled = true;
      if (!selectedLoanId) {
        appendMessage("assistant", "Select a loan first.");
        if (sendBtn) sendBtn.disabled = false;
        return;
      }
      // Use the run shown in the UI (from loan list or last job) so we don't hit a stale cached run_id
      let runId = selectedRunId;
      if (!runId) runId = await getLatestSuccessRunId();
      if (!runId) {
        appendMessage("assistant", "No successful run for this loan. Process the loan first.");
        if (sendBtn) sendBtn.disabled = false;
        return;
      }
      appendMessage("user", question);
      if (inputEl) inputEl.value = "";
      const processingEl = el("chat-processing");
      if (processingEl) processingEl.hidden = false;
      const profile = (profileSelect && profileSelect.value) ? profileSelect.value : "default";
      const llmModel = (llmInput && llmInput.value) ? llmInput.value.trim() : null;
      const tenant = getTenantId();
      const body = {
        question: question,
        profile: profile,
        llm_model: llmModel || undefined,
        offline_embeddings: true,
        top_k: 80,
        max_per_file: 12,
      };
      try {
        let res = await apiFetch("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/query_jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.status === 404) {
          const syncRes = await apiFetch("/tenants/" + encodeURIComponent(tenant) + "/loans/" + encodeURIComponent(selectedLoanId) + "/runs/" + encodeURIComponent(runId) + "/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (!syncRes.ok) {
            const t = await syncRes.text();
            throw new Error(syncRes.status + " " + t);
          }
          const syncData = await syncRes.json();
          const answerText = (syncData && syncData.answer) ? syncData.answer : (syncData && syncData.response) ? syncData.response : JSON.stringify(syncData);
          appendMessage("assistant", answerText);
          return;
        }
        if (!res.ok) {
          const t = await res.text();
          throw new Error(res.status + " " + t);
        }
        const data = await res.json();
        const jobId = data.job_id;
        if (!jobId) {
          appendMessage("assistant", "No job_id returned.");
          return;
        }
        const result = await pollForQueryJobAnswer(jobId, profile, runId);
        if (result.ok) {
          appendMessage("assistant", result.answer || "(No answer returned)");
        } else {
          appendErrorPanel(result.job);
        }
      } catch (e) {
        appendMessage("assistant", "Error: " + (e.message || e));
      } finally {
        if (processingEl) processingEl.hidden = true;
        if (sendBtn) sendBtn.disabled = false;
      }
    }

    function pollForQueryJobAnswer(jobId, profile, runId) {
      return new Promise(function (resolve) {
        var attempts = 0;
        var maxAttempts = 120;
        function poll() {
          attempts++;
          apiJson("/jobs/" + encodeURIComponent(jobId)).then(function (job) {
            var status = (job && job.status) ? job.status : "";
            if (status === "SUCCESS") {
              var tenant = getTenantId();
              var artifactsPath = "/tenants/" + encodeURIComponent(tenant) +
                "/loans/" + encodeURIComponent(selectedLoanId) +
                "/runs/" + encodeURIComponent(runId) + "/artifacts";
              function fetchAnswer() {
                var jsonPath = artifactsPath + "/" +
                  encodeURIComponent(profile) + "/answer.json";
                apiFetch(jsonPath).then(function (r) {
                  if (!r.ok) { throw new Error(r.status); }
                  return r.json();
                }).then(function (obj) {
                  var text = (obj && obj.answer) ? obj.answer : (obj ? JSON.stringify(obj) : null);
                  resolve({ ok: true, answer: text, job: job });
                }).catch(function () {
                  resolve({ ok: false, answer: null, job: Object.assign({}, job, { error: "Could not fetch answer artifact." }) });
                });
              }
              apiJson(artifactsPath).then(function (artifacts) {
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

    if (sendBtn) sendBtn.addEventListener("click", sendQuestion);
    if (inputEl) inputEl.addEventListener("keydown", function (e) { if (e.key === "Enter") sendQuestion(); });
  })();

  // ——— Load saved config into settings inputs (on first load) ———
  try {
    const base = localStorage.getItem(STORAGE_BASE_URL);
    if (base && el("base-url")) el("base-url").value = base;
    const key = localStorage.getItem(STORAGE_API_KEY);
    if (key && el("api-key")) el("api-key").value = key;
    const tenant = localStorage.getItem(STORAGE_TENANT);
    if (tenant && el("tenant-id")) el("tenant-id").value = tenant;
  } catch (_) {}

  el("refresh-loans").addEventListener("click", refreshLoans);
  updateProcessLoanButton();
  loadHealth();
  loadOllamaModels();
  setInterval(loadHealth, 30000);

  // ——— Form Fill ———
  (function initFormFill() {
    var selectEl = el("formfill-template");
    var generateBtn = el("formfill-generate-btn");
    if (!selectEl || !generateBtn) return;

    function populateSelect(templates) {
      selectEl.innerHTML = '<option value="">Form Fill...</option>';
      var groups = {};
      templates.forEach(function (t) {
        if (!groups[t.category]) groups[t.category] = [];
        groups[t.category].push(t);
      });
      Object.keys(groups).sort().forEach(function (cat) {
        var optgroup = document.createElement("optgroup");
        optgroup.label = cat;
        groups[cat].forEach(function (t) {
          var opt = document.createElement("option");
          opt.value = t.template_id;
          opt.textContent = t.display_name;
          if (t.description) opt.title = t.description;
          optgroup.appendChild(opt);
        });
        selectEl.appendChild(optgroup);
      });
    }

    // Static fallback so dropdown works even before API responds
    populateSelect([
      {template_id: "income_calc_w2", display_name: "Income Calc (W2)", category: "Income"},
      {template_id: "fha_max_mortgage_calc", display_name: "FHA Max Mortgage Calc", category: "FHA"},
      {template_id: "va_irrrl_recoupment_calc", display_name: "VA IRRRL Recoupment Calc", category: "VA"}
    ]);

    function loadTemplates() {
      apiFetch("/formfill/templates").then(function (data) {
        if (!data || !data.templates || !data.templates.length) return;
        populateSelect(data.templates);
      }).catch(function () { /* keep static fallback */ });
    }

    selectEl.addEventListener("change", function () {
      generateBtn.disabled = !selectEl.value;
    });

    generateBtn.addEventListener("click", function () {
      var templateId = selectEl.value;
      if (!templateId) return;
      if (!selectedRunId || !selectedLoanId) {
        showInlineMsg("Select a loan with a completed run first", "error");
        return;
      }
      var tenantId = getTenantId();
      var url = getBaseUrl() + "/tenants/" + tenantId + "/loans/" + selectedLoanId
              + "/runs/" + selectedRunId + "/formfill/" + templateId;

      generateBtn.disabled = true;
      selectEl.disabled = true;

      var headers = {};
      var key = getApiKey();
      if (key) headers["X-API-Key"] = key;

      fetch(url, { method: "POST", headers: headers })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().then(function (err) {
              throw new Error(err.detail || "Form generation failed");
            });
          }
          var filled = resp.headers.get("X-FormFill-Cells-Filled") || "?";
          var total = resp.headers.get("X-FormFill-Cells-Total") || "?";
          showInlineMsg("Form generated (" + filled + "/" + total + " cells filled)", "ok");
          return resp.blob();
        })
        .then(function (blob) {
          if (!blob) return;
          var disposition = templateId + ".xlsx";
          var blobUrl = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = blobUrl;
          a.download = disposition;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 10000);
        })
        .catch(function (err) {
          showInlineMsg("Form fill error: " + err.message, "error");
        })
        .finally(function () {
          selectEl.disabled = false;
          generateBtn.disabled = !selectEl.value;
        });
    });

    loadTemplates();
  })();

})();
