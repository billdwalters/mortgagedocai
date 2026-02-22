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

NON-NEGOTIABLES
No background daemons
No renaming scripts
No schema drift without updating this contract

