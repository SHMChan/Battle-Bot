import logging
import threading
from threading import Event
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.order import Order
from ibapi.contract import Contract, ComboLeg
from src.broker.ibkr.config import PORT

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
        self.port = kwargs.get("port", PORT)
        self.client_id = kwargs.get("client_id", 1)

        # Core State Tracking Registries
        self.active_orders = {}
        self.open_positions = {}
        self._account_data = {}

        # Thread Synchronization Tools
        self._id_ready = Event()
        self._next_order_id = None

        # Request ID counter (separate from order IDs)
        self._next_req_id = 3000

        # Option conId resolution
        self._option_con_id_cache = {}   # (symbol, expiry, strike, right) -> conId
        self._option_con_id_events = {}  # req_id -> threading.Event
        self._req_to_option = {}         # req_id -> cache key tuple

        # Order fill tracking
        self._order_fill_events = {}     # order_id -> threading.Event

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

    def get_new_req_id(self) -> int:
        """Returns a unique request ID for non-order IBKR calls."""
        self._next_req_id += 1
        return self._next_req_id

    # =========================================================================
    # Option Contract Resolution
    # =========================================================================

    def _resolve_option_con_id(self, symbol: str, expiry: str, strike: float,
                                right: str, timeout: float = 5.0) -> int:
        """
        Resolves the IBKR conId for an option contract via reqContractDetails.
        Required for building BAG combo legs — conId must be exact.
        Results are cached so the second leg lookup in the same session is instant.
        """
        cache_key = (symbol, expiry, strike, right)
        if cache_key in self._option_con_id_cache:
            return self._option_con_id_cache[cache_key]

        req_id = self.get_new_req_id()
        event  = threading.Event()
        self._option_con_id_events[req_id] = event
        self._req_to_option[req_id] = cache_key

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = expiry
        contract.strike = strike
        contract.right = right
        contract.multiplier = "100"

        logger.info(f"TradingClient: Resolving conId for {symbol} {expiry} {right}@{strike}")
        self.reqContractDetails(req_id, contract)
        event.wait(timeout=timeout)

        return self._option_con_id_cache.get(cache_key, 0)

    # =========================================================================
    # Bull PUT Spread Orders
    # =========================================================================

    def place_bull_put_spread(self, symbol: str, expiry: str, short_strike: float,
                               long_strike: float, net_credit: float,
                               quantity: int = 1) -> int:
        """
        Places a bull PUT spread via a BAG combo limit order.

        Sells the short PUT (higher strike) and buys the long PUT (lower strike)
        as a single combo order at the net_credit limit price.

        Returns the order_id or 0 on failure (unresolvable conIds).
        """
        short_con_id = self._resolve_option_con_id(symbol, expiry, short_strike, "P")
        long_con_id  = self._resolve_option_con_id(symbol, expiry, long_strike,  "P")

        if not short_con_id or not long_con_id:
            logger.error(f"TradingClient: Could not resolve conIds for {symbol} spread — order skipped.")
            return 0

        contract = self._build_bag_contract(symbol, short_con_id, long_con_id,
                                             short_action="SELL", long_action="BUY")

        order = self.create_limit_order(action="SELL", totalQuantity=quantity,
                                        limit_price=round(net_credit, 2))

        order_id = self.get_next_valid_id()
        self._order_fill_events[order_id] = threading.Event()

        print(f"\n📤 Placing Bull PUT spread order #{order_id} | "
              f"{symbol} {expiry} {short_strike:.0f}/{long_strike:.0f} P "
              f"@ net credit ${net_credit:.4f}")
        self.place_order_lifecycle(order_id, contract, order)
        return order_id

    def close_bull_put_spread(self, symbol: str, expiry: str, short_strike: float,
                               long_strike: float, net_debit: float,
                               quantity: int = 1) -> int:
        """
        Buys back a previously sold bull PUT spread at the net_debit limit price.

        Returns the order_id or 0 on failure.
        """
        short_con_id = self._resolve_option_con_id(symbol, expiry, short_strike, "P")
        long_con_id  = self._resolve_option_con_id(symbol, expiry, long_strike,  "P")

        if not short_con_id or not long_con_id:
            logger.error(f"TradingClient: Could not resolve conIds for closing {symbol} spread.")
            return 0

        # Reverse legs — buy back short PUT, sell long PUT
        contract = self._build_bag_contract(symbol, short_con_id, long_con_id,
                                             short_action="BUY", long_action="SELL")

        order = self.create_limit_order(action="BUY", totalQuantity=quantity,
                                        limit_price=round(net_debit, 2))

        order_id = self.get_next_valid_id()
        self._order_fill_events[order_id] = threading.Event()

        print(f"\n📥 Closing Bull PUT spread order #{order_id} | "
              f"{symbol} {expiry} {short_strike:.0f}/{long_strike:.0f} P "
              f"@ net debit ${net_debit:.4f}")
        self.place_order_lifecycle(order_id, contract, order)
        return order_id

    def wait_for_fill(self, order_id: int, timeout: float = 60.0) -> bool:
        """
        Blocks until the order is filled or the timeout expires.
        Returns True if filled, False on timeout or missing order.
        """
        event = self._order_fill_events.get(order_id)
        if event is None:
            logger.warning(f"TradingClient: No fill event registered for order {order_id}.")
            return False
        filled = event.wait(timeout=timeout)
        if not filled:
            logger.warning(f"TradingClient: Order {order_id} fill timed out after {timeout}s.")
        return filled

    def _build_bag_contract(self, symbol: str, short_con_id: int, long_con_id: int,
                             short_action: str, long_action: str) -> Contract:
        """Builds an IBKR BAG (combo) contract for a two-leg option spread."""
        leg1 = ComboLeg()
        leg1.conId    = short_con_id
        leg1.ratio    = 1
        leg1.action   = short_action
        leg1.exchange = "CBOE"

        leg2 = ComboLeg()
        leg2.conId    = long_con_id
        leg2.ratio    = 1
        leg2.action   = long_action
        leg2.exchange = "CBOE"

        contract = Contract()
        contract.symbol      = symbol
        contract.secType     = "BAG"
        contract.currency    = "USD"
        contract.exchange    = "SMART"
        contract.comboLegs   = [leg1, leg2]
        return contract

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

    def contractDetails(self, reqId: int, contractDetails):
        """Captures conId from option contract details response."""
        cache_key = self._req_to_option.get(reqId)
        if cache_key and contractDetails.contract.secType == "OPT":
            con_id = contractDetails.contract.conId
            self._option_con_id_cache[cache_key] = con_id
            logger.info(f"TradingClient: Resolved option conId={con_id} for {cache_key}")

    def contractDetailsEnd(self, reqId: int):
        """Unblocks the thread waiting for option conId resolution."""
        event = self._option_con_id_events.get(reqId)
        if event:
            event.set()

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

        if status == "Filled" and orderId in self._order_fill_events:
            self._order_fill_events[orderId].set()

    def error(self, id: int, errorCode: int, errorString: str):
        logger.error(f"IBKR Event Error Context [{id}] Code {errorCode}: {errorString}")
        if id in self.active_orders:
            if errorCode in [201, 202]:
                self.active_orders[id]["status"] = "Cancelled" if errorCode == 202 else "Rejected"