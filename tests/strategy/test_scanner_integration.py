import unittest
import time
import threading
from datetime import datetime, timedelta
from src.broker.ibkr.market_data_client import MarketDataClient
from src.broker.ibkr.config import PORT
from src.strategy.bull_put.scanner import BullPutScanner
from src.strategy.bull_put.expected_move import ExpectedMove


class TestBullPutScannerIntegration(unittest.TestCase):
    """
    Integration test for BullPutScanner against live IBKR Gateway.

    Verifies:
      - IV is fetchable from IBKR (works 24/7)
      - 2σ lower bound is computed correctly from live IV
      - Scanner finds and prices a qualifying spread (market hours only)

    All assertions are soft — results are printed for manual inspection.
    No orders are placed.
    """

    def setUp(self):
        self.client = MarketDataClient(host="127.0.0.1", port=PORT, client_id=97)
        self.client.connect(self.client.host, self.client.port, clientId=self.client.client_id)

        self.client_thread = threading.Thread(target=self.client.run, daemon=True)
        self.client_thread.start()
        time.sleep(3)

        self.symbol  = "SPY"
        self.scanner = BullPutScanner(self.client, self.symbol)

        # Prefetch option chain
        self.strikes  = []
        self.expiries = []
        self._prefetch_chain()

    def tearDown(self):
        self.client.disconnect()
        self.client_thread.join(timeout=2)

    # -------------------------------------------------------------------------
    # Prefetch (reuse pattern from broker integration tests)
    # -------------------------------------------------------------------------

    def _prefetch_chain(self):
        req_id = self.client.get_new_req_id()
        print(f"\n⏳ Pre-fetching {self.symbol} option chain (Req: {req_id})...")
        self.client.fetch_option_chain(req_id=req_id, symbol=self.symbol)

        for _ in range(20):
            time.sleep(1)
            chain = self.client.market_data.get(req_id, {})
            if chain.get("strikes") and chain.get("expirations"):
                self.strikes  = sorted(chain["strikes"])
                self.expiries = sorted(chain["expirations"])
                print(f"✅ Chain ready: {len(self.strikes)} strikes, {len(self.expiries)} expiries")
                return

        print("⚠️  Chain prefetch timed out — tests will likely skip.")

    def _get_spy_spot(self) -> float:
        """Fetches SPY last close via historical bars."""
        req_id = self.client.get_new_req_id()
        self.client.fetch_historical_bars(req_id=req_id, symbol=self.symbol,
                                          duration="1 D", bar_size="1 hour")
        for _ in range(8):
            time.sleep(1)
            if self.client.market_data.get(req_id):
                break
        bars = self.client.market_data.get(req_id, [])
        return bars[-1]["close"] if bars else 0.0

    # -------------------------------------------------------------------------
    # Test 1: IV fetch
    # -------------------------------------------------------------------------

    def test_1_iv_fetch(self):
        """
        Verifies IV is fetchable via VIX historical bars (24/7).
        VIX close / 100 = SPY annualised implied volatility decimal.
        Hard assert — IV is fundamental to the strategy.
        """
        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot price.")

        print(f"\n📌 SPY Spot: ${spot:.2f}")

        iv = self.scanner._fetch_iv(spot)

        print(f"\n📐 IV Result:")
        if iv:
            print(f"   IV (decimal) : {iv:.4f}")
            print(f"   IV (%)       : {iv*100:.2f}%")
            print(f"   Reasonable?  : {'✅ Yes' if 0.01 < iv < 2.0 else '⚠️  Suspicious'}")
        else:
            print(f"   ⚠️  IV not received — market may be closed or subscription inactive")

        # Hard assert — if we can't get IV the strategy cannot function
        self.assertIsNotNone(iv, "IV fetch returned None — check market data subscription.")
        self.assertGreater(iv, 0.01, "IV suspiciously low.")
        self.assertLess(iv, 2.0,    "IV suspiciously high.")

    # -------------------------------------------------------------------------
    # Test 2: 2σ expected move calculation from live IV
    # -------------------------------------------------------------------------

    def test_2_expected_move_from_live_iv(self):
        """
        Fetches live IV, computes 2σ expected move, prints summary for all
        near-term expiries. Cross-reference these against Perspicium/OptionCharts.
        """
        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot price.")

        iv = self.scanner._fetch_iv(spot)
        if iv is None:
            self.skipTest("Could not fetch IV.")

        em = ExpectedMove(spot=spot, iv=iv)

        today    = datetime.today().date()
        # Print 2σ summary for next 10 expiries that are at least 1 day out
        upcoming = [
            e for e in self.expiries
            if (datetime.strptime(e, "%Y%m%d").date() - today).days >= 1
        ][:10]

        print(f"\n📐 2σ Expected Move Summary — {self.symbol} @ ${spot:.2f} | IV {iv*100:.1f}%")
        print(f"   {'Expiry':<12} {'DTE':>4}  {'1σ ($)':>8}  {'2σ ($)':>8}  "
              f"{'2σ Lower':>10}  {'2σ Upper':>10}")
        print(f"   {'-'*60}")

        for expiry in upcoming:
            dte = (datetime.strptime(expiry, "%Y%m%d").date() - today).days
            s   = em.summary(dte)
            print(f"   {expiry:<12} {dte:>4}  ${s['one_sigma']:>7.2f}  ${s['two_sigma']:>7.2f}  "
                  f"${s['lower_bound']:>9.2f}  ${s['upper_bound']:>9.2f}")

        # Soft assert — just verify the table populated
        self.assertGreater(len(upcoming), 0, "No upcoming expiries found.")

    # -------------------------------------------------------------------------
    # Test 3: Full scanner — find qualifying Bull PUT spread
    # -------------------------------------------------------------------------

    def test_3_find_bull_put_spread(self):
        """
        Runs the full scanner to find the shortest-DTE Bull PUT spread
        with net credit >= $0.50. Prints result for manual inspection.

        Outside market hours: legs cannot be priced — scanner will report
        no qualifying spread. This is expected behavior, not a failure.
        """
        if not self.strikes or not self.expiries:
            self.skipTest("Option chain not available.")

        spot = self._get_spy_spot()
        if spot == 0.0:
            self.skipTest("Could not fetch SPY spot price.")

        print(f"\n🔍 Running Bull PUT Scanner | {self.symbol} @ ${spot:.2f}")

        spread = self.scanner.find_spread(
            spot=spot,
            strikes=self.strikes,
            expiries=self.expiries,
        )

        print(f"\n{'='*60}")
        if spread:
            print(f"  ✅ QUALIFYING SPREAD FOUND")
            print(f"{'='*60}")
            print(f"  Expiry      : {spread['expiry']} (DTE {spread['dte']})")
            print(f"  IV          : {spread['iv']*100:.1f}%")
            print(f"  2σ bound    : ${spread['lower_bound']:.2f}")
            print(f"  Short PUT   : ${spread['short_strike']:.0f}")
            print(f"  Long  PUT   : ${spread['long_strike']:.0f}")
            print(f"  Short bid   : ${spread['short_bid']:.4f}")
            print(f"  Long ask    : ${spread['long_ask']:.4f}")
            print(f"  Net credit  : ${spread['net_credit']:.4f}")
            print(f"  Max profit  : ${spread['net_credit']*100:.2f} per contract")
            print(f"  Max loss    : ${(10 - spread['net_credit'])*100:.2f} per contract")
            print(f"  Exit at     : $0.15 → keep ${(spread['net_credit']-0.15)*100:.2f} "
                  f"({((spread['net_credit']-0.15)/spread['net_credit']*100):.0f}% gross)")
            print(f"\n  ⚠️  Manual verification:")
            print(f"  → Perspicium : https://perspicium.com/ticker/SPY/expected_move")
            print(f"  → OptionCharts: https://optioncharts.io/options/SPY/expected-move")
        else:
            print(f"  ℹ️  No qualifying spread found.")
            print(f"  This is expected outside market hours when live pricing is unavailable.")
        print(f"{'='*60}")

        # Soft assert — just log, don't fail
        if spread:
            self.assertGreaterEqual(spread["net_credit"], 0.50)
            self.assertEqual(spread["short_strike"] - spread["long_strike"], 10.0)


if __name__ == "__main__":
    unittest.main()
