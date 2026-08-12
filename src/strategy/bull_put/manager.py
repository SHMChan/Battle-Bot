import time
import logging
import threading
from typing import Optional

from src.strategy.bull_put.scanner import BullPutScanner

logger = logging.getLogger(__name__)

# Exit threshold — buy back spread when net price drops to this level
EXIT_THRESHOLD = 0.15

# How often to check position value during monitoring (seconds)
MONITOR_INTERVAL = 30

# Max number of redeploy cycles per session (safety guard)
MAX_CYCLES = 10


class BullPutManager:
    """
    Manages the full Bull PUT spread lifecycle:

    1. SCAN   — find shortest-DTE spread with net credit >= $0.50
    2. ENTER  — sell the spread (short PUT + long PUT)
    3. MONITOR — poll net price every 30 seconds
    4. EXIT   — buy back spread when net price <= $0.15
    5. REDEPLOY — repeat from step 1

    Each cycle targets ~70% gross profit ($0.35 kept from $0.50 collected).
    """

    def __init__(self, client, trading_client=None, symbol: str = "SPY"):
        """
        Args:
            client:         Connected MarketDataClient instance (market data + scanning)
            trading_client: Connected TradingClient instance for order placement.
                            Pass None to run in paper/log-only mode (no real orders).
            symbol:         Underlying symbol (default SPY)
        """
        self.client         = client
        self.trading_client = trading_client
        self.symbol         = symbol
        self.scanner        = BullPutScanner(client, symbol)

        self._active_position: Optional[dict] = None
        self._cycle_count = 0
        self._running = False

    # =========================================================================
    # Public API
    # =========================================================================

    def run(self, spot: float, strikes: list, expiries: list):
        """
        Starts the Bull PUT strategy loop.
        Runs until MAX_CYCLES reached or stop() is called.

        Args:
            spot:     Current SPY price
            strikes:  Verified strike list from prefetch
            expiries: Sorted expiry list from prefetch
        """
        self._running = True
        print(f"\n🚀 Bull PUT Manager started | Symbol: {self.symbol} | "
              f"Max cycles: {MAX_CYCLES}")

        while self._running and self._cycle_count < MAX_CYCLES:
            self._cycle_count += 1
            print(f"\n{'='*60}")
            print(f"  CYCLE {self._cycle_count} of {MAX_CYCLES}")
            print(f"{'='*60}")

            # Step 1: Scan for qualifying spread
            spread = self.scanner.find_spread(spot, strikes, expiries)
            if spread is None:
                print("⚠️  No qualifying spread found — stopping.")
                break

            # Step 2: Enter position
            entered = self._enter(spread)
            if not entered:
                print("⚠️  Entry failed — stopping.")
                break

            # Step 3: Monitor until exit condition
            self._monitor()

            # Step 4: Exit position
            self._exit()

            print(f"\n✅ Cycle {self._cycle_count} complete. "
                  f"Net kept: ~${spread['net_credit'] - EXIT_THRESHOLD:.2f} per contract")

        print(f"\n🏁 Bull PUT Manager stopped after {self._cycle_count} cycle(s).")
        self._running = False

    def stop(self):
        """Signals the manager to stop after the current cycle completes."""
        print("\n🛑 Stop signal received — will exit after current cycle.")
        self._running = False

    # =========================================================================
    # Position Lifecycle
    # =========================================================================

    def _enter(self, spread: dict) -> bool:
        """
        Places the Bull PUT spread order.
        Sells short PUT, buys long PUT as a combo order.

        In paper trading mode this logs the order details.
        Wire up to TradingClient.place_bull_put_spread() when ready.
        """
        self._active_position = {
            **spread,
            "entry_credit": spread["net_credit"],
            "status": "open",
        }

        print(f"\n📥 ENTERING POSITION")
        print(f"   Symbol      : {self.symbol}")
        print(f"   Expiry      : {spread['expiry']} (DTE {spread['dte']})")
        print(f"   Short PUT   : ${spread['short_strike']:.0f} @ bid ${spread['short_bid']:.4f}")
        print(f"   Long  PUT   : ${spread['long_strike']:.0f} @ ask ${spread['long_ask']:.4f}")
        print(f"   Net Credit  : ${spread['net_credit']:.4f}")
        print(f"   Max Profit  : ${spread['net_credit']:.4f} × 100 = ${spread['net_credit']*100:.2f}")
        print(f"   Max Loss    : ${10 - spread['net_credit']:.4f} × 100 = ${(10 - spread['net_credit'])*100:.2f}")
        print(f"   Exit target : ${EXIT_THRESHOLD:.2f} (keep ${spread['net_credit'] - EXIT_THRESHOLD:.2f})")

        if self.trading_client is not None:
            order_id = self.trading_client.place_bull_put_spread(
                symbol=self.symbol,
                expiry=spread["expiry"],
                short_strike=spread["short_strike"],
                long_strike=spread["long_strike"],
                net_credit=spread["net_credit"],
            )
            if not order_id:
                print("⚠️  Spread order placement failed (could not resolve conIds).")
                return False

            self._active_position["entry_order_id"] = order_id
            print(f"   ⏳ Waiting for fill on order #{order_id}...")
            filled = self.trading_client.wait_for_fill(order_id, timeout=60.0)
            if not filled:
                print(f"   ⚠️  Order #{order_id} did not fill within 60s — check TWS.")
        else:
            print("   ℹ️  Paper mode — no order placed.")

        return True

    def _monitor(self):
        """
        Polls the net value of the open spread at MONITOR_INTERVAL seconds.
        Blocks until exit condition is met (net price <= EXIT_THRESHOLD).
        """
        if self._active_position is None:
            return

        spread = self._active_position
        print(f"\n👁  MONITORING position — checking every {MONITOR_INTERVAL}s")
        print(f"   Exit when net price ≤ ${EXIT_THRESHOLD:.2f}")

        while self._running:
            net_price = self._fetch_spread_price(spread)

            if net_price is None:
                print(f"   ⚠️  Could not fetch spread price — retrying...")
                time.sleep(MONITOR_INTERVAL)
                continue

            profit_so_far = round(spread["entry_credit"] - net_price, 4)
            print(f"   💰 Spread net price: ${net_price:.4f} | "
                  f"P&L: ${profit_so_far:.4f} | "
                  f"Exit at: ${EXIT_THRESHOLD:.2f}")

            if net_price <= EXIT_THRESHOLD:
                print(f"   ✅ Exit condition met — net price ${net_price:.4f} ≤ ${EXIT_THRESHOLD:.2f}")
                break

            time.sleep(MONITOR_INTERVAL)

    def _exit(self):
        """
        Closes the Bull PUT spread by buying back the position.
        Logs P&L for the cycle.
        """
        if self._active_position is None:
            return

        spread = self._active_position
        net_price = self._fetch_spread_price(spread) or EXIT_THRESHOLD

        profit = round(spread["entry_credit"] - net_price, 4)
        profit_pct = round((profit / spread["entry_credit"]) * 100, 1)

        print(f"\n📤 EXITING POSITION")
        print(f"   Buy back at : ${net_price:.4f}")
        print(f"   Collected   : ${spread['entry_credit']:.4f}")
        print(f"   Kept        : ${profit:.4f} ({profit_pct}% gross)")

        if self.trading_client is not None:
            order_id = self.trading_client.close_bull_put_spread(
                symbol=self.symbol,
                expiry=spread["expiry"],
                short_strike=spread["short_strike"],
                long_strike=spread["long_strike"],
                net_debit=net_price,
            )
            if order_id:
                print(f"   ⏳ Waiting for close fill on order #{order_id}...")
                self.trading_client.wait_for_fill(order_id, timeout=60.0)
        else:
            print("   ℹ️  Paper mode — no close order placed.")

        self._active_position = None

    # =========================================================================
    # Spread Pricing
    # =========================================================================

    def _fetch_spread_price(self, spread: dict) -> Optional[float]:
        """
        Fetches current net value of the spread:
            net = short PUT ask (cost to buy back) - long PUT bid (proceeds from selling)

        This is what it costs to close the position — we exit when this drops to $0.15.
        """
        short_ask = self.scanner._fetch_option_ask(
            spread["expiry"], spread["short_strike"], "P"
        )
        long_bid = self.scanner._fetch_option_bid(
            spread["expiry"], spread["long_strike"], "P"
        )

        if short_ask is None or long_bid is None:
            return None

        return round(short_ask - long_bid, 4)
