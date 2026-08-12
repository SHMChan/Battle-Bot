import logging
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from src.broker.ibkr.config import PORT

logger = logging.getLogger(__name__)


class MarketDataClient(EWrapper, EClient):
    def __init__(self, **kwargs):
        # 1. Initialize the base interfaces completely
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        # 2. Extract configuration variables with clear safety fallbacks
        self.host = kwargs.get("host", "127.0.0.1")
        self.port = kwargs.get("port", PORT)

        assigned_id = kwargs.get("client_id", kwargs.get("clientId", 2))
        self.client_id = assigned_id
        self.clientId = assigned_id  # Ensures complete alignment with native naming

        # 3. Dynamic Tracking Registries
        self.market_data = {}
        self._req_to_symbol = {}
        self._next_req_id = 1000
        self._active_streams = set()
        self._active_historical = set()
        self._option_chain_right_filter = {}  # req_id -> "C", "P", or ""

        # 4. Contract ID resolution
        # Maps symbol -> conId, populated by _resolve_con_id()
        self._con_id_cache = {}
        self._con_id_events = {}  # symbol -> threading.Event

        # 5. Trading class cache
        # Maps (symbol, expiry) -> tradingClass, e.g. ("SPY", "20260626") -> "SPYW"
        # Populated by securityDefinitionOptionParameter — used by _build_option_contract
        self._trading_class_cache = {}  # (symbol, expiry) -> tradingClass

    def get_new_req_id(self) -> int:
        """Increments and returns a unique, collision-free tracking ID."""
        self._next_req_id += 1
        return self._next_req_id

    def nextValidId(self, orderId: int):
        """Fires after the full IBKR handshake — the first safe point to send requests.
        Sets delayed market data mode so all subsequent reqMktData calls use delayed quotes."""
        self.reqMarketDataType(3)
        logger.info("MarketDataClient: Connected — market data type set to delayed (3).")

    # =========================================================================
    # Equity Methods
    # =========================================================================

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
    # Options Methods
    # =========================================================================

    def _resolve_con_id(self, symbol: str, timeout: float = 5.0) -> int:
        """
        Resolves the IBKR conId for a stock symbol via reqContractDetails.
        Required by reqSecDefOptParams — passing conId=0 causes Code 321.
        Results are cached so subsequent calls are instant.
        """
        if symbol in self._con_id_cache:
            return self._con_id_cache[symbol]

        req_id = self.get_new_req_id()
        event = threading.Event()
        self._con_id_events[symbol] = event

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        logger.info(f"MarketDataClient: Resolving conId for {symbol} (Req: {req_id})")
        self._req_to_symbol[req_id] = symbol
        self._pending_con_id_req = req_id  # track which req is the conId lookup
        self.reqContractDetails(req_id, contract)

        event.wait(timeout=timeout)
        return self._con_id_cache.get(symbol, 0)

    def fetch_option_chain(self, req_id: int, symbol: str, right: str = ""):
        """
        Fetches the full option chain (strikes + expiries) for a symbol via
        reqSecDefOptParams. Works 24/7, independent of market hours.
        right: "C" = calls only, "P" = puts only, "" = both (default)
        Resolves conId automatically before requesting the chain.
        """
        con_id = self._resolve_con_id(symbol)
        if con_id == 0:
            logger.error(f"MarketDataClient: Could not resolve conId for {symbol}")
            return

        self._req_to_symbol[req_id] = symbol
        self.market_data[req_id] = {}
        self._option_chain_right_filter[req_id] = right

        logger.info(f"MarketDataClient: Requesting option chain for {symbol} conId={con_id} (Req: {req_id})")
        self.reqSecDefOptParams(req_id, symbol, "", "STK", con_id)

    def stream_option_realtime_bars(self, req_id: int, symbol: str, expiry: str,
                                     strike: float, right: str):
        """
        Streams live 5-second real-time bars for a specific option contract.
        right: "C" = Call, "P" = Put
        expiry: YYYYMMDD format
        """
        contract = self._build_option_contract(symbol, expiry, strike, right)

        self._req_to_symbol[req_id] = symbol
        self._active_streams.add(req_id)

        logger.info(f"MarketDataClient: Streaming option bars {symbol} {expiry} {right}@{strike} (Req: {req_id})")
        self.reqRealTimeBars(req_id, contract, 5, "TRADES", False, [])

    def fetch_option_historical_bars(self, req_id: int, symbol: str, expiry: str,
                                      strike: float, right: str,
                                      duration: str = "1 D", bar_size: str = "1 hour"):
        """
        Fetches historical bars for a specific option contract.
        right: "C" = Call, "P" = Put
        expiry: YYYYMMDD format
        Works 24/7 independent of market hours.

        Uses SMART exchange for historical data — CBOE rejects historical
        requests for weekly contracts even when real-time streaming works fine.
        SMART routes to the correct venue automatically.
        """
        contract = self._build_option_contract(symbol, expiry, strike, right)
        contract.exchange = "SMART"  # Override CBOE — required for historical data on weeklies

        self._req_to_symbol[req_id] = symbol
        self._active_historical.add(req_id)
        self.market_data[req_id] = []

        logger.info(f"MarketDataClient: Historical option bars {symbol} {expiry} {right}@{strike} (Req: {req_id})")
        self.reqHistoricalData(req_id, contract, "", duration, bar_size, "TRADES", 1, 1, False, [])

    def _build_option_contract(self, symbol: str, expiry: str, strike: float, right: str) -> Contract:
        """
        Builds a standard IBKR option contract definition.
        Uses CBOE as the primary exchange — SMART often fails to resolve
        option contracts and returns Code 200.

        tradingClass is looked up from _trading_class_cache, populated during
        fetch_option_chain via securityDefinitionOptionParameter callbacks.
        This is critical: monthly SPY uses tradingClass "SPY", but weekly SPY
        uses "SPYW" — hardcoding symbol here breaks all weekly contracts (Code 200).
        Falls back to symbol if cache miss (safe for monthly contracts).
        """
        trading_class = self._trading_class_cache.get((symbol, expiry), symbol)
        cache_hit = (symbol, expiry) in self._trading_class_cache
        print(f"\n🔧 Building contract: {symbol} {expiry} {right}@{strike} "
              f"tradingClass={trading_class} (cache {'HIT' if cache_hit else 'MISS — fallback to symbol'})")

        contract = Contract()
        contract.symbol = symbol
        contract.secType = "OPT"
        contract.exchange = "CBOE"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = expiry
        contract.strike = strike
        contract.right = right          # "C" or "P"
        contract.multiplier = "100"     # Standard options multiplier
        contract.tradingClass = trading_class
        return contract

    def resolve_valid_strike(self, target_price: float, valid_strikes: list) -> float:
        """
        Finds the nearest valid strike from a verified IBKR chain strike list.
        Always prefer this over mathematical rounding.
        """
        if valid_strikes:
            return min(valid_strikes, key=lambda s: abs(s - target_price))
        return round(target_price)

    # =========================================================================
    # Asynchronous Data Stream Callbacks
    # =========================================================================

    def contractDetails(self, reqId: int, contractDetails):
        """Captures conId from contract details response."""
        symbol = self._req_to_symbol.get(reqId, "")
        if symbol and contractDetails.contract.secType == "STK":
            con_id = contractDetails.contract.conId
            self._con_id_cache[symbol] = con_id
            logger.info(f"MarketDataClient: Resolved conId={con_id} for {symbol}")

    def contractDetailsEnd(self, reqId: int):
        """Signals contract details lookup is complete — unblocks waiting thread."""
        symbol = self._req_to_symbol.get(reqId, "")
        if symbol in self._con_id_events:
            self._con_id_events[symbol].set()

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

    def securityDefinitionOptionParameter(self, reqId: int, exchange: str,
                                           underlyingConId: int, tradingClass: str,
                                           multiplier: str, expirations: set,
                                           strikes: set):
        """Callback for reqSecDefOptParams — populates option chain data.

        IBKR sends one callback per (exchange, tradingClass) combination.
        For SPY this means separate callbacks for "SPY" (monthly) and "SPYW" (weekly).
        We capture the tradingClass per expiry so _build_option_contract can use
        the correct value — critical for avoiding Code 200 on weekly contracts.
        """
        # Only capture SMART exchange to avoid duplicates from multiple venues
        if exchange != "SMART":
            return

        symbol = self._req_to_symbol.get(reqId, "")

        # Cache tradingClass per (symbol, expiry) — e.g. ("SPY", "20260626") -> "SPYW"
        for expiry in expirations:
            key = (symbol, expiry)
            # Don't overwrite a monthly ("SPY") entry with a weekly ("SPYW") if both arrive
            # Monthly class matches symbol exactly — prefer it as the canonical entry
            if key not in self._trading_class_cache or tradingClass == symbol:
                self._trading_class_cache[key] = tradingClass

        chain = self.market_data.get(reqId, {})
        existing_expirations = set(chain.get("expirations", []))
        existing_strikes = set(chain.get("strikes", []))

        existing_expirations.update(expirations)
        existing_strikes.update(strikes)

        self.market_data[reqId] = {
            "exchange": exchange,
            "trading_class": tradingClass,
            "multiplier": multiplier,
            "expirations": sorted(existing_expirations),
            "strikes": sorted(existing_strikes),
        }

    def securityDefinitionOptionParameterEnd(self, reqId: int):
        """Signals that all option chain data has been delivered."""
        chain = self.market_data.get(reqId, {})
        symbol = self._req_to_symbol.get(reqId, "UNKNOWN")
        print(f"\n📋 Option Chain Complete for {symbol}! "
              f"{len(chain.get('expirations', []))} expiries, "
              f"{len(chain.get('strikes', []))} strikes.")

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib):
        """
        Handles live price ticks from reqMktData.

        Tick types used:
          1  = bid price
          2  = ask price
          24 = implied volatility (IV) for the underlying

        Routes data to the appropriate waiting event:
          - IV data    → _iv_data / _iv_events
          - Option bid/ask → _option_price_data / _option_price_events
        """
        # Tick 106 = impvolat (IV) for STK underlying — tick 24 is futures/options only
        if tickType == 106 and price > 0:
            iv_events = getattr(self, "_iv_events", {})
            iv_data   = getattr(self, "_iv_data",   {})
            if reqId in iv_events:
                iv_data[reqId] = price
                iv_events[reqId].set()
                return

        # Live:    tick 1 = bid,         tick 2 = ask
        # Delayed: tick 66 = delayed bid, tick 67 = delayed ask
        BID_TICKS = {1, 66}
        ASK_TICKS = {2, 67}

        option_events = getattr(self, "_option_price_events", {})
        option_data   = getattr(self, "_option_price_data",   {})
        if reqId in option_events and price > 0:
            # Always capture full quote so callers reading _option_quote_data get both sides
            quote_data = getattr(self, "_option_quote_data", {})
            if reqId not in quote_data:
                quote_data[reqId] = {}
            if tickType in BID_TICKS:
                quote_data[reqId]["bid"] = price
            elif tickType in ASK_TICKS:
                quote_data[reqId]["ask"] = price
            self._option_quote_data = quote_data

            # Signal event for whichever side this req ID was registered for
            entry = option_events[reqId]
            side  = entry.get("side", "")
            if (side == "bid" and tickType in BID_TICKS) or \
               (side == "ask" and tickType in ASK_TICKS):
                option_data[reqId] = price
                entry["event"].set()

    def tickSize(self, reqId: int, tickType: int, size: int):
        """
        Handles size ticks from reqMktData.

        Tick type 22 = open interest for option contracts.
        Signals _option_oi_events[reqId] when OI arrives so callers can block on it.
        """
        if tickType == 22 and size >= 0:
            oi_data = getattr(self, "_option_oi_data",   {})
            oi_data[reqId] = size
            self._option_oi_data = oi_data

            oi_events = getattr(self, "_option_oi_events", {})
            if reqId in oi_events:
                oi_events[reqId].set()

    def error(self, id: int, errorCode: int, errorString: str):
        print(f"\n⚠️ IBKR GATEWAY MESSAGE [ID {id}] | Code {errorCode}: {errorString}")
