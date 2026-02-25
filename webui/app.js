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
  const STALL_MS = 120000;   // 2 min without job-state change → stale
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
    STEP13_INCOME: "Income Retrieval",
    STEP12_INCOME_ANALYSIS: "Income Analysis",
    STEP12_UW_DECISION: "UW Decision",
    DONE: "Done",
    FAIL: "Failed",
  };

  const STEPPER_ORDER = [
    "INTAKE", "PROCESS", "STEP13_GENERAL", "STEP13_INCOME",
    "STEP12_INCOME_ANALYSIS", "STEP12_UW_DECISION", "DONE", "FAIL",
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

  // ——— Loans list (source-of-truth from GET /tenants/{tenant}/source_loans) ———
  async function refreshLoans() {
    const listEl = el("loan-list");
    if (!listEl) return;
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
        const lastText = (it.last_processed_utc != null && it.last_processed_utc !== "") ? it.last_processed_utc : "Never";
        const badge = it.needs_reprocess ? "Needs Processing" : "Up to date";
        const badgeClass = it.needs_reprocess ? "loan-badge needs-processing" : "loan-badge up-to-date";
        li.innerHTML =
          "<span class=\"loan-id\">" + escapeHtml(it.loan_id) + "</span>" +
          "<span class=\"loan-folder-name small\">" + escapeHtml(it.folder_name || "") + "</span>" +
          "<span class=\"loan-meta small\">Source: " + escapeHtml(it.source_last_modified_utc || "") + "</span>" +
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
    }
  }

  function updateProcessLoanButton() {
    const btn = el("process-loan-btn");
    if (btn) btn.disabled = !selectedLoanId;
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  function hideMainOverview() {
    const main = el("main-overview");
    if (main) main.hidden = true;
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
      if (el("overview-last-processed")) el("overview-last-processed").textContent = (item.last_processed_utc != null && item.last_processed_utc !== "") ? item.last_processed_utc : "Never";
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
    if (detailsRun) detailsRun.textContent = selectedRunId ? "run_id: " + selectedRunId : "";
    updateProcessLoanButton();
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
      if (!selectedLoanId) {
        alert("Select a loan first.");
        return;
      }
      const pathInput = el("source-path-input");
      const sourcePath = (pathInput && pathInput.value) ? pathInput.value.trim() : "";
      if (!sourcePath) {
        alert("Set the source folder (click Change…) and try again.");
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
        if (el("details-run-id")) el("details-run-id").textContent = "run_id: " + (data.run_id || "");
        showProgressPanel();
        startJobPolling(data.job_id);
      } catch (err) {
        alert("Error: " + (err.message || err));
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

          var status = (job && job.status) ? job.status : "";

          if (status === "PENDING" || status === "RUNNING" || status === "QUEUED") {
            if (Date.now() - lastChangeAtMs > STALL_MS) {
              showPollWarning("Job appears stalled (no progress updates for 2m).");
              stopPolling();
            }
          }

          setJobFields(job);
          if (!stopped) { hidePollWarning(); }

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
          if (consecutiveFailures >= MAX_FAILURES) {
            showPollWarning(
              "Connection lost to server \u2014 polling stopped. Check VPN/Tailscale or API service."
            );
            stopPolling();
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

  // ——— View Artifacts ———
  (function initViewArtifacts() {
    const btn = el("view-artifacts-btn");
    const panel = el("artifacts-panel");
    const indexEl = el("artifacts-index");
    const previewWrap = el("artifact-preview-wrap");
    const previewContent = el("artifact-preview-content");
    if (!btn || !panel || !indexEl) return;
    btn.addEventListener("click", async function () {
      const runId = selectedRunId || (selectedLoanId && lastProcessedCache[selectedLoanId] && lastProcessedCache[selectedLoanId].run_id);
      if (!selectedLoanId || !runId) {
        alert("No run selected. Process a loan first or select a loan that has been processed.");
        return;
      }
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
            apiFetch(url).then(function (r) { return r.text(); }).then(function (text) {
              if (filename.endsWith(".json")) {
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
              previewWrap.hidden = false;
            });
          });
        });
      } catch (e) {
        indexEl.innerHTML = "<p class=\"muted\">Error: " + escapeHtml(e.message || e) + "</p>";
      }
    });
  })();

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
      if (!selectedLoanId) {
        appendMessage("assistant", "Select a loan first.");
        return;
      }
      // Use the run shown in the UI (from loan list or last job) so we don't hit a stale cached run_id
      let runId = selectedRunId;
      if (!runId) runId = await getLatestSuccessRunId();
      if (!runId) {
        appendMessage("assistant", "No successful run for this loan. Process the loan first.");
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
})();
