import math
import time
import threading
import unittest
from datetime import datetime, timedelta

from src.broker.ibkr.market_data_client import MarketDataClient
from src.broker.ibkr.config import PORT

FRIDAYS_OUT   = 4     # target expiry = 4 Fridays from today
STRIKE_OFFSET = 10    # spot minus this
STRIKE_STEP   = 5.0  # round down to nearest $5


def nth_friday_from_today(n: int) -> datetime.date:
    """Returns the date of the nth upcoming Friday (n=1 = this coming Friday)."""
    today = datetime.today().date()
    days_until_friday = (4 - today.weekday()) % 7 or 7  # weekday 4 = Friday
    first_friday = today + timedelta(days=days_until_friday)
    return first_friday + timedelta(weeks=n - 1)


class TestPutContractQuery(unittest.TestCase):
    """
    Queries live bid/ask for a single SPY PUT contract:
      - Strike : spot - 10, rounded DOWN to the nearest $5 (e.g. 583 → 570)
      - Expiry : chain expiry nearest to 4 Fridays from today
    Works during market hours only (live bid/ask requires an open market).
    """

    def setUp(self):
        self.client = MarketDataClient(host="127.0.0.1", port=PORT, client_id=94)
        self.client.connect(self.client.host, self.client.port, clientId=self.client.client_id)
        self.thread = threading.Thread(target=self.client.run, daemon=True)
        self.thread.start()
        time.sleep(2)

    def tearDown(self):
        self.client.disconnect()
        self.thread.join(timeout=2)

    def test_spy_put_near_spot(self):
        # ── 1. Fetch SPY spot ─────────────────────────────────────────────────
        req_id = self.client.get_new_req_id()
        self.client.fetch_historical_bars(req_id=req_id, symbol="SPY",
                                          duration="1 D", bar_size="1 hour")
        for _ in range(8):
            time.sleep(1)
            if self.client.market_data.get(req_id):
                break

        bars = self.client.market_data.get(req_id, [])
        if not bars:
            self.skipTest("Could not fetch SPY spot price.")

        spot   = bars[-1]["close"]
        strike = math.floor((spot - STRIKE_OFFSET) / STRIKE_STEP) * STRIKE_STEP

        target_friday = nth_friday_from_today(FRIDAYS_OUT)
        print(f"\n📌 SPY spot    : ${spot:.2f}")
        print(f"   Target strike : ${strike:.0f}  (spot - {STRIKE_OFFSET}, ↓ nearest ${STRIKE_STEP:.0f})")
        print(f"   Target expiry : {target_friday.strftime('%Y%m%d')}  ({FRIDAYS_OUT} Fridays away)")

        # ── 2. Fetch option chain, find expiry nearest to 4th Friday ─────────
        chain_req = self.client.get_new_req_id()
        self.client.fetch_option_chain(req_id=chain_req, symbol="SPY")

        for _ in range(20):
            time.sleep(1)
            chain = self.client.market_data.get(chain_req, {})
            if chain.get("strikes") and chain.get("expirations"):
                break

        chain = self.client.market_data.get(chain_req, {})
        expiries = sorted(chain.get("expirations", []))
        strikes  = sorted(chain.get("strikes", []))

        if not expiries:
            self.skipTest("Option chain not available.")

        if strike not in strikes:
            self.skipTest(f"Strike ${strike:.0f} not in IBKR chain.")

        expiry = min(
            expiries,
            key=lambda e: abs((datetime.strptime(e, "%Y%m%d").date() - target_friday).days),
        )

        today = datetime.today().date()
        dte   = (datetime.strptime(expiry, "%Y%m%d").date() - today).days
        print(f"   Chain expiry  : {expiry}  (DTE {dte})")

        # ── 3. Single reqMktData — captures bid, ask, and open interest ──────
        req_id    = self.client.get_new_req_id()
        bid_event = threading.Event()
        oi_event  = threading.Event()

        self.client._option_price_events = getattr(self.client, "_option_price_events", {})
        self.client._option_price_data   = getattr(self.client, "_option_price_data",   {})
        self.client._option_quote_data   = getattr(self.client, "_option_quote_data",   {})
        self.client._option_oi_data      = getattr(self.client, "_option_oi_data",      {})
        self.client._option_oi_events    = getattr(self.client, "_option_oi_events",    {})

        # Register bid event (tickPrice type 1) and OI event (tickSize type 22)
        self.client._option_price_events[req_id] = {"event": bid_event, "side": "bid"}
        self.client._option_oi_events[req_id]    = oi_event

        contract = self.client._build_option_contract("SPY", expiry, strike, "P")
        contract.exchange = "SMART"

        self.client.reqMktData(req_id, contract, "", False, False, [])

        bid_event.wait(timeout=10)
        oi_event.wait(timeout=10)

        self.client.cancelMktData(req_id)

        quote = self.client._option_quote_data.get(req_id, {})
        bid   = quote.get("bid")
        ask   = quote.get("ask")
        oi    = self.client._option_oi_data.get(req_id)

        # ── 4. Print result ───────────────────────────────────────────────────
        print(f"\n{'='*50}")
        print(f"  SPY PUT Contract")
        print(f"  Strike        : ${strike:.0f}")
        print(f"  Expiry        : {expiry}  (DTE {dte})")
        print(f"  Bid           : {'${:.4f}'.format(bid) if bid is not None else 'N/A'}")
        print(f"  Ask           : {'${:.4f}'.format(ask) if ask is not None else 'N/A'}")
        if bid is not None and ask is not None:
            print(f"  Mid           : ${(bid + ask) / 2:.4f}")
        print(f"  Open Interest : {oi if oi is not None else 'N/A'}")
        print(f"{'='*50}")

        if bid is None and ask is None and oi is None:
            print("ℹ️  No live data — expected outside market hours.")
        else:
            if ask is not None:
                self.assertGreater(ask, 0)
            if oi is not None:
                self.assertGreaterEqual(oi, 0)


if __name__ == "__main__":
    unittest.main()
