# Hermes v2

Hermes v2 is an **Adaptive Quantitative Trading System** currently in its
infrastructure phase.

Its future purpose is to study adaptive selection of quantitative models,
GARCH, Monte Carlo simulation, and risk management, with possible execution
through Binance.

A full Binance READ+WRITE execution path exists (auth → RBAC → risk engine
→ order service → Binance → reconciliation — see
`docs/architecture/trading.md`), but **live order execution is disabled by
default** (`TRADING_ENABLED=false`) and only becomes active after an
operator deliberately enables it, per that document's activation runbook.
