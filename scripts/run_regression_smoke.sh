#!/usr/bin/env bash
# run_regression_smoke.sh — deterministic regression smoke test for MortgageDocAI
# Proves end-to-end pipeline health: Step13 retrieval + Step12 analysis (phi3 + mistral)
set -euo pipefail

# ---------------------------------------------------------------------------
# HuggingFace / transformers progress-bar suppression (smoke-only defaults)
# ---------------------------------------------------------------------------
: "${HF_HUB_DISABLE_PROGRESS_BARS:=1}"
: "${TRANSFORMERS_VERBOSITY:=error}"
: "${TQDM_MININTERVAL:=5}"
export HF_HUB_DISABLE_PROGRESS_BARS TRANSFORMERS_VERBOSITY TQDM_MININTERVAL

# ---------------------------------------------------------------------------
# Env-var overrides (all have defaults)
# ---------------------------------------------------------------------------
TENANT_ID="${TENANT_ID:-peak}"
LOAN_ID="${LOAN_ID:-16271681}"
if [ -z "${RUN_ID:-}" ]; then
    _AUTO_RUN_DIR="/mnt/nas_apps/nas_chunk/tenants/${TENANT_ID}/loans/${LOAN_ID}"
    RUN_ID=$(ls -1 "$_AUTO_RUN_DIR" 2>/dev/null \
        | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$' \
        | sort \
        | tail -1) || true
    if [ -z "$RUN_ID" ]; then
        echo "FATAL: no RUN_ID set and no timestamped run dirs found under ${_AUTO_RUN_DIR}" >&2
        exit 1
    fi
    echo "Auto-selected RUN_ID=${RUN_ID} (latest in ${_AUTO_RUN_DIR})"
fi
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
EMBED_OFFLINE="${EMBED_OFFLINE:-1}"
TOP_K="${TOP_K:-80}"
QUERY_RETRIEVE="${QUERY_RETRIEVE:-conditions of approval underwriting conditions prior to closing PTC suspense approval conditions}"
QUERY_ANALYZE="${QUERY_ANALYZE:-List all underwriting conditions found in the provided documents.}"
PHI3_MODEL="${PHI3_MODEL:-phi3}"
MISTRAL_MODEL="${MISTRAL_MODEL:-mistral}"
PHI3_MAX_TOKENS="${PHI3_MAX_TOKENS:-900}"
PHI3_EVIDENCE_MAX_CHARS="${PHI3_EVIDENCE_MAX_CHARS:-4500}"
MISTRAL_MAX_TOKENS="${MISTRAL_MAX_TOKENS:-450}"
MISTRAL_EVIDENCE_MAX_CHARS="${MISTRAL_EVIDENCE_MAX_CHARS:-3500}"
OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-900}"
RUN_UW_CONDITIONS="${RUN_UW_CONDITIONS:-0}"
UW_QUERY="${UW_QUERY:-Extract underwriting conditions / conditions of approval as a checklist.}"
UW_MAX_TOKENS="${UW_MAX_TOKENS:-650}"
UW_EVIDENCE_MAX_CHARS="${UW_EVIDENCE_MAX_CHARS:-4500}"
RUN_INCOME_ANALYSIS="${RUN_INCOME_ANALYSIS:-0}"
INCOME_QUERY="${INCOME_QUERY:-Extract all income sources, liabilities, and proposed housing payment (PITIA) for DTI calculation.}"
INCOME_RETRIEVE_QUERY="${INCOME_RETRIEVE_QUERY:-Estimated Total Monthly Payment PITIA Proposed housing payment Principal & Interest Escrow Amount can increase over time Loan Estimate Closing Disclosure HOA dues Property Taxes Homeowners Insurance credit report liabilities monthly payment Total Monthly Payments monthly debt obligations Uniform Residential Loan Application assets and liabilities Gross Monthly Income Base Employment Income qualifying income Total Monthly Income Desktop Underwriter DU Findings Profit and Loss Net Income Total Income}"
INCOME_MAX_TOKENS="${INCOME_MAX_TOKENS:-650}"
INCOME_EVIDENCE_MAX_CHARS="${INCOME_EVIDENCE_MAX_CHARS:-4500}"
INCOME_TOP_K="${INCOME_TOP_K:-120}"
INCOME_MAX_PER_FILE="${INCOME_MAX_PER_FILE:-12}"
INCOME_REQUIRED_KEYWORDS="${INCOME_REQUIRED_KEYWORDS:-Total Monthly Payments}"
INCOME_REQUIRED_KEYWORDS_2="${INCOME_REQUIRED_KEYWORDS_2:-Profit and Loss}"
EXPECT_DTI="${EXPECT_DTI:-0}"
MAX_DROPPED_CHUNKS="${MAX_DROPPED_CHUNKS:-999999}"
RUN_LLM="${RUN_LLM:-1}"
RUN_UW_DECISION="${RUN_UW_DECISION:-0}"
UW_DECISION_QUERY="${UW_DECISION_QUERY:-Deterministic underwriting decision based on DTI thresholds.}"
SMOKE_DEBUG="${SMOKE_DEBUG:-0}"
EXPECT_RP_HASH_STABLE="${EXPECT_RP_HASH_STABLE:-0}"

# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------
BASE="/mnt/nas_apps/nas_analyze/tenants/${TENANT_ID}/loans/${LOAN_ID}"
RP_PATH="${BASE}/retrieve/${RUN_ID}/retrieval_pack.json"
PHI3_ROOT="${BASE}/${RUN_ID}/outputs/profiles/default_phi3"
MISTRAL_ROOT="${BASE}/${RUN_ID}/outputs/profiles/default_mistral"
UW_ROOT="${BASE}/${RUN_ID}/outputs/profiles/uw_conditions"
INCOME_ROOT="${BASE}/${RUN_ID}/outputs/profiles/income_analysis"
UW_DECISION_ROOT="${BASE}/${RUN_ID}/outputs/profiles/uw_decision"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FAIL=0

fail() {
    echo "FAIL: $1" >&2
    FAIL=1
}

# ---------------------------------------------------------------------------
# Pre-flight: materialize autofs mount (if applicable)
# ---------------------------------------------------------------------------
ls /mnt/source_loans >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# A) Build retrieval pack
# ---------------------------------------------------------------------------
echo "=== Step13: building retrieval pack ==="
OFFLINE_FLAG=""
if [ "${EMBED_OFFLINE}" = "1" ]; then
    OFFLINE_FLAG="--offline-embeddings"
fi
STEP13_DEBUG_FLAG=""
if [ "${SMOKE_DEBUG}" = "1" ]; then STEP13_DEBUG_FLAG="--debug"; fi

python3 "${SCRIPT_DIR}/step13_build_retrieval_pack.py" \
    --tenant-id "$TENANT_ID" \
    --loan-id "$LOAN_ID" \
    --run-id "$RUN_ID" \
    --query "$QUERY_RETRIEVE" \
    --out-run-id "$RUN_ID" \
    --top-k "$TOP_K" \
    ${OFFLINE_FLAG} \
    ${STEP13_DEBUG_FLAG}

# ---------------------------------------------------------------------------
# B) Assert retrieval_pack.json exists and retrieved_chunks > 0
# ---------------------------------------------------------------------------
echo "=== Assert: retrieval_pack.json ==="
if [ ! -f "$RP_PATH" ]; then
    fail "retrieval_pack.json not found at ${RP_PATH}"
else
    RP_CHUNK_COUNT=$(python3 -c "
import json, sys
rp = json.load(open('${RP_PATH}'))
chunks = rp.get('retrieved_chunks', []) or []
print(len(chunks))
")
    if [ "$RP_CHUNK_COUNT" -eq 0 ]; then
        fail "retrieval_pack.json has 0 retrieved_chunks"
    else
        echo "  retrieved_chunks: ${RP_CHUNK_COUNT}"
    fi
fi

# ---------------------------------------------------------------------------
# B2) Optional: verify retrieval_pack.json hash stability
# ---------------------------------------------------------------------------
if [ "${EXPECT_RP_HASH_STABLE}" = "1" ]; then
    echo "=== Assert: retrieval_pack.json hash stability ==="
    RP_HASH_1=$(sha256sum "$RP_PATH" | awk '{print $1}')

    python3 "${SCRIPT_DIR}/step13_build_retrieval_pack.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$QUERY_RETRIEVE" \
        --out-run-id "$RUN_ID" \
        --top-k "$TOP_K" \
        ${OFFLINE_FLAG} \
        ${STEP13_DEBUG_FLAG}

    RP_HASH_2=$(sha256sum "$RP_PATH" | awk '{print $1}')

    if [ "$RP_HASH_1" != "$RP_HASH_2" ]; then
        fail "retrieval_pack.json hash changed across identical Step13 runs (${RP_HASH_1} != ${RP_HASH_2})"
    else
        echo "  OK: retrieval_pack.json hash stable (${RP_HASH_1})"
    fi
fi

# ---------------------------------------------------------------------------
# C) Run Step12 with phi3
# D) Assert phi3 outputs exist and citations.jsonl > 0
# ---------------------------------------------------------------------------
if [ "${RUN_LLM}" = "1" ]; then
    echo "=== Step12: phi3 ==="
    python3 "${SCRIPT_DIR}/step12_analyze.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$QUERY_ANALYZE" \
        --analysis-profile default_phi3 \
        --ollama-url "$OLLAMA_URL" \
        --llm-model "$PHI3_MODEL" \
        --llm-temperature 0 \
        --llm-max-tokens "$PHI3_MAX_TOKENS" \
        --evidence-max-chars "$PHI3_EVIDENCE_MAX_CHARS" \
        --ollama-timeout "$OLLAMA_TIMEOUT" \
        --save-llm-raw

    echo "=== Assert: phi3 outputs ==="
    PHI3_ANSWER="${PHI3_ROOT}/answer.json"
    PHI3_CITATIONS="${PHI3_ROOT}/citations.jsonl"
    PHI3_RAW="${PHI3_ROOT}/llm_raw.txt"

    if [ ! -f "$PHI3_ANSWER" ]; then
        fail "phi3: answer.json not found at ${PHI3_ANSWER}"
    fi
    if [ ! -f "$PHI3_CITATIONS" ]; then
        fail "phi3: citations.jsonl not found at ${PHI3_CITATIONS}"
    else
        PHI3_CIT_COUNT=$(wc -l < "$PHI3_CITATIONS" | tr -d ' ')
        if [ "$PHI3_CIT_COUNT" -eq 0 ]; then
            fail "phi3: citations.jsonl has 0 lines"
        else
            echo "  phi3 citations: ${PHI3_CIT_COUNT}"
        fi
    fi
    if [ ! -f "$PHI3_RAW" ]; then
        fail "phi3: llm_raw.txt not found (--save-llm-raw not working?)"
    else
        echo "  phi3 llm_raw.txt: $(wc -c < "$PHI3_RAW" | tr -d ' ') bytes"
    fi
fi

# ---------------------------------------------------------------------------
# E) Run Step12 with mistral
# F) Assert mistral outputs exist and citations.jsonl > 0
# ---------------------------------------------------------------------------
if [ "${RUN_LLM}" = "1" ]; then
    echo "=== Step12: mistral ==="
    python3 "${SCRIPT_DIR}/step12_analyze.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$QUERY_ANALYZE" \
        --analysis-profile default_mistral \
        --ollama-url "$OLLAMA_URL" \
        --llm-model "$MISTRAL_MODEL" \
        --llm-temperature 0 \
        --llm-max-tokens "$MISTRAL_MAX_TOKENS" \
        --evidence-max-chars "$MISTRAL_EVIDENCE_MAX_CHARS" \
        --ollama-timeout "$OLLAMA_TIMEOUT" \
        --save-llm-raw

    echo "=== Assert: mistral outputs ==="
    MISTRAL_ANSWER="${MISTRAL_ROOT}/answer.json"
    MISTRAL_CITATIONS="${MISTRAL_ROOT}/citations.jsonl"
    MISTRAL_RAW="${MISTRAL_ROOT}/llm_raw.txt"

    if [ ! -f "$MISTRAL_ANSWER" ]; then
        fail "mistral: answer.json not found at ${MISTRAL_ANSWER}"
    fi
    if [ ! -f "$MISTRAL_CITATIONS" ]; then
        fail "mistral: citations.jsonl not found at ${MISTRAL_CITATIONS}"
    else
        MISTRAL_CIT_COUNT=$(wc -l < "$MISTRAL_CITATIONS" | tr -d ' ')
        if [ "$MISTRAL_CIT_COUNT" -eq 0 ]; then
            fail "mistral: citations.jsonl has 0 lines"
        else
            echo "  mistral citations: ${MISTRAL_CIT_COUNT}"
        fi
    fi
    if [ ! -f "$MISTRAL_RAW" ]; then
        fail "mistral: llm_raw.txt not found (--save-llm-raw not working?)"
    else
        echo "  mistral llm_raw.txt: $(wc -c < "$MISTRAL_RAW" | tr -d ' ') bytes"
    fi
fi

# ---------------------------------------------------------------------------
# G) Integrity checks: every cited chunk_id must be in retrieval_pack
# ---------------------------------------------------------------------------
if [ "${RUN_LLM}" = "1" ]; then
    echo "=== Integrity: citation chunk_id ∈ retrieval_pack ==="
    INTEGRITY_RESULT=$(python3 -c "
import json, sys

rp = json.load(open('${RP_PATH}'))
rp_ids = set()
for ch in (rp.get('retrieved_chunks') or []):
    cid = ch.get('chunk_id') or ch.get('payload', {}).get('chunk_id') or ''
    if cid:
        rp_ids.add(cid)

errors = []

# Check both phi3 and mistral profiles (separate output dirs)
for profile_root in ['${PHI3_ROOT}', '${MISTRAL_ROOT}']:
    answer = json.load(open(profile_root + '/answer.json'))
    for c in (answer.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'{profile_root}: cited chunk_id {cid} NOT in retrieval_pack')

    with open(profile_root + '/citations.jsonl') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            cid = c.get('chunk_id', '')
            if cid and cid not in rp_ids:
                errors.append(f'{profile_root}: cited chunk_id {cid} NOT in retrieval_pack')

if errors:
    for e in errors:
        print(f'INTEGRITY FAIL: {e}')
    sys.exit(1)
else:
    print(f'OK: all cited chunk_ids found in retrieval_pack ({len(rp_ids)} rp_ids)')
    sys.exit(0)
")
    if [ $? -ne 0 ]; then
        fail "citation integrity check failed"
        echo "$INTEGRITY_RESULT"
    else
        echo "  ${INTEGRITY_RESULT}"
    fi
fi

# ---------------------------------------------------------------------------
# H) Optional: uw_conditions profile
# ---------------------------------------------------------------------------
UW_COND_COUNT="skip"
UW_CIT_TOTAL="skip"
if [ "${RUN_UW_CONDITIONS}" = "1" ]; then
    echo "=== Step12: uw_conditions (mistral) ==="
    python3 "${SCRIPT_DIR}/step12_analyze.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$UW_QUERY" \
        --analysis-profile uw_conditions \
        --ollama-url "$OLLAMA_URL" \
        --llm-model "$MISTRAL_MODEL" \
        --llm-temperature 0 \
        --llm-max-tokens "$UW_MAX_TOKENS" \
        --evidence-max-chars "$UW_EVIDENCE_MAX_CHARS" \
        --ollama-timeout "$OLLAMA_TIMEOUT" \
        --save-llm-raw

    echo "=== Assert: uw_conditions outputs ==="
    UW_CONDITIONS_JSON="${UW_ROOT}/conditions.json"
    if [ ! -f "$UW_CONDITIONS_JSON" ]; then
        fail "uw_conditions: conditions.json not found at ${UW_CONDITIONS_JSON}"
    else
        UW_CHECK=$(python3 -c "
import json, sys

cj = json.load(open('${UW_CONDITIONS_JSON}'))
conds = cj.get('conditions', [])
if not isinstance(conds, list):
    print('FAIL: conditions is not a list')
    sys.exit(1)

total_cits = sum(len(c.get('citations', [])) for c in conds)
print(f'{len(conds)} {total_cits}')
sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "uw_conditions: conditions.json validation failed: ${UW_CHECK}"
        else
            UW_COND_COUNT=$(echo "$UW_CHECK" | awk '{print $1}')
            UW_CIT_TOTAL=$(echo "$UW_CHECK" | awk '{print $2}')
            echo "  uw_conditions: ${UW_COND_COUNT} conditions, ${UW_CIT_TOTAL} total citations"
        fi
    fi

    # Verify per-profile version.json
    if [ ! -f "${UW_ROOT}/version.json" ]; then
        fail "uw_conditions: version.json not found at ${UW_ROOT}/version.json"
    else
        echo "  uw_conditions: version.json found"
    fi

    echo "=== Integrity: uw_conditions chunk_id ∈ retrieval_pack ==="
    if [ -f "$UW_CONDITIONS_JSON" ]; then
        UW_INTEGRITY=$(python3 -c "
import json, sys

rp = json.load(open('${RP_PATH}'))
rp_ids = set()
for ch in (rp.get('retrieved_chunks') or []):
    cid = ch.get('chunk_id') or ch.get('payload', {}).get('chunk_id') or ''
    if cid:
        rp_ids.add(cid)

cj = json.load(open('${UW_CONDITIONS_JSON}'))
errors = []
for i, cond in enumerate(cj.get('conditions', [])):
    for c in (cond.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'condition[{i}]: cited chunk_id {cid} NOT in retrieval_pack')

if errors:
    for e in errors:
        print(f'INTEGRITY FAIL: {e}')
    sys.exit(1)
else:
    print(f'OK: all uw_conditions chunk_ids found in retrieval_pack')
    sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "uw_conditions citation integrity check failed"
            echo "$UW_INTEGRITY"
        else
            echo "  ${UW_INTEGRITY}"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# I) Optional: income_analysis profile
# ---------------------------------------------------------------------------
INCOME_ITEMS_COUNT="skip"
INCOME_LIAB_COUNT="skip"
INCOME_DTI="skip"
INCOME_PITIA="skip"
if [ "${RUN_INCOME_ANALYSIS}" = "1" ]; then
    echo "=== Step13: building income-focused retrieval pack ==="
    _STEP13_INC_OUT=$(python3 "${SCRIPT_DIR}/step13_build_retrieval_pack.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$INCOME_RETRIEVE_QUERY" \
        --out-run-id "$RUN_ID" \
        --top-k "$INCOME_TOP_K" \
        --max-per-file "$INCOME_MAX_PER_FILE" \
        --required-keywords "$INCOME_REQUIRED_KEYWORDS" \
        --required-keywords "$INCOME_REQUIRED_KEYWORDS_2" \
        --debug \
        ${OFFLINE_FLAG} 2>&1)
    if [ "${SMOKE_DEBUG}" = "1" ]; then
        echo "$_STEP13_INC_OUT"
    else
        echo "$_STEP13_INC_OUT" | grep -v '^\[DEBUG\]'
    fi

    # Gate: dropped chunk_ids must not exceed MAX_DROPPED_CHUNKS
    _DROPPED_COUNT=$(echo "$_STEP13_INC_OUT" \
        | grep -o '[0-9]*/[0-9]* chunk_ids not found' \
        | sed 's|/.*||' \
        | tail -1) || true
    _DROPPED_COUNT="${_DROPPED_COUNT:-0}"
    if [ "$_DROPPED_COUNT" -gt "$MAX_DROPPED_CHUNKS" ]; then
        fail "income Step13: dropped ${_DROPPED_COUNT} chunk_ids (max allowed: ${MAX_DROPPED_CHUNKS})"
    fi

    echo "=== Step12: income_analysis (mistral) ==="
    python3 "${SCRIPT_DIR}/step12_analyze.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$INCOME_QUERY" \
        --analysis-profile income_analysis \
        --ollama-url "$OLLAMA_URL" \
        --llm-model "$MISTRAL_MODEL" \
        --llm-temperature 0 \
        --llm-max-tokens "$INCOME_MAX_TOKENS" \
        --evidence-max-chars "$INCOME_EVIDENCE_MAX_CHARS" \
        --ollama-timeout "$OLLAMA_TIMEOUT" \
        --save-llm-raw \
        --debug

    echo "=== Assert: income_analysis outputs ==="
    INCOME_FE="${INCOME_ROOT}/income_analysis.json"
    INCOME_DTI_FILE="${INCOME_ROOT}/dti.json"

    if [ ! -f "$INCOME_FE" ]; then
        fail "income_analysis: income_analysis.json not found at ${INCOME_FE}"
    else
        FE_CHECK=$(python3 -c "
import json, sys

fe = json.load(open('${INCOME_FE}'))
inc = fe.get('income_items', [])
liab = fe.get('liability_items', [])
if not isinstance(inc, list):
    print('FAIL: income_items is not a list')
    sys.exit(1)
if not isinstance(liab, list):
    print('FAIL: liability_items is not a list')
    sys.exit(1)

# Check proposed_pitia structure (may be null, but if present must have value)
pitia = fe.get('proposed_pitia')
pitia_status = 'null'
if pitia is not None:
    if not isinstance(pitia, dict) or 'value' not in pitia:
        print('FAIL: proposed_pitia present but missing value field')
        sys.exit(1)
    pitia_status = 'value=' + str(pitia.get('value'))

print(str(len(inc)) + ' ' + str(len(liab)) + ' ' + pitia_status)
sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "income_analysis: income_analysis.json validation failed: ${FE_CHECK}"
        else
            INCOME_ITEMS_COUNT=$(echo "$FE_CHECK" | awk '{print $1}')
            INCOME_LIAB_COUNT=$(echo "$FE_CHECK" | awk '{print $2}')
            INCOME_PITIA=$(echo "$FE_CHECK" | awk '{print $3}')
            echo "  income_items: ${INCOME_ITEMS_COUNT}, liability_items: ${INCOME_LIAB_COUNT}, proposed_pitia: ${INCOME_PITIA}"
        fi
    fi

    if [ ! -f "$INCOME_DTI_FILE" ]; then
        fail "income_analysis: dti.json not found at ${INCOME_DTI_FILE}"
    else
        DTI_CHECK=$(python3 -c "
import json, sys

dti = json.load(open('${INCOME_DTI_FILE}'))
bed = dti.get('back_end_dti')
fed = dti.get('front_end_dti')
mit = dti.get('monthly_income_total')
mdt = dti.get('monthly_debt_total')
hpu = dti.get('housing_payment_used')
mi = dti.get('missing_inputs', [])
mic = dti.get('monthly_income_combined')
fedc = dti.get('front_end_dti_combined')
bedc = dti.get('back_end_dti_combined')
print(f'back_end_dti={bed} front_end_dti={fed} income={mit} debt={mdt} housing={hpu} missing={mi}')
print(f'  combined: income={mic} front_end_dti={fedc} back_end_dti={bedc}')
sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "income_analysis: dti.json validation failed: ${DTI_CHECK}"
        else
            INCOME_DTI="$DTI_CHECK"
            echo "  dti.json: ${DTI_CHECK}"
        fi
    fi

    # EXPECT_DTI: when 1, assert front_end_dti or back_end_dti is non-null
    if [ "${EXPECT_DTI}" = "1" ]; then
        echo "=== Assert: EXPECT_DTI — at least one DTI ratio must be non-null ==="
        if [ -f "$INCOME_DTI_FILE" ]; then
            DTI_EXPECT_CHECK=$(python3 -c "
import json, sys
dti = json.load(open('${INCOME_DTI_FILE}'))
fed = dti.get('front_end_dti')
bed = dti.get('back_end_dti')
if fed is None and bed is None:
    mi = dti.get('missing_inputs', [])
    print(f'FAIL: both front_end_dti and back_end_dti are null; missing_inputs={mi}')
    sys.exit(1)
print(f'OK: front_end_dti={fed} back_end_dti={bed}')
sys.exit(0)
")
            if [ $? -ne 0 ]; then
                fail "EXPECT_DTI: ${DTI_EXPECT_CHECK}"
            else
                echo "  ${DTI_EXPECT_CHECK}"
            fi
        fi
    fi

    # Assert: if retrieval_pack contains PITIA evidence, proposed_pitia must be non-null
    echo "=== Assert: PITIA evidence → proposed_pitia non-null ==="
    if [ -f "$RP_PATH" ] && [ -f "$INCOME_FE" ]; then
        PITIA_EVIDENCE_CHECK=$(python3 -c "
import json, sys

rp = json.load(open('${RP_PATH}'))
has_pitia_evidence = False
for ch in (rp.get('retrieved_chunks') or []):
    text = (ch.get('text') or '').lower()
    if 'total monthly payment' in text:
        has_pitia_evidence = True
        break

if not has_pitia_evidence:
    print('SKIP: no PITIA evidence in retrieval_pack (Total Monthly Payment not found)')
    sys.exit(0)

fe = json.load(open('${INCOME_FE}'))
pitia = fe.get('proposed_pitia')
if pitia is None or not isinstance(pitia, dict) or pitia.get('value') is None:
    print('FAIL: retrieval_pack contains Total Monthly Payment evidence but proposed_pitia is null')
    sys.exit(1)
print('OK: PITIA evidence found and proposed_pitia.value=' + str(pitia.get('value')))
sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "PITIA evidence check: ${PITIA_EVIDENCE_CHECK}"
        else
            echo "  ${PITIA_EVIDENCE_CHECK}"
        fi
    fi

    # Verify per-profile version.json
    if [ ! -f "${INCOME_ROOT}/version.json" ]; then
        fail "income_analysis: version.json not found at ${INCOME_ROOT}/version.json"
    else
        echo "  income_analysis: version.json found"
    fi

    echo "=== Integrity: income_analysis chunk_id ∈ retrieval_pack ==="
    if [ -f "$INCOME_FE" ]; then
        INC_INTEGRITY=$(python3 -c "
import json, sys

rp = json.load(open('${RP_PATH}'))
rp_ids = set()
for ch in (rp.get('retrieved_chunks') or []):
    cid = ch.get('chunk_id') or ch.get('payload', {}).get('chunk_id') or ''
    if cid:
        rp_ids.add(cid)

fe = json.load(open('${INCOME_FE}'))
errors = []

for i, item in enumerate(fe.get('income_items', [])):
    for c in (item.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'income_items[{i}]: cited chunk_id {cid} NOT in retrieval_pack')

for i, item in enumerate(fe.get('liability_items', [])):
    for c in (item.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'liability_items[{i}]: cited chunk_id {cid} NOT in retrieval_pack')

# Check proposed_pitia citations
pitia = fe.get('proposed_pitia')
if isinstance(pitia, dict):
    for c in (pitia.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'proposed_pitia: cited chunk_id {cid} NOT in retrieval_pack')

# Check monthly_liabilities_total citations
mlt = fe.get('monthly_liabilities_total')
if isinstance(mlt, dict):
    for c in (mlt.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'monthly_liabilities_total: cited chunk_id {cid} NOT in retrieval_pack')

# Check monthly_income_total citations
mit = fe.get('monthly_income_total')
if isinstance(mit, dict):
    for c in (mit.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'monthly_income_total: cited chunk_id {cid} NOT in retrieval_pack')

# Check monthly_income_total_combined citations
mic = fe.get('monthly_income_total_combined')
if isinstance(mic, dict):
    for c in (mic.get('citations') or []):
        cid = c.get('chunk_id', '')
        if cid and cid not in rp_ids:
            errors.append(f'monthly_income_total_combined: cited chunk_id {cid} NOT in retrieval_pack')

if errors:
    for e in errors:
        print(f'INTEGRITY FAIL: {e}')
    sys.exit(1)
else:
    print(f'OK: all income_analysis chunk_ids found in retrieval_pack')
    sys.exit(0)
")
        if [ $? -ne 0 ]; then
            fail "income_analysis citation integrity check failed"
            echo "$INC_INTEGRITY"
        else
            echo "  ${INC_INTEGRITY}"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# K) Optional: uw_decision profile (requires income_analysis)
# ---------------------------------------------------------------------------
UW_DEC_PRIMARY="skip"
UW_DEC_COMBINED="skip"
if [ "${RUN_UW_DECISION}" = "1" ]; then
    echo "=== Step12: uw_decision (deterministic) ==="
    python3 "${SCRIPT_DIR}/step12_analyze.py" \
        --tenant-id "$TENANT_ID" \
        --loan-id "$LOAN_ID" \
        --run-id "$RUN_ID" \
        --query "$UW_DECISION_QUERY" \
        --analysis-profile uw_decision \
        --ollama-url "$OLLAMA_URL" \
        --llm-model "$MISTRAL_MODEL" \
        --llm-temperature 0 \
        --llm-max-tokens 1 \
        --ollama-timeout 10 \
        --no-auto-retrieve \
        --debug

    if [ $? -ne 0 ]; then
        fail "Step12 uw_decision exited with error"
    else
        echo "=== Assert: uw_decision outputs ==="
        UW_DECISION_JSON="${UW_DECISION_ROOT}/decision.json"
        if [ ! -f "$UW_DECISION_JSON" ]; then
            fail "uw_decision: decision.json not found at ${UW_DECISION_JSON}"
        else
            UW_DEC_CHECK=$(python3 -c "
import json, sys

dj = json.load(open('${UW_DECISION_JSON}'))
primary = dj.get('decision_primary', {})
p_status = primary.get('status')
if p_status not in ('PASS', 'FAIL', 'UNKNOWN'):
    print(f'FAIL: decision_primary.status={p_status!r} not in (PASS, FAIL, UNKNOWN)')
    sys.exit(1)

version = dj.get('ruleset', {}).get('version')
if version != 'v0.7-policy':
    print(f'FAIL: ruleset.version={version!r} expected v0.7-policy')
    sys.exit(1)

policy_src = dj.get('ruleset', {}).get('policy_source')
if policy_src not in ('file', 'default'):
    print(f'FAIL: ruleset.policy_source={policy_src!r} not in (file, default)')
    sys.exit(1)

combined = dj.get('decision_combined')
c_status = 'N/A'
if combined is not None:
    c_status = combined.get('status', 'N/A')
    if c_status not in ('PASS', 'FAIL', 'UNKNOWN'):
        print(f'FAIL: decision_combined.status={c_status!r} not valid')
        sys.exit(1)

cits = dj.get('citations', {})
for key in ('pitia', 'liabilities', 'income_primary'):
    if key not in cits:
        print(f'FAIL: citations.{key} missing')
        sys.exit(1)

print(f'primary={p_status} combined={c_status} policy={policy_src}')
sys.exit(0)
")
            if [ $? -ne 0 ]; then
                fail "uw_decision: decision.json validation failed: ${UW_DEC_CHECK}"
            else
                UW_DEC_PRIMARY=$(echo "$UW_DEC_CHECK" | sed -n 's/primary=\([^ ]*\).*/\1/p')
                UW_DEC_COMBINED=$(echo "$UW_DEC_CHECK" | sed -n 's/.*combined=\([^ ]*\).*/\1/p')
                UW_DEC_POLICY=$(echo "$UW_DEC_CHECK" | sed -n 's/.*policy=\([^ ]*\).*/\1/p')
                echo "  uw_decision: ${UW_DEC_CHECK}"
            fi
        fi

        # Verify standard profile files exist
        for f in answer.json answer.md decision.md; do
            if [ ! -f "${UW_DECISION_ROOT}/${f}" ]; then
                fail "uw_decision: ${f} not found"
            fi
        done

        # Verify version.json exists at run-level outputs/_meta
        VERSION_JSON="${BASE}/${RUN_ID}/outputs/_meta/version.json"
        if [ ! -f "$VERSION_JSON" ]; then
            fail "uw_decision: version.json not found at ${VERSION_JSON}"
        else
            echo "  uw_decision: version.json found"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
if [ "$FAIL" -ne 0 ]; then
    echo "SMOKE TEST: FAIL"
    exit 1
else
    echo "SMOKE TEST: PASS"
    echo "  retrieved_chunks : ${RP_CHUNK_COUNT:-?}"
    if [ "${RUN_LLM}" = "1" ]; then
        echo "  phi3 citations   : ${PHI3_CIT_COUNT:-?}"
        echo "  mistral citations: ${MISTRAL_CIT_COUNT:-?}"
    fi
    if [ "${RUN_UW_CONDITIONS}" = "1" ]; then
        echo "  uw_conditions    : ${UW_COND_COUNT} conditions, ${UW_CIT_TOTAL} citations"
    fi
    if [ "${RUN_INCOME_ANALYSIS}" = "1" ]; then
        echo "  income_analysis  : income=${INCOME_ITEMS_COUNT} liab=${INCOME_LIAB_COUNT} pitia=${INCOME_PITIA}"
        echo "  dti              : ${INCOME_DTI}"
    fi
    if [ "${RUN_UW_DECISION}" = "1" ]; then
        echo "  uw_decision      : primary=${UW_DEC_PRIMARY} combined=${UW_DEC_COMBINED} policy=${UW_DEC_POLICY}"
    fi
    exit 0
fi
