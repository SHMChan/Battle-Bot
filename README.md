battle-bot/
│
├── .gitignore
├── README.md
├── requirements.txt
│
├── src/
│   ├── __init__.py
│   └── broker/
│       ├── __init__.py
│       └── ibkr/
│           ├── __init__.py
│           └── trading_client.py   <-- Relocated here
│
└── tests/
    ├── __init__.py
    └── broker/
        └── ibkr/
            ├── __init__.py
            ├── test_trading_client.py  <-- Relocated here
            └── integration_test.py     <-- Relocated here

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