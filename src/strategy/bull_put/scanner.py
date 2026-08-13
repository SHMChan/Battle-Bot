import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.strategy.bull_put.expected_move import ExpectedMove

logger = logging.getLogger(__name__)

# Minimum net credit to qualify a spread for entry
MIN_NET_CREDIT = 0.50

# Spread width in dollars (short strike - long strike)
SPREAD_WIDTH = 10.0

# IV tick type for reqMktData
TICK_IV = 106  # 106 = impvolat for STK (tick 24 is futures/options only)

# Seconds to wait for IV/option price data
IV_TIMEOUT = 10

# DTE scan window — skip illiquid near-term and far-dated contracts
MIN_DTE = 20
MAX_DTE = 120

# Sigma levels to check per expiry, from deepest OTM to shallowest.
# Scanner returns the first (most OTM) qualifying spread found.
SIGMA_SCAN_LEVELS = [2.2, 2.0, 1.8, 1.5, 1.2]

# Short strikes must land on this dollar increment (e.g. 700, 705, 710)
STRIKE_INCREMENT = 5.0

# US Eastern timezone — IBKR/NYSE market hours reference
ET = ZoneInfo("America/New_York")

# Regular market hours (Eastern)
MARKET_OPEN  = (9, 30)   # 09:30 ET
MARKET_CLOSE = (16, 0)   # 16:00 ET


class BullPutScanner:
    """
    Scans SPY option expiries from shortest DTE outward to find the first
    Bull PUT spread where:
      - Short PUT = nearest strike at or below the 2σ lower bound
      - Long  PUT = short PUT strike - $10
      - Net credit (short bid - long ask) >= $0.50

    Uses IBKR live market data for IV and option pricing.
    Leg pricing requires market hours — scanner bails fast when closed.
    """

    def __init__(self, client, symbol: str = "SPY"):
        """
        Args:
            client: Connected MarketDataClient instance
            symbol: Underlying symbol (default SPY)
        """
        self.client = client
        self.symbol = symbol

    # =========================================================================
    # Public API
    # =========================================================================

    def find_spread(self, spot: float, strikes: list, expiries: list) -> Optional[dict]:
        """
        Scans expiries from shortest DTE outward. Returns the first spread
        that meets the net credit threshold, or None if none qualify.

        Only scans expiries within MAX_DTE days.
        Bails immediately if market is closed — live pricing unavailable.

        Args:
            spot:     Current SPY price
            strikes:  Verified strike list from IBKR option chain
            expiries: Sorted expiry list (YYYYMMDD) from IBKR option chain

        Returns:
            {
                "expiry":       str,    # YYYYMMDD
                "dte":          int,
                "short_strike": float,
                "long_strike":  float,
                "short_bid":    float,
                "long_ask":     float,
                "net_credit":   float,
                "iv":           float,
                "lower_bound":  float,
            }
            or None if no qualifying spread found.
        """
        # Gate 1: market hours check — leg pricing only works when market is open
        if not self._is_market_open():
            now_et = datetime.now(ET)
            print(f"\n⏰ Market closed ({now_et.strftime('%H:%M ET %a')}) — "
                  f"live leg pricing unavailable.")
            print(f"   Market hours: Mon–Fri {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d}–"
                  f"{MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d} ET")
            return None

        # Gate 2: IV fetch
        iv = self._fetch_iv(spot)
        if iv is None:
            print("❌ Could not fetch IV — aborting scan.")
            return None

        em = ExpectedMove(spot=spot, iv=iv)
        today = datetime.today().date()

        # Filter to upcoming expiries within DTE cap
        candidates = []
        for expiry in expiries:
            expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
            dte = (expiry_date - today).days
            if MIN_DTE <= dte <= MAX_DTE:
                candidates.append((expiry, dte))

        print(f"\n🔍 Scanning Bull PUT spreads for {self.symbol}")
        print(f"   Spot: ${spot:.2f} | IV: {iv*100:.1f}% | "
              f"Scanning {len(candidates)} expiries (DTE {MIN_DTE}–{MAX_DTE})")

        if not candidates:
            print(f"   ⚠️  No expiries found within DTE {MIN_DTE}–{MAX_DTE}")
            return None

        for expiry, dte in candidates:
            summary    = em.summary(dte)
            lower_bound = summary["lower_bound"]
            one_sigma   = summary["one_sigma"]

            print(f"\n   DTE {dte:3d} | {expiry} | 2σ bound ${lower_bound:.2f} "
                  f"| Checking {len(SIGMA_SCAN_LEVELS)} sigma levels")

            # Collect candidate short strikes at each sigma level, deduplicating
            checked: set = set()
            for sigma in SIGMA_SCAN_LEVELS:
                target       = spot - sigma * one_sigma
                short_strike = self._resolve_short_strike(strikes, target)

                if short_strike in checked:
                    continue
                checked.add(short_strike)

                long_strike = short_strike - SPREAD_WIDTH
                if long_strike not in strikes:
                    long_strike = self._resolve_long_strike(strikes, short_strike)
                if long_strike is None or long_strike >= short_strike:
                    print(f"      {sigma}σ: ${short_strike:.0f} — no valid long strike")
                    continue

                # Fetch live bid/ask for both legs concurrently
                short_bid, long_ask = self._fetch_leg_prices(expiry, short_strike, long_strike, "P")

                if short_bid is None or long_ask is None:
                    print(f"      {sigma}σ: {expiry} ${short_strike:.0f}/${long_strike:.0f} — pricing failed")
                    continue

                net_credit = round(short_bid - long_ask, 4)
                print(f"      {sigma}σ: {expiry} ${short_strike:.0f}/${long_strike:.0f} "
                      f"| bid ${short_bid:.4f} / ask ${long_ask:.4f} "
                      f"| credit ${net_credit:.4f}")

                if net_credit >= MIN_NET_CREDIT:
                    print(f"      ✅ QUALIFIES at {sigma}σ — {expiry} ${short_strike:.0f}/${long_strike:.0f} "
                          f"credit ${net_credit:.4f} >= ${MIN_NET_CREDIT}")
                    return {
                        "expiry":       expiry,
                        "dte":          dte,
                        "short_strike": short_strike,
                        "long_strike":  long_strike,
                        "short_bid":    short_bid,
                        "long_ask":     long_ask,
                        "net_credit":   net_credit,
                        "iv":           iv,
                        "lower_bound":  lower_bound,
                        "sigma_level":  sigma,
                    }

            print(f"      ❌ No qualifying credit across all sigma levels — trying next expiry")

        print(f"\n❌ No qualifying Bull PUT spread found in DTE {MIN_DTE}–{MAX_DTE}.")
        return None

    # =========================================================================
    # Market Hours
    # =========================================================================

    def _is_market_open(self) -> bool:
        """
        Returns True if current time is within NYSE regular market hours.
        Mon–Fri, 09:30–16:00 Eastern. Does not account for market holidays.
        """
        now = datetime.now(ET)
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        market_open  = now.replace(hour=MARKET_OPEN[0],  minute=MARKET_OPEN[1],
                                   second=0, microsecond=0)
        market_close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                                   second=0, microsecond=0)
        return market_open <= now < market_close

    # =========================================================================
    # Strike Resolution
    # =========================================================================

    def _resolve_short_strike(self, strikes: list, lower_bound: float) -> float:
        """
        Nearest $5-increment strike at or below the target price.
        Restricting to STRIKE_INCREMENT multiples keeps contracts liquid and
        avoids wide bid/ask spreads on odd strikes.
        """
        candidates = [s for s in strikes if s <= lower_bound and s % STRIKE_INCREMENT == 0]
        if not candidates:
            # Fall back to nearest $5-increment strike overall
            fallback = [s for s in strikes if s % STRIKE_INCREMENT == 0]
            return min(fallback, key=lambda s: abs(s - lower_bound)) if fallback else min(strikes)
        return max(candidates)

    def _resolve_long_strike(self, strikes: list, short_strike: float) -> Optional[float]:
        """
        Long PUT = short strike - $10, or nearest strike below that.
        """
        target = short_strike - SPREAD_WIDTH
        candidates = [s for s in strikes if s < short_strike]
        if not candidates:
            return None
        return min(candidates, key=lambda s: abs(s - target))

    # =========================================================================
    # IV Fetch
    # =========================================================================

    def _fetch_iv(self, spot: float) -> Optional[float]:
        """
        Fetches implied volatility for SPY using VIX close price via historical bars.

        VIX = 30-day implied volatility of SPY, expressed as an annualised percentage.
        Works 24/7 — no live market data subscription required.
        Returns IV as a decimal (e.g. 0.15 for 15%).
        """
        req_id = self.client.get_new_req_id()

        from ibapi.contract import Contract
        contract = Contract()
        contract.symbol   = "VIX"
        contract.secType  = "IND"
        contract.exchange = "CBOE"
        contract.currency = "USD"

        self.client._req_to_symbol[req_id] = "VIX"
        self.client._active_historical.add(req_id)
        self.client.market_data[req_id] = []

        print(f"\n⏳ Fetching VIX (SPY IV proxy) via historical bars (Req: {req_id})...")
        self.client.reqHistoricalData(
            req_id, contract, "", "2 D", "1 day", "TRADES", 1, 1, False, []
        )

        for _ in range(IV_TIMEOUT):
            time.sleep(1)
            if self.client.market_data.get(req_id):
                break

        bars = self.client.market_data.get(req_id, [])
        if not bars:
            print(f"   ⚠️ VIX data not received within {IV_TIMEOUT}s")
            return None

        vix_close = bars[-1]["close"]
        iv = vix_close / 100.0

        print(f"   VIX close    : {vix_close:.2f}")
        print(f"   IV (decimal) : {iv:.4f}")
        print(f"   IV (%)       : {iv*100:.2f}%")
        return iv

    # =========================================================================
    # Option Pricing
    # =========================================================================

    def _fetch_leg_prices(self, expiry: str, short_strike: float,
                          long_strike: float, right: str) -> tuple:
        """
        Fetches short PUT bid and long PUT ask concurrently via two simultaneous
        reqMktData calls. Returns (short_bid, long_ask) — either may be None on timeout.

        Firing both requests in parallel halves wall-clock time vs sequential calls.
        """
        short_req = self.client.get_new_req_id()
        long_req  = self.client.get_new_req_id()

        short_event = threading.Event()
        long_event  = threading.Event()

        self.client._option_price_events = getattr(self.client, "_option_price_events", {})
        self.client._option_price_data   = getattr(self.client, "_option_price_data",   {})

        self.client._option_price_events[short_req] = {"event": short_event, "side": "bid"}
        self.client._option_price_events[long_req]  = {"event": long_event,  "side": "ask"}

        short_contract = self.client._build_option_contract(self.symbol, expiry, short_strike, right)
        long_contract  = self.client._build_option_contract(self.symbol, expiry, long_strike,  right)
        short_contract.exchange = "SMART"
        long_contract.exchange  = "SMART"

        # Fire both simultaneously
        self.client.reqMktData(short_req, short_contract, "", False, False, [])
        self.client.reqMktData(long_req,  long_contract,  "", False, False, [])

        # Wait for both — IV_TIMEOUT is the max, market hours responses arrive in <1s
        short_event.wait(timeout=IV_TIMEOUT)
        long_event.wait(timeout=IV_TIMEOUT)

        self.client.cancelMktData(short_req)
        self.client.cancelMktData(long_req)

        return (
            self.client._option_price_data.get(short_req),
            self.client._option_price_data.get(long_req),
        )

    def _fetch_option_bid(self, expiry: str, strike: float, right: str) -> Optional[float]:
        """Fetches the live bid price for an option contract."""
        return self._fetch_option_price(expiry, strike, right, side="bid")

    def _fetch_option_ask(self, expiry: str, strike: float, right: str) -> Optional[float]:
        """Fetches the live ask price for an option contract."""
        return self._fetch_option_price(expiry, strike, right, side="ask")

    def _fetch_option_price(self, expiry: str, strike: float, right: str,
                             side: str) -> Optional[float]:
        """
        Fetches live bid or ask for a specific option contract via reqMktData.
        side: "bid" or "ask"
        Returns price as float, or None on timeout.
        Only called when market is confirmed open.
        """
        req_id = self.client.get_new_req_id()
        event  = threading.Event()

        self.client._option_price_events = getattr(self.client, "_option_price_events", {})
        self.client._option_price_data   = getattr(self.client, "_option_price_data",   {})
        self.client._option_price_events[req_id] = {"event": event, "side": side}

        contract = self.client._build_option_contract(self.symbol, expiry, strike, right)
        contract.exchange = "SMART"

        self.client.reqMktData(req_id, contract, "", False, False, [])

        event.wait(timeout=IV_TIMEOUT)
        self.client.cancelMktData(req_id)

        return self.client._option_price_data.get(req_id)