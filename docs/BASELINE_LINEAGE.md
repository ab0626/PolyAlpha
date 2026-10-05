# Research baseline lineage

Every research baseline has an immutable identity (a git tag + a frozen
`research_logic_sha256`). This file records the lineage so a baseline is never
ambiguously "moved". The original tags are never rewritten; a rebaseline gets a
new identity.

## v0.3.0 — original (2026-09-15)

- tag: `v0.3.0-research-baseline` → `427bc150`
- `research_logic_sha256`: `df71b8f3…` (as recorded at freeze)
- status: **immutable history** — never moved.

## v0.3.0-r1 — rebaseline (effective 2026-09-23)

- tag: `v0.3.0-r1-research-baseline` → `fe365d9`
- `research_logic_sha256`: `b48fdf8d4b39ee420d0cb06073161cecf0edeba1e6345ed5d7d75bf6f4bdf1ac`
- reason: commit `3e8c3d4` "fix: fail-closed pre-trade gate, notional sizing
  units, deployment backlog" changed frozen research-logic files after the
  original freeze without re-freezing, so `freeze_baseline.py verify` drifted
  red.
- exact diff (`3e8c3d4`, frozen-path files):
  - `src/polyalpha/sizing.py` — `max_shares` → `max_notional` (units/name
    clarification; the cap value and `min(size, cap)` computation are
    numerically unchanged).
  - `src/polyalpha/pre_trade_gate.py` — fail-closed execution-safety behavior
    (not research logic).
  - `src/polyalpha/order_intent.py` — binds resolution/fee-schedule hashes
    (execution safety).
- **numerical research behavior: UNCHANGED.** The sizing correction is a
  units/name change with identical numeric output; the rest is execution-safety
  code. The frozen information-propagation hash is unchanged (`51c370…`).

## v0.4 — information propagation (unchanged)

- tag: `v0.4-info-propagation-baseline` → `1c6ef1d`
- `info_propagation_sha256`: `51c3701808cae0ead28c80fef08313556fe21db8f9e850fcefe8fe2627ad31f0`
- status: **unchanged** — still cleanly anchored.

## Rebaseline procedure

`scripts/freeze_baseline.py freeze` now takes `--tag` and `--commit` so a
rebaseline records an explicit identity without moving HEAD:

    .venv/Scripts/python.exe scripts/freeze_baseline.py freeze \
        --tag v0.3.0-rN-research-baseline --commit <sha>

then tag the same commit (`git tag v0.3.0-rN-research-baseline <sha>`) and append
an entry here.
