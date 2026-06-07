import logging
from threading import Event
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.order import Order
from ibapi.contract import Contract

logger = logging.getLogger(__name__)


class TradingClient(EWrapper, EClient):
    def __init__(self, ib_api=None, **kwargs):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        # Backward-compatibility layer for legacy mock framework
        self.is_legacy_mock = ib_api is not None
        if self.is_legacy_mock:
            self.conn = ib_api

        self.host = kwargs.get("host", "127.0.0.1")
        self.port = kwargs.get("port", 7497)
        self.client_id = kwargs.get("client_id", 1)

        # Core State Tracking Registries
        self.active_orders = {}
        self.open_positions = {}
        self._account_data = {}

        # Thread Synchronization Tools
        self._id_ready = Event()
        self._next_order_id = None

    def connect(self, host: str, port: int, clientId: int = None, client_id: int = None):
        """
        Maps snake_case and camelCase connection parameters cleanly.
        Bypasses raw sockets if interacting with a legacy test mock engine.
        """
        target_client_id = clientId if clientId is not None else client_id
        if target_client_id is None:
            target_client_id = self.client_id

        if self.is_legacy_mock:
            self.conn.connect(host, port, clientId=target_client_id)
            return

        super().connect(host, port, target_client_id)

    # =========================================================================
    # Broker-Agnostic Core Interface
    # =========================================================================

    def get_account_summary(self) -> dict:
        """Requests and flattens account summary metrics into parsed floats."""
        if self.is_legacy_mock:
            mock_data = self.conn.accountSummary()
            return {item.tag: float(item.value) for item in mock_data}

        self.reqAccountSummary(reqId=9001, groupName="All", tags="NetLiquidation,BuyingPower")
        return {k: float(v) for k, v in self._account_data.items()}

    def place_limit_order(self, symbol: str, action: str, quantity: float, limit_price: float, order_id: int = 1):
        """Builds a standard US Stock contract and routes it through the execution manager."""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange ="COBE"
        contract.currency = "USD"

        order = self.create_limit_order(action, quantity, limit_price)
        self.place_order_lifecycle(order_id, contract, order)

    def req_live_positions(self):
        """Initiates a streaming server subscription for all active portfolio positions."""
        logger.info("Requesting live position synchronization stream from TWS.")
        if self.is_legacy_mock:
            return
        self.reqPositions()

    def get_next_valid_id(self, timeout: float = 5.0) -> int:
        """Thread-safe blocking retrieval for tracking index synchronizations."""
        if self.is_legacy_mock:
            return 1001

        self._id_ready.clear()
        self.reqIds(-1)

        success = self._id_ready.wait(timeout=timeout)
        if not success:
            logger.warning("ID request timed out. Falling back to internal increment calculation.")
            if self._next_order_id is None:
                raise TimeoutError("Failed to synchronize initial Order ID sequence from IB Gateway.")
            self._next_order_id += 1
            return self._next_order_id

        return self._next_order_id

    def cancel_active_order(self, order_id: int):
        """Transmits an exit/cancellation signal to the exchange for a working order."""
        if order_id not in self.active_orders:
            logger.warning(f"Cancellation rejected locally: Order ID {order_id} is not tracked.")
            return

        logger.info(f"Initiating cancellation request for Order ID: {order_id}")
        if self.is_legacy_mock:
            self.conn.cancelOrder(order_id)
        else:
            self.cancelOrder(order_id)

    # =========================================================================
    # Core Order Generation & Safety Lifecycle Guards
    # =========================================================================

    def create_limit_order(self, action: str, totalQuantity: float, limit_price: float) -> Order:
        """Helper factory to cleanly generate explicit limit orders."""
        order = Order()
        order.action = action
        order.orderType = "LMT"
        order.totalQuantity = totalQuantity
        order.lmtPrice = limit_price
        order.transmit = True
        return order

    def place_order_lifecycle(self, order_id: int, contract: Contract, order: Order):
        """
        Executes order routing. Intercepts and completely blocks market
        orders from touching network sockets to maintain strict system compliance.
        """
        # CRITICAL SAFETY GUARDRAIL: Violating this parameter halts execution immediately
        if order.orderType == "MKT":
            logger.critical(
                f"CRITICAL VIOLATION: Execution engine blocked a Market Order request for {contract.symbol}.")
            raise ValueError("FORBIDDEN: Market orders are strictly disabled. Limit orders only.")

        self.active_orders[order_id] = {
            "status": "PreSubmitted",
            "filled": 0,
            "remaining": order.totalQuantity,
            "symbol": contract.symbol,
            "avg_price": 0.0
        }

        if self.is_legacy_mock:
            self.conn.placeOrder(contract, order)
        else:
            self.placeOrder(order_id, contract, order)

    # =========================================================================
    # IBAPI Asynchronous Wrapper Event Callbacks
    # =========================================================================

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self._next_order_id = orderId
        self._id_ready.set()
        logger.info(f"Order ID sequence synchronized at index: {orderId}")

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        self._account_data[tag] = value

    def position(self, account: str, contract: object, position: float, avgCost: float):
        """Updates internal risk inventory matrix when executions alter exposure."""
        symbol = contract.symbol
        if position == 0:
            if symbol in self.open_positions:
                del self.open_positions[symbol]
                logger.info(f"Position for {symbol} closed. Cleaned from active risk tracking.")
        else:
            self.open_positions[symbol] = {
                "position": position,
                "avg_cost": avgCost,
                "market_value": position * avgCost
            }
            logger.info(f"Position Sync | {symbol}: {position} holdings @ Avg Cost: ${avgCost:.2f}")

    def positionEnd(self):
        logger.info("All open core positions have finished synchronizing.")

    def orderStatus(self, orderId: int, status: str, filled: float,
                    remaining: float, avgFillPrice: float, permId: int,
                    parentId: int, lastFillPrice: float, clientId: int,
                    whyHeld: str):
        if orderId in self.active_orders:
            self.active_orders[orderId].update({
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avg_price": avgFillPrice
            })
            logger.info(f"Order {orderId} Sync: {status} | Filled: {filled} | Remaining: {remaining}")

    def error(self, id: int, errorCode: int, errorString: str):
        logger.error(f"IBKR Event Error Context [{id}] Code {errorCode}: {errorString}")
        if id in self.active_orders:
            if errorCode in [201, 202]:
                self.active_orders[id]["status"] = "Cancelled" if errorCode == 202 else "Rejected"