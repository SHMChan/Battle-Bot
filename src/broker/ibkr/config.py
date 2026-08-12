import os

PAPER_PORT = 4002
LIVE_PORT  = 4001

# Set IBKR_ENV=live for production. Anything else (or unset) defaults to paper.
_env = os.getenv("IBKR_ENV", "paper").strip().lower()
IS_LIVE = _env == "live"

PORT = LIVE_PORT if IS_LIVE else PAPER_PORT
