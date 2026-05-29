from ib_insync import IB, Stock, LimitOrder

class TradingClient:
    def __init__(self, ib_api=None):
        self.ib = ib_api if ib_api is not None else IB()

    def connect(self, host: str, port: int, client_id: int):
        self.ib.connect(host, port, clientId=client_id)

    def get_account_summary(self) -> dict:
        summary_data = self.ib.accountSummary()
        formatted = {}
        for item in summary_data:
            if item.tag in ['NetLiquidation', 'BuyingPower']:
                formatted[item.tag] = float(item.value)
        return formatted

    def place_limit_order(self, symbol: str, action: str, quantity: int, limit_price: float):
        contract = Stock(symbol, 'SMART', 'USD')
        # Explicitly using LimitOrder to guard against slippage
        order = LimitOrder(action, quantity, limit_price)
        return self.ib.placeOrder(contract, order)

    def disconnect(self):
        """Disconnects cleanly from the IB Gateway/TWS session."""
        if self.ib.isConnected():
            self.ib.disconnect()

            def get_open_orders(self) -> list:
                """Retrieves all currently active working orders formatted uniformly."""
                # ib.trades() returns all trades managed in the current session
                active_trades = self.ib.trades()

                open_orders = []
                for trade in active_trades:
                    # We filter out filled or cancelled orders to only track 'working' ones
                    if trade.orderStatus.status in ['PendingSubmit', 'PreSubmitted', 'Submitted']:
                        open_orders.append({
                            'order_id': trade.order.orderId,
                            'symbol': trade.contract.symbol,
                            'action': trade.order.action,
                            'quantity': trade.order.totalQuantity,
                            'limit_price': trade.order.lmtPrice,
                            'status': trade.orderStatus.status
                        })
                return open_orders

            def cancel_order(self, order_id: int) -> bool:
                """Finds a working order by ID and requests its cancellation."""
                for trade in self.ib.trades():
                    if trade.order.orderId == order_id:
                        self.ib.cancelOrder(trade.order)
                        return True
                return False