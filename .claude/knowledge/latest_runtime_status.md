# Latest Runtime Status

Updated: 2026-05-19 11:22 Beijing time.

## Runner state

- Personal runtime plan: 4 planned, 4 running after user restart.
- Competition runtime plan: 3 planned, 3 running after user restart.
- Live market data refresh: fresh.
- Strategy readiness checks: passing for registered strategies.

## Account state

- Personal profile: exchange positions are flat.
- Competition/live profile: one owned `ETH-USDT-SWAP` long position, `0.04`
  contracts, owned by `trend_pullback_reversal_quality_top20_v1`.
- Personal profile ordinary open orders are empty.
- Competition/live ordinary open orders are empty for `ETH-USDT-SWAP`.
- Competition/live has a live reduce-only protective stop algo for
  `ETH-USDT-SWAP`, size `0.04`, trigger `2093.31`.

## Ownership state

Ownership reconciliation is clean after reviewed append-only repair:

- Personal ownership journal now has `external_exit` repair events for the historical
  `APT-USDT-SWAP` and `ETH-USDT-SWAP` entries after both exchange profiles were
  verified flat.
- Personal reconcile: `ok=true`, owned positions `0`, exchange positions `0`.
- Competition reconcile: `ok=true`, owned positions `1`, exchange positions `1`.

## Fixes applied

- Kit subprocess calls now strip `OKX_*` credential environment variables for non-live profiles so a named profile cannot be silently hijacked by inherited live credentials.
- Ownership reconcile scheduler now passes the explicit OKX profile for each environment.
- Ownership reconcile now retries transient OKX private endpoint reads before writing failure status.
- Protective OCO stop-loss CLI arguments now use `--slOrdPx=-1` to avoid the OKX CLI ambiguous-option parser failure.
- KitClient now validates environment/profile mapping before executing a command.
- Live ownership journal now rejects execution receipts whose environment/profile
  do not match the journal path.
- Ownership journal now supports append-only reviewed `external_exit`,
  `adoption`, and `transfer` repair events.
- Added `scripts/repair_live_ownership.py` for audited ownership repairs.
- Legacy Broker CLI calls now strip ambient OKX credentials for non-live profiles.
- Codex OKX account tools, dashboard private reads, and C-Auto truth reports now
  strip ambient OKX credentials for non-live profiles.
- Retired custom competition runners are disabled by default in `engine/main.py`;
  the emergency override only allows demo profile, never live/personal.
- Retired legacy strategy files (`elite_flow`, `yolo_momentum`,
  `yolo_orchestrator`) now fail closed when launched directly unless explicitly
  enabled for demo-only historical use.

## Signal-only migration update

Updated: 2026-05-19 11:21 Beijing time.

- C-Auto v2 micro-live signal and committee selection logic has been extracted
  to `engine/strategies/c_auto_v2_signal.py`; the runner now consumes accepted
  decisions rather than owning the signal-selection algorithm.
- Research sleeve candidate-to-signal and committee submission logic has been
  extracted to `engine/strategies/research_sleeve_signal.py`.
- C-Auto bracket entry placement has been moved to
  `engine/execution/bracket_entry.py`.
- Runner-level direct Kit close execution has been moved to
  `engine/execution/position_close.py`.
- These code changes apply on the next strategy process restart; already-running
  Python processes keep the old in-memory code until restarted.
