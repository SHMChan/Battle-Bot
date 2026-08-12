import unittest
import math
from src.strategy.bull_put.expected_move import ExpectedMove


class TestExpectedMove(unittest.TestCase):
    """
    Unit tests for ExpectedMove — pure math, no IBKR connection required.
    Uses SPY-like values for intuitive sanity checks.
    """

    def setUp(self):
        # SPY @ $737, IV = 15% — representative real-world values
        self.spot = 737.0
        self.iv   = 0.15
        self.em   = ExpectedMove(spot=self.spot, iv=self.iv)

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    def test_invalid_spot_raises(self):
        with self.assertRaises(ValueError):
            ExpectedMove(spot=0, iv=0.15)

    def test_invalid_iv_raises(self):
        with self.assertRaises(ValueError):
            ExpectedMove(spot=737.0, iv=0)

    def test_negative_dte_raises(self):
        with self.assertRaises(ValueError):
            self.em.one_sigma(dte=0)

    # -------------------------------------------------------------------------
    # 1σ calculation
    # -------------------------------------------------------------------------

    def test_one_sigma_formula(self):
        """1σ = IV × spot × √(DTE/365)"""
        dte = 5
        expected = 0.15 * 737.0 * math.sqrt(5 / 365)
        self.assertAlmostEqual(self.em.one_sigma(dte), expected, places=6)

    def test_one_sigma_scales_with_dte(self):
        """Longer DTE = larger expected move."""
        self.assertGreater(self.em.one_sigma(30), self.em.one_sigma(5))

    def test_one_sigma_dte1(self):
        """Single day move is small but positive."""
        move = self.em.one_sigma(1)
        self.assertGreater(move, 0)
        self.assertLess(move, 10)  # SPY doesn't move $10 in a day at 15% IV

    # -------------------------------------------------------------------------
    # 2σ calculation
    # -------------------------------------------------------------------------

    def test_two_sigma_is_double_one_sigma(self):
        for dte in [1, 5, 10, 30]:
            self.assertAlmostEqual(
                self.em.two_sigma(dte),
                2 * self.em.one_sigma(dte),
                places=10
            )

    # -------------------------------------------------------------------------
    # Bounds
    # -------------------------------------------------------------------------

    def test_lower_bound_below_spot(self):
        """2σ lower bound must always be below spot."""
        for dte in [1, 5, 10, 30]:
            self.assertLess(self.em.lower_bound_2sigma(dte), self.spot)

    def test_upper_bound_above_spot(self):
        """2σ upper bound must always be above spot."""
        for dte in [1, 5, 10, 30]:
            self.assertGreater(self.em.upper_bound_2sigma(dte), self.spot)

    def test_bounds_symmetric(self):
        """Upper and lower bounds are equidistant from spot."""
        for dte in [1, 5, 10, 30]:
            upper_dist = self.em.upper_bound_2sigma(dte) - self.spot
            lower_dist = self.spot - self.em.lower_bound_2sigma(dte)
            self.assertAlmostEqual(upper_dist, lower_dist, places=10)

    def test_lower_bound_dte5_reasonable(self):
        """
        SPY @ $737, IV=15%, DTE=5:
        1σ ≈ $7.28, 2σ ≈ $14.56, lower bound ≈ $722.44
        Sanity check that result is in a reasonable range.
        """
        lb = self.em.lower_bound_2sigma(5)
        self.assertGreater(lb, 700)
        self.assertLess(lb, 735)

    # -------------------------------------------------------------------------
    # Summary dict
    # -------------------------------------------------------------------------

    def test_summary_keys(self):
        s = self.em.summary(5)
        for key in ["dte", "spot", "iv", "one_sigma", "two_sigma",
                    "lower_bound", "upper_bound"]:
            self.assertIn(key, s)

    def test_summary_values_consistent(self):
        s = self.em.summary(10)
        self.assertEqual(s["spot"], self.spot)
        self.assertEqual(s["iv"],   self.iv)
        self.assertEqual(s["dte"],  10)
        self.assertAlmostEqual(s["two_sigma"], 2 * s["one_sigma"], places=2)
        self.assertAlmostEqual(s["lower_bound"], self.spot - s["two_sigma"], places=2)
        self.assertAlmostEqual(s["upper_bound"], self.spot + s["two_sigma"], places=2)

    def test_summary_rounded_to_2dp(self):
        s = self.em.summary(7)
        for key in ["one_sigma", "two_sigma", "lower_bound", "upper_bound"]:
            val = s[key]
            self.assertEqual(val, round(val, 2), f"{key} not rounded to 2dp")


if __name__ == "__main__":
    unittest.main()
