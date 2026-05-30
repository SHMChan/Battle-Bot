import logging
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

logger = logging.getLogger(__name__)


class MarketDataClient(EWrapper, EClient):
    def __init__(self, **kwargs):
        # 1. Initialize the base interfaces completely
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        # 2. Extract configuration variables with clear safety fallbacks
        self.host = kwargs.get("host", "127.0.0.1")
        self.port = kwargs.get("port", 7497)

        assigned_id = kwargs.get("client_id", kwargs.get("clientId", 2))
        self.client_id = assigned_id
        self.clientId = assigned_id  # Ensures complete alignment with native naming

        # 3. Dynamic Tracking Registries
        self.market_data = {}
        self._req_to_symbol = {}
        self._next_req_id = 1000
        self._active_streams = set()
        self._active_historical = set()

    def get_new_req_id(self) -> int:
        """Increments and returns a unique, collision-free tracking ID."""
        self._next_req_id += 1
        return self._next_req_id

    def stream_realtime_bars(self, req_id: int, symbol: str):
        """Subscribes to live 5-second real-time OHLC bar data."""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        self._req_to_symbol[req_id] = symbol
        self._active_streams.add(req_id)

        logger.info(f"MarketDataClient: Subscribing to 5s Bars for {symbol} (Req: {req_id})")
        self.reqRealTimeBars(req_id, contract, 5, "TRADES", False, [])

    def cancel_realtime_bars(self, req_id: int):
        """Safely cancels active real-time data streams."""
        if req_id in self._active_streams:
            logger.info(f"MarketDataClient: Canceling Stream for Req: {req_id}")
            self.cancelRealTimeBars(req_id)
            self._active_streams.remove(req_id)
            if req_id in self._req_to_symbol:
                del self._req_to_symbol[req_id]

    def fetch_historical_bars(self, req_id: int, symbol: str, duration: str = "1 D", bar_size: str = "1 hour"):
        """Fetches a static historical block of candles from IBKR's warehouse."""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        self._req_to_symbol[req_id] = symbol
        self._active_historical.add(req_id)
        self.market_data[req_id] = []

        self.reqHistoricalData(req_id, contract, "", duration, bar_size, "TRADES", 1, 1, False, [])

    # =========================================================================
    # Asynchronous Data Stream Callbacks
    # =========================================================================

    def realTimeBar(self, reqId: int, time: int, open_: float, high: float,
                    low: float, close: float, volume: int, wap: float, count: int):
        symbol = self._req_to_symbol.get(reqId, "UNKNOWN")

        self.market_data[reqId] = {
            "symbol": symbol,
            "time": time,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume
        }

    def historicalData(self, reqId: int, bar: object):
        if not isinstance(self.market_data.get(reqId), list):
            self.market_data[reqId] = []

        self.market_data[reqId].append({
            "symbol": self._req_to_symbol.get(reqId, "UNKNOWN"),
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume
        })

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        symbol = self._req_to_symbol.get(reqId, "UNKNOWN")
        total_bars = len(self.market_data.get(reqId, []))
        print(f"\n📊 Historical Sync Complete! Loaded {total_bars} bars for {symbol}.")

    def error(self, id: int, errorCode: int, errorString: str):
        print(f"\n⚠️ IBKR GATEWAY MESSAGE [ID {id}] | Code {errorCode}: {errorString}")