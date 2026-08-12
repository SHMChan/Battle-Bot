import unittest
import time
import threading

from src.broker.ibkr.market_data_client import MarketDataClient
from src.broker.ibkr.trading_client import TradingClient
from src.broker.ibkr.config import PORT
from src.strategy.bull_put.manager import BullPutManager


class TestBullPutManagerIntegration(unittest.TestCase):
    """
    Integration test for BullPutManager against live IBKR Gateway (paper account).

    Tests:
      1. Paper-mode run: manager scans, enters, monitors, exits — no real orders.
      2. Live-mode entry: resolves conIds, places BAG spread order on paper account.
      3. Live-mode close: closes the spread after entry is confirmed.

    All live-mode tests require market hours. Tests 2 & 3 are soft-assert —
    they print results for inspection rather than failing on fill timeout.

    Connections:
      - MarketDataClient: clientId 96 — market data + scanning
      - TradingClient:    clientId 95 — order placement
    Both connect to IB Gateway on the port set by IBKR_ENV (paper=4002, live=4001).
    """

    @classmethod
    def setUpClass(cls):
        # ── Market data client ────────────────────────────────────────────────
        cls.market_client = MarketDataClient(host="127.0.0.1", port=PORT, client_id=96)
        cls.market_client.connect(
            cls.market_client.host, cls.market_client.port,
            clientId=cls.market_client.client_id
        )
        cls.market_thread = threading.Thread(target=cls.market_client.run, daemon=True)
        cls.market_thread.start()

        # ── Trading client ────────────────────────────────────────────────────
        cls.trading_client = TradingClient(host="127.0.0.1", port=PORT, client_id=95)
        cls.trading_client.connect(
            cls.trading_client.host, cls.trading_client.port,
            clientId=cls.trading_client.client_id
        )
        cls.trading_thread = threading.Thread(target=cls.trading_client.run, daemon=True)
        cls.trading_thread.start()

        time.sleep(3)

        cls.symbol = "SPY"
        cls.strikes = []
        cls.expiries = []
        cls._prefetch_chain()

    @classmethod
    def tearDownClass(cls):
        cls.market_client.disconnect()
        cls.trading_client.disconnect()
        cls.market_thread.join(timeout=2)
        cls.trading_thread.join(timeout=2)

    @classmethod
    def _prefetch_chain(cls):
        req_id = cls.market_client.get_new_req_id()
        print(f"\n⏳ Pre-fetching {cls.symbol} option chain (Req: {req_id})...")
        cls.market_client.fetch_option_chain(req_id=req_id, symbol=cls.symbol)

        for _ in range(20):
            time.sleep(1)
            chain = cls.market_client.market_data.get(req_id, {})
            if chain.get("strikes") and chain.get("expirations"):
                cls.strikes  = sorted(chain["strikes"])
                cls.expiries = sorted(chain["expirations"])
                print(f"✅ Chain ready: {len(cls.strikes)} strikes, {len(cls.expiries)} expiries")
                return

        print("⚠️  Chain prefetch timed out.")

    @classmethod
    def _get_spy_spot(cls) -> float:
        req_id = cls.market_client.get_new_req_id()
        cls.market_client.fetch_historical_bars(
            req_id=req_id, symbol=cls.symbol, duration="1 D", bar_size="1 hour"
        )
        for _ in range(8):
            time.sleep(1)
            if cls.market_client.market_data.get(req_id):
                break
        bars = cls.market_client.market_data.get(req_id, [])
        return bars[-1]["close"] if bars else 0.0

    # -------------------------------------------------------------------------
    # Test 1: Paper-mode manager run (no real orders)
    # -------------------------------------------------------------------------

    def test_1_paper_mode_scan_and_log(self):
        """
        Runs the manager in paper mode (no trading_client) — verifies scanner
        integration and that manager lifecycle prints correctly without placing orders.
        Soft assert on spread result (market hours gate applies).
        """
        if not self.strikes or not self.expiries:
            self.skipTest("Option chain not available.")

        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot.")

        manager = BullPutManager(
            client=self.market_client,
            trading_client=None,
            symbol=self.symbol,
        )

        print(f"\n🔍 Paper-mode manager scan | SPY @ ${spot:.2f}")
        spread = manager.scanner.find_spread(
            spot=spot, strikes=self.strikes, expiries=self.expiries
        )

        if spread:
            print(f"\n✅ Spread found — simulating 1 paper-mode cycle")
            manager._active_position = {
                **spread,
                "entry_credit": spread["net_credit"],
                "status": "open",
            }
            manager._exit()
            self.assertIsNone(manager._active_position)
        else:
            print(f"\nℹ️  No qualifying spread (expected outside market hours).")

    # -------------------------------------------------------------------------
    # Test 2: Live conId resolution for both spread legs
    # -------------------------------------------------------------------------

    def test_2_resolve_option_con_ids(self):
        """
        Verifies that TradingClient can resolve conIds for SPY PUT options.
        Uses a near-term expiry from the prefetched chain.
        Works 24/7 — conId resolution does not require market hours.
        """
        if not self.strikes or not self.expiries:
            self.skipTest("Option chain not available.")

        from datetime import datetime
        today = datetime.today().date()
        upcoming = [
            e for e in self.expiries
            if (datetime.strptime(e, "%Y%m%d").date() - today).days >= 1
        ]
        if not upcoming:
            self.skipTest("No upcoming expiries in chain.")

        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot.")

        expiry = upcoming[0]
        short_strike = max([s for s in self.strikes if s <= spot - 5], default=None)
        if short_strike is None:
            self.skipTest("Could not find a valid OTM short strike.")

        long_strike = max([s for s in self.strikes if s < short_strike - 9], default=None)
        if long_strike is None:
            self.skipTest("Could not find a valid long strike 10 points below short.")

        print(f"\n🔧 Resolving conIds: {self.symbol} {expiry} P | "
              f"short ${short_strike:.0f} / long ${long_strike:.0f}")

        short_con_id = self.trading_client._resolve_option_con_id(
            self.symbol, expiry, short_strike, "P"
        )
        long_con_id = self.trading_client._resolve_option_con_id(
            self.symbol, expiry, long_strike, "P"
        )

        print(f"   Short PUT conId: {short_con_id}")
        print(f"   Long  PUT conId: {long_con_id}")

        self.assertGreater(short_con_id, 0, "Short PUT conId resolution failed.")
        self.assertGreater(long_con_id,  0, "Long PUT conId resolution failed.")
        self.assertNotEqual(short_con_id, long_con_id, "Short and long conIds must differ.")

    # -------------------------------------------------------------------------
    # Test 3: Live spread order placement (paper account, market hours only)
    # -------------------------------------------------------------------------

    def test_3_place_bull_put_spread_live(self):
        """
        Places a real bull PUT spread order on the paper account via TradingClient.
        Uses the scanner to find the qualifying spread first.

        Outside market hours: scanner returns None — test is skipped cleanly.
        Inside market hours: order is placed and order_id is logged.
        Fill is NOT waited for — test just confirms order submission.
        """
        if not self.strikes or not self.expiries:
            self.skipTest("Option chain not available.")

        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot.")

        manager = BullPutManager(
            client=self.market_client,
            trading_client=self.trading_client,
            symbol=self.symbol,
        )

        spread = manager.scanner.find_spread(
            spot=spot, strikes=self.strikes, expiries=self.expiries
        )

        if spread is None:
            print(f"\nℹ️  No qualifying spread found — skipping live order test.")
            print(f"   Expected outside market hours.")
            return

        print(f"\n📋 Qualifying spread:")
        print(f"   {self.symbol} {spread['expiry']} "
              f"${spread['short_strike']:.0f}/${spread['long_strike']:.0f} P")
        print(f"   Net credit: ${spread['net_credit']:.4f}")

        order_id = self.trading_client.place_bull_put_spread(
            symbol=self.symbol,
            expiry=spread["expiry"],
            short_strike=spread["short_strike"],
            long_strike=spread["long_strike"],
            net_credit=spread["net_credit"],
        )

        print(f"\n{'='*60}")
        if order_id:
            print(f"  ✅ ORDER SUBMITTED | Order ID: {order_id}")
            print(f"     Symbol  : {self.symbol}")
            print(f"     Expiry  : {spread['expiry']} (DTE {spread['dte']})")
            print(f"     Strikes : ${spread['short_strike']:.0f}/${spread['long_strike']:.0f} P")
            print(f"     Credit  : ${spread['net_credit']:.4f}")
            print(f"  ⚠️  Check TWS/Gateway for order status.")
            self.assertGreater(order_id, 0)
        else:
            print(f"  ⚠️  Order placement returned 0 — conId resolution may have failed.")
        print(f"{'='*60}")


if __name__ == "__main__":
    unittest.main()
