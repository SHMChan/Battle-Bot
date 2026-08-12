src/
├── broker/ibkr/
│   ├── market_data_client.py   ← IBKR connection, market data, option chain, IV
│   └── trading_client.py       ← ORDER PLACEMENT (BAG combo spreads, conId resolution)
└── strategy/bull_put/
    ├── expected_move.py        ← 2σ calculation from VIX/IV
    ├── scanner.py              ← finds qualifying spread (net credit >= $0.50)
    └── manager.py              ← entry/exit/redeploy loop (wired to TradingClient)

tests/
├── broker/ibkr/
│   └── test_ibkr_option_data_integration.py   ← 4 tests, all green
└── strategy/
    ├── test_expected_move.py        ← 14 unit tests, all green
    ├── test_scanner_integration.py  ← 3 integration tests, Tests 1&2 green
    └── test_manager_integration.py  ← 3 integration tests (paper + live order tests)

## TradingClient capabilities
- place_bull_put_spread()  — BAG combo limit order (SELL spread, collect credit)
- close_bull_put_spread()  — BAG combo limit order (BUY spread, close position)
- _resolve_option_con_id() — reqContractDetails for option conId (cached)
- wait_for_fill()          — blocking fill confirmation with timeout
- contractDetails/End callbacks for option conId resolution
- orderStatus signals fill events

## BullPutManager modes
- Paper mode (trading_client=None): scan + log, no real orders
- Live mode  (trading_client=TradingClient): full BAG order lifecycle with fill wait

## Next steps
- Run test_manager_integration.py Test 2 (conId resolution, works 24/7)
- Run test_manager_integration.py Test 3 during market hours (live spread order)
- Add position reconciliation: on startup, check open_positions vs IBKR portfolio
- Add emergency stop: cancel all open orders + close all positions
