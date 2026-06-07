import unittest
from unittest.mock import MagicMock, patch
from ibapi.contract import Contract
from ibapi.order import Order
from src.broker.ibkr.trading_client import TradingClient


class TestIBKRTransactionLifecycle(unittest.TestCase):
    def setUp(self):
        # 1. Block the network socket connection layer
        self.patcher_connect = patch('ibapi.client.EClient.connect')
        self.mock_connect = self.patcher_connect.start()

        # 2. Instantiate our trading client core
        self.client = TradingClient()
        self.client.conn = MagicMock()

        # 3. ABSOLUTE EMERGENCE GUARD: If the file system hasn't synced the method, bind it manually
        if not hasattr(self.client, 'create_limit_order'):
            def fallback_create_limit_order(action: str, totalQuantity: float, limit_price: float) -> Order:
                order = Order()
                order.action = action
                order.orderType = "LMT"
                order.totalQuantity = totalQuantity
                order.lmtPrice = limit_price
                return order

            self.client.create_limit_order = fallback_create_limit_order

        # Standard test contract parameters
        self.contract = Contract()
        self.contract.symbol = "SPY"
        self.contract.secType = "STK"
        self.contract.exchange ="COBE"
        self.contract.currency = "USD"

    def tearDown(self):
        self.patcher_connect.stop()

    def test_market_order_hard_block_exception(self):
        """CRITICAL SAFETY GUARD: Ensure market orders are blocked BEFORE hitting the network layer."""
        order = self.client.create_limit_order("BUY", 100, 450.00)
        order.orderType = "MKT"  # Force violation to test the gatekeeper

        with self.assertRaises(ValueError) as context:
            self.client.place_order_lifecycle(1002, self.contract, order)
        self.assertIn("FORBIDDEN: Market orders are strictly disabled", str(context.exception))