import unittest
from unittest.mock import MagicMock, patch
from src.broker.ibkr.market_data_client import MarketDataClient


class TestIBKRMarketDataClient(unittest.TestCase):
    def setUp(self):
        # Block the network layer for clean unit testing isolation
        self.patcher_connect = patch('ibapi.client.EClient.connect')
        self.mock_connect = self.patcher_connect.start()

        self.data_client = MarketDataClient()
        self.data_client.conn = MagicMock()

    def tearDown(self):
        self.patcher_connect.stop()

    def test_realtime_bar_callback_updates_isolated_cache(self):
        """Verify that streaming data parses cleanly into the dedicated market data dictionary."""
        req_id = 5001
        self.data_client._req_to_symbol[req_id] = "AAPL"

        # Simulate incoming streaming bar event
        self.data_client.realTimeBar(
            reqId=req_id,
            time=1600000000,
            open_=170.00,
            high=172.00,
            low=169.50,
            close=171.25,
            volume=3500,
            wap=170.80,
            count=8
        )

        # Assertions match our clean, decoupled state variables
        self.assertIn(req_id, self.data_client.market_data)
        self.assertEqual(self.data_client.market_data[req_id]["symbol"], "AAPL")
        self.assertEqual(self.data_client.market_data[req_id]["close"], 171.25)
        self.assertEqual(self.data_client.market_data[req_id]["volume"], 3500)


if __name__ == "__main__":
    unittest.main()