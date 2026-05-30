import unittest
from unittest.mock import MagicMock, patch
from ibapi.contract import Contract
from ibapi.order import Order
from src.broker.ibkr.trading_client import TradingClient


class TestIBKRTransactionLifecycle(unittest.TestCase):
    def setUp(self):
        # 1. Patch EClient's __init__ so it doesn't trigger internal network/socket initialization
        self.patcher_init = patch('ibapi.client.EClient.__init__', return_value=None)
        self.mock_init = self.patcher_init.start()

        # 2. Instantiate without keywords if your class configures these parameters downstream
        self.client = TradingClient()

        # 3. Manually assign the connection parameters your client uses to hit the network
        self.client.host = "127.0.0.1"
        self.client.port = 4002
        self.client.client_id = 1

        # 4. Hard mock the internal socket layer so ibapi never attempts to broadcast packets
        self.client.conn = MagicMock()

        # Standard test contract definition
        self.contract = Contract()
        self.contract.symbol = "SPY"
        self.contract.secType = "STK"
        self.contract.exchange = "SMART"
        self.contract.currency = "USD"

    def tearDown(self):
        # Safely tear down the patcher to keep our test suite isolated
        self.patcher_init.stop()

    def test_market_order_hard_block_exception(self):
        """CRITICAL SAFETY GUARD: Ensure market orders are blocked BEFORE hitting the network layer."""
        order = self.client.create_limit_order("BUY", 100, 450.00)
        order.orderType = "MKT"  # Intentionally forcing a violation to test the gatekeeper

        with self.assertRaises(ValueError) as context:
            self.client.place_order_lifecycle(1002, self.contract, order)

        # Verify the system threw the exact metric violation error message
        self.assertIn("FORBIDDEN: Market orders are strictly disabled", str(context.exception))