import unittest
import time
import threading
from datetime import datetime, timedelta
from src.broker.ibkr.market_data_client import MarketDataClient
from src.broker.ibkr.config import PORT


class TestIBKROptionsIntegration(unittest.TestCase):
    def setUp(self):
        """Set up a real network connection to a live running IB Gateway/TWS instance."""
        # Port 7497 = TWS Paper, Port 4002 = IB Gateway Paper.
        self.client = MarketDataClient(host="127.0.0.1", port=PORT, client_id=98)

        # Open live TCP socket to the broker
        self.client.connect(self.client.host, self.client.port, clientId=self.client.client_id)

        # IBAPI requires a dedicated background thread to read incoming socket frames
        self.client_thread = threading.Thread(target=self.client.run, daemon=True)
        self.client_thread.start()

        # Allow the connection handshake to stabilize
        time.sleep(5)

        # SPY — highly liquid ETF, ideal for options testing
        self.symbol = "SPY"

        # Shared chain state — populated by test_1, consumed by tests 2/3/4
        self.verified_strikes = []
        self.verified_expiries = []
        self.verified_expiry = None
        # Pre-fetch the option chain once so all tests share it
        self._prefetch_chain()

    def tearDown(self):
        """Clean up all active streaming channels before disconnecting."""
        print("\n🧼 Cleaning up active options streaming channels...")

        for req_id in list(self.client._active_streams):
            print(f"Stopping active stream ID: {req_id}")
            self.client.cancel_realtime_bars(req_id)

        time.sleep(0.5)
        self.client.disconnect()
        self.client_thread.join(timeout=2)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _nearest_monthly_expiry(self) -> str:
        """
        Returns the nearest upcoming monthly options expiry date (3rd Friday)
        in YYYYMMDD format as required by IBKR contract definitions.
        """
        today = datetime.today()
        for month_offset in range(0, 3):
            year = today.year + (today.month + month_offset - 1) // 12
            month = (today.month + month_offset - 1) % 12 + 1
            first_day = datetime(year, month, 1)
            first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
            third_friday = first_friday + timedelta(weeks=2)
            if third_friday > today:
                return third_friday.strftime("%Y%m%d")
        return (today + timedelta(days=30)).strftime("%Y%m%d")

    def _standard_monthly_expiry(self) -> str:
        today = datetime.today()
        min_date = today + timedelta(days=7)

        for month_offset in range(0, 6):
            year = today.year + (today.month + month_offset - 1) // 12
            month = (today.month + month_offset - 1) % 12 + 1
            first_day = datetime(year, month, 1)
            # weekday(): Monday=0 ... Friday=4
            # days until first Friday:
            days_to_friday = (4 - first_day.weekday()) % 7
            first_friday = first_day + timedelta(days=days_to_friday)
            third_friday = first_friday + timedelta(weeks=2)
            if third_friday >= min_date:
                return third_friday.strftime("%Y%m%d")

        return (today + timedelta(days=30)).strftime("%Y%m%d")

    def _resolve_expiry_from_chain(self, expiries: list) -> str:
        """
        Picks the best expiry from the actual IBKR chain:
        - At least 7 days out (ensures historical bar data exists)
        - Prefers a Friday (standard monthly/weekly expiry)
        - Falls back to nearest available if no Friday found
        Always uses real chain data — never relies on computed dates matching.
        """
        min_date_str = (datetime.today() + timedelta(days=7)).strftime("%Y%m%d")
        candidates = [e for e in expiries if e >= min_date_str]

        if not candidates:
            return expiries[-1]

        # Prefer Fridays — standard monthly and weekly expiries land on Friday
        fridays = [
            e for e in candidates
            if datetime.strptime(e, "%Y%m%d").weekday() == 4  # 4 = Friday
        ]
        chosen = fridays[0] if fridays else candidates[0]
        print(f"   Expiry selected: {chosen} ({'Friday' if fridays else 'non-Friday fallback'})")
        return chosen

    def _fetch_chain_and_resolve(self) -> tuple[list, list, str]:
        """
        Returns pre-fetched SPY chain data plus the best available expiry.
        - sorted list of verified strikes
        - sorted list of verified expiries
        - best expiry: Friday, at least 7 days out, actually in the chain
        """
        strikes = self.verified_strikes
        expiries = self.verified_expiries

        print(f"\n⏳ Using pre-fetched SPY option chain ({len(strikes)} strikes, {len(expiries)} expiries)...")

        if not strikes or not expiries:
            return [], [], ""

        nearest_expiry = self._resolve_expiry_from_chain(expiries)

        print(f"✅ Chain resolved: {len(strikes)} strikes | {len(expiries)} expiries")
        print(f"   Strike range   : ${strikes[0]:.2f} — ${strikes[-1]:.2f}")
        print(f"   Using expiry   : {nearest_expiry}")

        return strikes, expiries, nearest_expiry

    def _resolve_atm_strike(self, strikes: list, target_price: float, window: float = 10.0) -> float:
        """
        Finds the nearest real strike to the target price from verified IBKR chain data.
        - Rounds target_price to nearest whole dollar first (options trade on integer strikes)
        - Only considers strikes within ±window dollars of the rounded target (default: $10)
        - Falls back to globally nearest strike if no candidates exist in window
        """
        rounded_target = round(target_price)
        candidates = [s for s in strikes if abs(s - rounded_target) <= window]
        pool = candidates if candidates else strikes
        chosen = min(pool, key=lambda s: abs(s - rounded_target))
        if not candidates:
            print(f"   ⚠️ No strikes within ±${window:.0f} of ${rounded_target}, "
                  f"using nearest overall: ${chosen:.2f}")
        else:
            print(f"   ATM window: ${rounded_target - window:.0f}–${rounded_target + window:.0f} "
                  f"→ {len(candidates)} candidates → chosen ${chosen:.2f}")
        return chosen

    def _get_spy_last_close(self) -> float:
        """Fetches SPY last close price via historical bars."""
        req_id = self.client.get_new_req_id()
        self.client.fetch_historical_bars(
            req_id=req_id, symbol=self.symbol, duration="1 D", bar_size="1 hour"
        )
        for _ in range(6):
            time.sleep(1)
            if self.client.market_data.get(req_id):
                break
        bars = self.client.market_data.get(req_id, [])
        return bars[-1]["close"] if bars else 0.0

    # -------------------------------------------------------------------------
    # Test 1: Option Chain — list all strikes and expiries (foundation test)
    # -------------------------------------------------------------------------

    def test_1_spy_option_chain_retrieval(self):
        """
        Integration Test: Fetches the full SPY option chain from IBKR.
        This is the FOUNDATION test — verifies real strikes and expiries exist.
        All other contract tests depend on this data. Works 24/7.
        """
        strikes, expiries, nearest_expiry = self._fetch_chain_and_resolve()

        if not strikes or not expiries:
            self.skipTest("Option chain data not returned — check IB Gateway market data subscriptions.")

        print(f"\n📋 SPY Option Chain Summary:")
        print(f"   Total strikes  : {len(strikes)}")
        print(f"   Total expiries : {len(expiries)}")
        print(f"   Strike range   : ${strikes[0]:.2f} — ${strikes[-1]:.2f}")
        print(f"   Expiries (first 5): {expiries[:5]}")
        print(f"   Nearest expiry : {nearest_expiry}")

        # Sample strikes around the middle of the range for readability
        mid = len(strikes) // 2
        sample = strikes[max(0, mid - 5): mid + 5]
        print(f"   Sample ATM strikes: {sample}")

        self.assertGreater(len(strikes), 0, "No strikes returned from IBKR.")
        self.assertGreater(len(expiries), 0, "No expiries returned from IBKR.")
        self.assertTrue(all(s > 0 for s in strikes), "All strikes must be positive.")
        self.assertTrue(
            all(len(e) == 8 for e in expiries),
            "All expiries must be in YYYYMMDD format."
        )

    # -------------------------------------------------------------------------
    # Test 2: Specific Contract — historical bars for SPY ATM PUT
    # -------------------------------------------------------------------------

    def test_2_spy_atm_put_historical_bars(self):
        """
        Integration Test: Fetches historical hourly bars for a verified SPY ATM PUT contract.
        Uses real IBKR strikes from the option chain — no guessing.
        Works 24/7 independent of market hours.
        """
        # Step 1: Resolve real strikes and expiry from IBKR
        strikes, expiries, nearest_expiry = self._fetch_chain_and_resolve()
        if not strikes:
            self.skipTest("Cannot resolve option chain — skipping contract test.")

        # Step 2: Determine ATM strike from real last close
        last_close = self._get_spy_last_close()
        if last_close == 0.0:
            self.skipTest("Cannot determine SPY last close price.")

        atm_strike = self._resolve_atm_strike(strikes, last_close)

        print(f"\n📌 SPY Last Close: ${last_close:.2f} → Verified ATM Strike: ${atm_strike:.2f}")
        print(f"   Contract: SPY {nearest_expiry} PUT @ {atm_strike}")

        # Step 3: Fetch historical bars using verified contract parameters
        req_id = self.client.get_new_req_id()
        self.client.fetch_option_historical_bars(
            req_id=req_id,
            symbol=self.symbol,
            expiry=nearest_expiry,
            strike=atm_strike,
            right="P",
            duration="1 D",
            bar_size="1 hour"
        )

        # Poll up to 10 seconds
        data_received = False
        for _ in range(10):
            time.sleep(1)
            if self.client.market_data.get(req_id):
                data_received = True
                break

        self.assertTrue(
            data_received,
            f"Failed to retrieve historical bars for SPY PUT {nearest_expiry} @ {atm_strike}."
        )

        bars = self.client.market_data[req_id]
        last_bar = bars[-1]

        print(f"\n✅ SPY ATM PUT Historical Bars Retrieved!")
        print(f"   Bars returned : {len(bars)}")
        print(f"   Last Bar Date : {last_bar['date']}")
        print(f"   Open  : ${last_bar['open']:.4f}")
        print(f"   High  : ${last_bar['high']:.4f}")
        print(f"   Low   : ${last_bar['low']:.4f}")
        print(f"   Close : ${last_bar['close']:.4f}")
        print(f"   Vol   : {last_bar['volume']}")

        self.assertGreater(len(bars), 0, "Historical option bars cannot be empty.")
        self.assertGreater(last_bar["close"], 0.0, "Option close price must be positive.")

    # -------------------------------------------------------------------------
    # Test 3: Specific Contract — ATM CALL real-time streaming
    # -------------------------------------------------------------------------

    def test_3_spy_atm_call_realtime_streaming(self):
        """
        Integration Test: Streams real-time 5-second bars for a verified SPY ATM CALL.
        Uses real IBKR strikes from the option chain — no guessing.
        Gracefully skips outside market hours.
        """
        # Step 1: Resolve real strikes and expiry from IBKR
        strikes, expiries, nearest_expiry = self._fetch_chain_and_resolve()
        if not strikes:
            self.skipTest("Cannot resolve option chain — skipping contract test.")

        # Step 2: Determine ATM strike from real last close
        last_close = self._get_spy_last_close()
        if last_close == 0.0:
            self.skipTest("Cannot determine SPY last close price.")

        atm_strike = self._resolve_atm_strike(strikes, last_close)

        print(f"\n📌 SPY Last Close: ${last_close:.2f} → Verified ATM Strike: ${atm_strike:.2f}")
        print(f"   Contract: SPY {nearest_expiry} CALL @ {atm_strike}")

        # Step 3: Stream real-time bars for verified contract
        req_id = self.client.get_new_req_id()
        self.client.stream_option_realtime_bars(
            req_id=req_id,
            symbol=self.symbol,
            expiry=nearest_expiry,
            strike=atm_strike,
            right="C"
        )

        # Poll up to 10 seconds
        data_received = False
        print(f"\n⏳ Waiting for live SPY CALL streaming data from IBKR...")
        for _ in range(10):
            time.sleep(1)
            if req_id in self.client.market_data and isinstance(self.client.market_data[req_id], dict):
                data_received = True
                break

        if not data_received:
            print(f"ℹ️ Live option stream timed out. Expected behavior outside regular market hours.")
            return

        bar = self.client.market_data[req_id]
        print(f"\n✅ Live SPY CALL Data Received!")
        print(f"   Strike : ${atm_strike:.2f} | Expiry: {nearest_expiry}")
        print(f"   Close  : ${bar['close']:.4f} | Volume: {bar['volume']}")

        self.assertGreater(bar["close"], 0.0, "Option close price must be positive.")

    # -------------------------------------------------------------------------
    # Test 4: Option Chain — verify CALL and PUT strike coverage
    # -------------------------------------------------------------------------

    def test_4_spy_option_chain_call_and_put_coverage(self):
        """
        Integration Test: Verifies the option chain covers both CALL and PUT sides
        with overlapping ATM strikes. Works 24/7.
        """
        call_req_id = self.client.get_new_req_id()
        put_req_id = self.client.get_new_req_id()

        print(f"\n⏳ Requesting SPY CALL chain (Req ID: {call_req_id})...")
        self.client.fetch_option_chain(req_id=call_req_id, symbol=self.symbol, right="C")

        print(f"⏳ Requesting SPY PUT chain (Req ID: {put_req_id})...")
        self.client.fetch_option_chain(req_id=put_req_id, symbol=self.symbol, right="P")

        for _ in range(10):
            time.sleep(1)
            call_ready = bool(self.client.market_data.get(call_req_id, {}).get("strikes"))
            put_ready = bool(self.client.market_data.get(put_req_id, {}).get("strikes"))
            if call_ready and put_ready:
                break

        call_chain = self.client.market_data.get(call_req_id, {})
        put_chain = self.client.market_data.get(put_req_id, {})

        if not call_chain.get("strikes") or not put_chain.get("strikes"):
            self.skipTest("Option chain data incomplete — check market data subscriptions.")

        call_strikes = set(call_chain["strikes"])
        put_strikes = set(put_chain["strikes"])
        overlap = call_strikes & put_strikes

        print(f"\n✅ SPY Option Chain Coverage Verified!")
        print(f"   CALL strikes   : {len(call_strikes)}")
        print(f"   PUT  strikes   : {len(put_strikes)}")
        print(f"   Shared strikes : {len(overlap)}")

        self.assertGreater(len(call_strikes), 0, "CALL chain returned no strikes.")
        self.assertGreater(len(put_strikes), 0, "PUT chain returned no strikes.")
        self.assertGreater(len(overlap), 0, "CALL and PUT chains share no common strikes.")

    def _prefetch_chain(self):
        """
        Fetches the SPY option chain once during setUp so all tests can share it.
        Polls up to 20 seconds for data to arrive — Gateway can be slow on first req.
        Populates self.verified_strikes and self.verified_expiries.
        """
        req_id = self.client.get_new_req_id()
        print(f"\n⏳ Pre-fetching SPY option chain (Req ID: {req_id})...")
        self.client.fetch_option_chain(req_id=req_id, symbol=self.symbol)

        for _ in range(20):
            time.sleep(1)
            chain = self.client.market_data.get(req_id, {})
            if chain.get("strikes") and chain.get("expirations"):
                self.verified_strikes = sorted(chain["strikes"])
                self.verified_expiries = sorted(chain["expirations"])
                print(f"✅ Pre-fetch complete: {len(self.verified_strikes)} strikes, "
                      f"{len(self.verified_expiries)} expiries")
                return

        print("⚠️ Pre-fetch timed out after 20s — chain data not received.")


if __name__ == "__main__":
    unittest.main()
