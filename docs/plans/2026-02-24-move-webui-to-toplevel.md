# Move WebUI to Top-Level Directory

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move `scripts/webui/` to a top-level `webui/` directory and update the single path reference in `loan_api.py`.

**Architecture:** Three files move via `git mv`. One line in `loan_api.py` changes from `SCRIPTS_DIR / "webui"` to `REPO_ROOT / "webui"`. No HTML/JS changes needed — all asset URLs are `/ui/static/…` (server-relative), never filesystem paths.

**Tech Stack:** Python/FastAPI, git

---

### Task 1: Move the three WebUI files

**Files:**
- Move: `scripts/webui/index.html` → `webui/index.html`
- Move: `scripts/webui/app.js` → `webui/app.js`
- Move: `scripts/webui/styles.css` → `webui/styles.css`

**Step 1: Move with git (preserves history)**

```bash
cd /opt/mortgagedocai
git mv scripts/webui/index.html webui/index.html
git mv scripts/webui/app.js     webui/app.js
git mv scripts/webui/styles.css webui/styles.css
```

**Step 2: Confirm old directory is empty and gone**

```bash
ls scripts/webui 2>&1
# Expected: ls: cannot access 'scripts/webui': No such file or directory
```

**Step 3: Confirm new directory has the three files**

```bash
ls webui/
# Expected: app.js  index.html  styles.css
```

---

### Task 2: Update WEBUI_DIR in loan_api.py

**Files:**
- Modify: `scripts/loan_api.py:1000`

**Step 1: Apply the one-line change**

Replace line 1000:
```python
WEBUI_DIR = SCRIPTS_DIR / "webui"
```
With:
```python
WEBUI_DIR = REPO_ROOT / "webui"
```

(`REPO_ROOT` is already defined at line 46 as `_scripts_dir.parent` — no new variable needed.)

**Step 2: Syntax check**

```bash
python3 -m py_compile scripts/loan_api.py && echo "OK"
# Expected: OK
```

---

### Task 3: Commit, restart, verify

**Step 1: Stage and commit**

```bash
git add webui/ scripts/loan_api.py
git status
# Expected: renamed: scripts/webui/app.js -> webui/app.js  (×3)
#           modified: scripts/loan_api.py
git commit -m "refactor: move webui/ from scripts/webui to top-level webui/"
```

**Step 2: Restart the API service**

```bash
sudo systemctl restart mortgagedocai-api
sleep 3
sudo systemctl is-active mortgagedocai-api
# Expected: active
```

**Step 3: Acceptance checks**

```bash
# UI HTML loads
curl -s http://127.0.0.1:8000/ui | head -5
# Expected: <!DOCTYPE html> ...

# Static asset resolves
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/static/app.js
# Expected: 200

# Health still OK
curl -s http://127.0.0.1:8000/health
# Expected: {"status":"ok"}
```

---

## Invariants confirmed

- `/ui` and `/ui/static/*` URL paths unchanged.
- No HTML/JS edits — asset URLs are server-relative, not filesystem paths.
- `index.html`, `app.js`, `styles.css` filenames unchanged.
- No new dependencies.
- Only change to `loan_api.py`: one token on one line (`SCRIPTS_DIR` → `REPO_ROOT`).
