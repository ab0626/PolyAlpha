# NFP #1 analysis template (preregistered inspection)

Status: FROZEN preregistration of the inspection, NOT the result. NFP #1 is
instrument validation, never alpha evidence. Any modification to this template
or the analysis script changes the preregistration hash and must be re-frozen.

## What the report produces (exactly these views, nothing else)

1. coverage / instrument health (from `check_release_coverage.py`)
2. raw provenance (`raw_store_root_hash`, window, market ids, segment hash)
3. `R(h)` at the existing frozen horizons (5s/10s/30s/60s/300s + stable)
4. `t10, t25, t50, t75, t90`
5. `W_quote`, `W_fill`
6. `Z_lag` ONLY where the existing propagation support gates permit

No new category slices, thresholds, event windows, alternative estimators,
"interesting" charts, or post-event parameter choices. Unsupported quantities
render as `INSUFFICIENT_DATA` / `UNRESOLVABLE` — they never disappear.

## Rules

- Read-only: consumes only already-frozen outputs and frozen functions
  (`event_shock.measure_shock_reaction`, `information.incorporation_curve`,
  `integration.raw_provenance`, `integration.compute_propagation_evidence`).
- NFP #1 cannot conclude "strategy passed". Its only possible conclusion is
  `INSTRUMENT_VALIDATION_OK` / `INSTRUMENT_VALIDATION_FAILED` / `INSUFFICIENT_DATA`.
- `Z_lag` for NFP #1 will be `INSUFFICIENT_DATA`: no historical propagation
  samples exist, so the support gate (min events/clusters) cannot pass. That is
  correct, not a failure.

## Manifest

`config/analysis_template.json` records the template version, the analysis
script sha256, the frozen methodology/info-propagation hashes, the collection
implementation commit, expected outputs, and created_at. The report embeds the
manifest so the inspection's drift is provable.
