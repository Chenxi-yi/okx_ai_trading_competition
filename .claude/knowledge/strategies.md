# Strategies Knowledge Base

## Active Strategy
| ID | Name |
|---|---|
| `elite_flow` | **Elite Flow** — tick multi-level OFI + crowding + regime gate |

## Index
`.claude/knowledge/strategies/_index.md` — status, CLI commands

## How Claude Should Use These Files
- Before modifying strategy code → read `competition/strategies/elite_flow.py`
- Signal formulas: multi-level OFI + crowding + regime gate → conviction state machine
- Parameters in `config/competition_strategies.json` under `elite_flow_config`
- After any parameter change → update `competition_strategies.json`
