battle-bot/
│
├── .gitignore
├── README.md
├── requirements.txt
│
├── src/
    ├── broker/
│       └── ibkr/
│           ├── market_data_client.py   ✅ done
│           └── trading_client.py       ← order execution (new)
└── strategy/
    └── bull_put/
        ├── __init__.py
        ├── expected_move.py        ← 2σ calculation
        ├── scanner.py              ← finds qualifying spread
        └── manager.py             ← entry/exit/redeploy loop
│
└── tests/
    ├── __init__.py
    └── broker/
        └── ibkr/
            ├── __init__.py
            ├── test_trading_client.py  <-- Relocated here
            └── integration_test.py     <-- Relocated here
    └── strategy/
        └── bull_put/
            ├── test_expected_move.py
            ├── test_scanner.py
            └── integration_test.py

## 🔄 Last Session Handoff
~~**June 7, 2026 — M4 Max restart**
- Options integration test prefetch returning 0 strikes/expiries
- Fix: increase setUp sleep to 3s, _prefetch_chain poll to range(20)
- Tests 1, 3, 4 passing. Test 2 still failing.
- Next: confirm all 4 green, then move to trading logic~~

## 🔄 Last Session Handoff
**June 7, 2026 — All 4 integration tests GREEN**
- Prefetch chain in setUp works (499 strikes, 36 expiries)
- tradingClass cache resolves SPY vs SPYW correctly
- ATM strike: rounded to nearest dollar, ±$10 window filter
- Historical bars use SMART exchange (CBOE rejects weeklies)
- Test 3 streams gracefully skip outside market hours (paper account, expected)
- Next: move to trading logic

## 🔄 Last Session Handoff
**June 20, 2026 — Bull PUT Strategy Foundation**

### Completed
- `src/strategy/bull_put/expected_move.py` — 2σ calc from IV, 14 unit tests green
- `src/strategy/bull_put/scanner.py` — scans expiries for net credit >= $0.50
- `src/strategy/bull_put/manager.py` — entry/exit/redeploy loop (order stubs, not wired yet)
- `tests/strategy/bull_put/test_expected_move.py` — 14/14 passing
- `tests/strategy/bull_put/test_scanner_integration.py` — Tests 1 & 2 passing

### Key Decisions
- IV source: VIX historical bars (24/7, correct source — VIX/100 = SPY annualised IV)
- Spread width: $10 (short PUT - long PUT)
- Entry threshold: net credit >= $0.50
- Exit threshold: net price <= $0.15 (keep $0.35, ~70% gross)
- Strike filter: ±$10 window from spot, rounded to nearest dollar
- Exchange: CBOE for real-time, SMART for historical bars

### Known Issues
- Test 3 (scanner integration) times out outside market hours — leg pricing
  via reqMktData only works during market hours. Each expiry waits full
  IV_TIMEOUT (10s) before giving up — 33 expiries = very slow outside hours
- tradingClass cache fix is in market_data_client_latest.py — confirm it
  replaced src/broker/ibkr/market_data_client.py in project

### Next Session
- Add market hours check to scanner.find_spread() to fail fast when closed
- Cap expiry scan to first N expiries (e.g. DTE <= 30) — no need to scan all 33
- Review Test 3 full output from today's run to tune entry parameters
- Build trading_client.py — actual order placement (place_bull_put_spread,
  close_bull_put_spread)
- Wire manager.py TODOs to trading_client