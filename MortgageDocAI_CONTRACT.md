MortgageDocAI_CONTRACT

Status: FINAL – Authoritative v1 (DO NOT DRIFT)
Last Updated: 2026-02-07
Owner: Bill Walters

PURPOSE
MortgageDocAI is an on-demand mortgage document processing system designed for determinism,
auditability, and minimal operational complexity. If code and this document disagree, this
document wins.

ARCHITECTURE (OPTION B – FINAL)
TrueNAS is the authoritative system of record for nas_ingest, nas_chunk, nas_analyze.
AI Server performs on-demand processing only; local SSDs are scratch/cache.
Synology provides read-only source documents and offsite backup.

MOUNTS
Source (RO): /mnt/source_loans/5-Borrowers TBD/
TrueNAS (RW, NFSv4):
- /mnt/nas_apps/nas_ingest
- /mnt/nas_apps/nas_chunk
- /mnt/nas_apps/nas_analyze

CANONICAL FILE SET
scripts/
- lib.py
- run_loan_pipeline.py
- step10_intake.py
- step11_process.py
- step12_analyze.py
- step13_build_retrieval_pack.py
- requirements.txt

IDENTITY RULES
tenant_id required (default: peak)
loan_id always explicit via CLI
document_id = SHA256(file bytes)

STEPS
Step 10: Intake / staging
Step 11: Process (extract, OCR, chunk, embed, Qdrant)
Step 12: Analyze (multi-query, auto Step 13)
Step 13: Retrieval Pack Builder

QDRANT
Local to AI server
Collection: {tenant_id}_e5largev2_1024_cosine_v1
Dim: 1024, Distance: cosine

ATOMIC PUBLISH
All writes go to _staging then atomic rename.

## Job Progress Phase Markers (PHASE lines)

Format (MUST): Each phase marker is a single line written to stdout:

    PHASE:<NAME> <UTC_ISO_Z>

where <UTC_ISO_Z> MUST be a UTC timestamp in the format YYYY-MM-DDTHH:MM:SSZ.
Example: PHASE:INTAKE 2026-02-07T12:00:00Z

Emission source (MUST): scripts/run_loan_job.py MUST emit these lines to stdout
during pipeline execution. No other process is authorised to emit PHASE: lines.

Ordering (SHOULD): Phase markers SHOULD appear in the following order.
Some phases are conditional on pipeline flags and output:

  1. PHASE:INTAKE              — only if --skip-intake is NOT set
  2. PHASE:PROCESS             — only if --skip-process is NOT set
  3. PHASE:STEP13_GENERAL      — always when retrieval pack is built
  4. PHASE:STEP13_INCOME       — only if income retrieval pack is built
  5. PHASE:STEP12_INCOME_ANALYSIS — only if income_analysis profile runs
  6. PHASE:STEP12_UW_DECISION  — only if uw_decision profile runs
  7. PHASE:DONE                — on successful completion
  8. PHASE:FAIL                — on failure (see Failure semantics below)

Stability (MUST): The phase names listed above are contract-stable.
They MUST NOT be renamed without a contract version bump and a coordinated
update to all consumers (UI, monitoring, downstream parsers).
Non-phase log lines MUST NOT start with the literal prefix PHASE: to avoid
ambiguous parsing by consumers.

Consumers (informational): The Web UI and API clients MAY parse these markers
from job stdout to display pipeline progress. Parsing PHASE: lines from job
stdout is supported and intentional behaviour, not an accidental coupling.

Failure semantics (MUST): On failure the system MUST emit PHASE:FAIL <UTC_ISO_Z>
to stdout — either from the pipeline itself or from the worker wrapper when an
unhandled exception or timeout occurs.

NON-NEGOTIABLES
No background daemons
No renaming scripts
No schema drift without updating this contract

