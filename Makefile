# Hyper-BTC 5-Minute Bot — common commands
# Usage: make <target>

.PHONY: help sim live check backtest approve test test-network derive-key

# ── Default: show help ────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Hyper-BTC Bot — available commands"
	@echo "  ────────────────────────────────────────────────────────"
	@echo "  make sim          Paper-trade (simulation mode, safe)"
	@echo "  make live         Start live trading wizard"
	@echo "  make check        Run pre-flight health check"
	@echo "  make backtest     Run historical backtest"
	@echo "  make approve      Check/send USDC approval on Polygon"
	@echo "  make derive-key   Generate Polymarket API credentials"
	@echo "  make test         Run offline test suite"
	@echo "  make test-network Run full test suite (requires network)"
	@echo "  ────────────────────────────────────────────────────────"
	@echo ""

# ── Paper trading (no real money) ─────────────────────────────────────────────
sim:
	python bot.py

# ── Live trading wizard (validates everything first) ──────────────────────────
live:
	python go_live.py

# ── Pre-flight health check ───────────────────────────────────────────────────
check:
	python health_check.py

# ── Historical backtest ───────────────────────────────────────────────────────
backtest:
	python backtest.py

# ── USDC approval (run once before going live) ────────────────────────────────
approve:
	python approve_usdc.py --dry-run
	@echo ""
	@echo "  Dry run complete. To actually send the approval transaction, run:"
	@echo "      python approve_usdc.py"

# ── Derive Polymarket API credentials from private key ────────────────────────
derive-key:
	python bot.py --derive-api-key

# ── Test suite ────────────────────────────────────────────────────────────────
test:
	python -m pytest tests/ -q --tb=short

test-network:
	python -m pytest tests/ -q --tb=short --network
