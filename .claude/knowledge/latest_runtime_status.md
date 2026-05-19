# Latest Runtime Status

Updated: 2026-05-19 09:30 Beijing time.

## Runner state

- Personal runtime plan: 4 planned, 4 running.
- Competition runtime plan: 3 planned, 3 running.
- Live market data refresh: fresh.
- Strategy readiness checks: passing for registered strategies.

## Account state

- Personal profile: exchange positions are flat.
- Competition/live profile: exchange has two open swap positions:
  - `APT-USDT-SWAP` long, protected by live reduce-only OCO.
  - `ETH-USDT-SWAP` short, protected by live reduce-only OCO.

## Ownership state

The system is intentionally reporting ownership mismatch:

- Personal ownership journal contains historical APT/ETH entries, but the personal exchange account is flat.
- Competition exchange account contains APT/ETH positions, but competition ownership journal has no matching owner.

This is not a normal state. Do not suppress or auto-adopt these positions without an explicit operational decision. The mismatch should block treating the environment as clean until the live positions are closed or ownership is repaired with a reviewed accounting event.

## Fixes applied

- Kit subprocess calls now strip `OKX_*` credential environment variables for non-live profiles so a named profile cannot be silently hijacked by inherited live credentials.
- Ownership reconcile scheduler now passes the explicit OKX profile for each environment.
- Ownership reconcile now retries transient OKX private endpoint reads before writing failure status.
- Protective OCO stop-loss CLI arguments now use `--slOrdPx=-1` to avoid the OKX CLI ambiguous-option parser failure.

