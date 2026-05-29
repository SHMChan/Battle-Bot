import pytest
from unittest.mock import MagicMock
from src.broker.ibkr.trading_client import TradingClient


class TestTradingClient:

    @pytest.fixture
    def mock_ib(self):
        """Pure MagicMock passed directly to the client under test."""
        return MagicMock()

    def test_connect_establishes_connection(self, mock_ib):
        # Arrange
        client = TradingClient(ib_api=mock_ib)

        # Act
        client.connect(host='127.0.0.1', port=7497, client_id=1)

        # Assert
        mock_ib.connect.assert_called_once_with('127.0.0.1', 7497, clientId=1)

    def test_get_account_summary_returns_formatted_data(self, mock_ib):
        # Arrange
        client = TradingClient(ib_api=mock_ib)

        # Mocking the return value of IB's account summary
        mock_ib.accountSummary.return_value = [
            MagicMock(tag='NetLiquidation', value='100000.00', currency='USD'),
            MagicMock(tag='BuyingPower', value='400000.00', currency='USD')
        ]

        # Act
        summary = client.get_account_summary()

        # Assert
        assert summary['NetLiquidation'] == 100000.00
        assert summary['BuyingPower'] == 400000.00



    def test_place_limit_order_sends_correct_contract_and_price(self, mock_ib):
        # Arrange
        client = TradingClient(ib_api=mock_ib)

        # Act
        client.place_limit_order(symbol='AAPL', action='BUY', quantity=10, limit_price=150.50)

        # Assert
        mock_ib.placeOrder.assert_called_once()
        call_args = mock_ib.placeOrder.call_args[0]

        contract = call_args[0]
        order = call_args[1]

        assert contract.symbol == 'AAPL'
        assert order.action == 'BUY'
        assert order.totalQuantity == 10
        assert order.orderType == 'LMT'
        assert order.lmtPrice == 150.50

        def test_get_open_orders_returns_list_of_active_orders(self, mock_ib):
            # Arrange
            client = TradingClient(ib_api=mock_ib)

            # ib_insync returns a list of 'Trade' objects for open trades
            mock_trade = MagicMock()
            mock_trade.contract.symbol = 'TSLA'
            mock_trade.order.action = 'BUY'
            mock_trade.order.totalQuantity = 5
            mock_trade.order.lmtPrice = 180.00
            mock_trade.orderStatus.status = 'Submitted'

            mock_ib.trades.return_value = [mock_trade]

            # Act
            open_orders = client.get_open_orders()

            # Assert
            assert len(open_orders) == 1
            assert open_orders[0]['symbol'] == 'TSLA'
            assert open_orders[0]['limit_price'] == 180.00
            assert open_orders[0]['status'] == 'Submitted'

        def test_cancel_order_sends_cancel_request_to_ib(self, mock_ib):
            # Arrange
            client = TradingClient(ib_api=mock_ib)

            # Mock an active trade that matches the order ID we want to cancel
            mock_trade = MagicMock()
            mock_trade.order.orderId = 42
            mock_ib.trades.return_value = [mock_trade]

            # Act
            success = client.cancel_order(order_id=42)

            # Assert
            # Verify IB's cancelOrder was called with the actual order object
            mock_ib.cancelOrder.assert_called_once_with(mock_trade.order)
            assert success is True

        def test_cancel_order_returns_false_if_order_not_found(self, mock_ib):
            # Arrange
            client = TradingClient(ib_api=mock_ib)
            mock_ib.trades.return_value = []  # No open orders

            # Act
            success = client.cancel_order(order_id=999)

            # Assert
            mock_ib.cancelOrder.assert_not_called()
            assert success is False