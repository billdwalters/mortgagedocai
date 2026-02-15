# MortgageDocAI – Architecture Authority

## Roles

### System Architect (ChatGPT)
- Defines system architecture, invariants, and contracts
- Owns `MortgageDocAI_CONTRACT.md`
- Decides data ownership, processing steps, and guarantees
- Approves structural changes

### Implementation Assistant (Claude)
- Implements changes per the contract and architect instructions
- Must not refactor, rename, or redesign without approval
- Stops and asks if instructions conflict with contract

## Rule of precedence
1. MortgageDocAI_CONTRACT.md
2. ChatGPT architectural instructions
3. Implementation tasks

If any conflict exists, stop and ask.
