import math
import logging

logger = logging.getLogger(__name__)


class ExpectedMove:
    """
    Computes the options-implied expected price move for a given symbol.

    Uses the standard Black-Scholes approximation:
        1σ move ($) = IV × Spot × √(DTE / 365)

    The 2σ lower bound is the target short PUT strike for a Bull PUT spread —
    statistically outside ~95% of expected outcomes.

    Formula reference:
        Expected Move = IV × Price × √(DTE/365)
        2σ Lower Bound = Spot - 2 × Expected Move
    """

    def __init__(self, spot: float, iv: float):
        """
        Args:
            spot: Current underlying price (e.g. SPY last close)
            iv:   Implied volatility as a decimal (e.g. 0.15 for 15%)
        """
        if spot <= 0:
            raise ValueError(f"Spot price must be positive, got {spot}")
        if iv <= 0:
            raise ValueError(f"IV must be positive, got {iv}")

        self.spot = spot
        self.iv = iv

    def one_sigma(self, dte: int) -> float:
        """
        1σ expected move in dollars for the given DTE.

        Args:
            dte: Days to expiration (calendar days)

        Returns:
            Dollar move representing 1 standard deviation
        """
        if dte <= 0:
            raise ValueError(f"DTE must be positive, got {dte}")
        return self.iv * self.spot * math.sqrt(dte / 365)

    def two_sigma(self, dte: int) -> float:
        """2σ expected move in dollars — covers ~95% of outcomes."""
        return 2 * self.one_sigma(dte)

    def lower_bound_2sigma(self, dte: int) -> float:
        """
        2σ lower bound — the target short PUT strike for a Bull PUT spread.
        Price has ~97.5% probability of staying above this level.
        """
        return self.spot - self.two_sigma(dte)

    def upper_bound_2sigma(self, dte: int) -> float:
        """2σ upper bound — symmetric upside."""
        return self.spot + self.two_sigma(dte)

    def summary(self, dte: int) -> dict:
        """
        Returns a full summary dict for a given DTE.

        Returns:
            {
                "dte": int,
                "spot": float,
                "iv": float,
                "one_sigma": float,
                "two_sigma": float,
                "lower_bound": float,   # 2σ short PUT target
                "upper_bound": float,
            }
        """
        one_sig = self.one_sigma(dte)
        two_sig = self.two_sigma(dte)
        return {
            "dte":         dte,
            "spot":        self.spot,
            "iv":          self.iv,
            "one_sigma":   round(one_sig, 2),
            "two_sigma":   round(two_sig, 2),
            "lower_bound": round(self.spot - two_sig, 2),
            "upper_bound": round(self.spot + two_sig, 2),
        }

    def print_summary(self, dte: int):
        """Prints a formatted summary for quick inspection."""
        s = self.summary(dte)
        print(f"\n📐 Expected Move Summary — DTE {s['dte']}")
        print(f"   Spot          : ${s['spot']:.2f}")
        print(f"   IV            : {s['iv']*100:.1f}%")
        print(f"   1σ move       : ±${s['one_sigma']:.2f}")
        print(f"   2σ move       : ±${s['two_sigma']:.2f}")
        print(f"   2σ lower bound: ${s['lower_bound']:.2f}  ← short PUT target")
        print(f"   2σ upper bound: ${s['upper_bound']:.2f}")
