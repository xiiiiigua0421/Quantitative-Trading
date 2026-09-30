"""Small, mockable Alpaca client with a permanently paper-only trading URL.

Official endpoint references:
https://docs.alpaca.markets/us/docs/working-with-orders
https://docs.alpaca.markets/us/reference/legacycalendar
https://docs.alpaca.markets/us/reference/legacyclock
https://docs.alpaca.markets/us/reference/stocksnapshotsingle
"""

from __future__ import annotations

import json
import os
import re
import socket
from typing import Any
from urllib import error, parse, request

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


class BrokerError(RuntimeError):
    """A deliberately sanitized broker failure; never includes headers/body."""


class BrokerHTTPError(BrokerError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Alpaca returned HTTP {status}; inspect the paper dashboard.")


class BrokerUnavailable(BrokerError):
    """A request failed without a trustworthy response."""


class _NoRedirect(request.HTTPRedirectHandler):
    # Authentication headers must never follow an unexpected redirect.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AlpacaPaperBroker:
    """Authenticated reads and explicit PAPER orders. There is no live switch.

    ``opener`` may be injected for offline tests. Credentials default to
    APCA_API_KEY_ID / APCA_API_SECRET_KEY, and are never put into exception text.
    """

    def __init__(self, *, timeout: float = 15.0, key_id: str | None = None,
                 secret_key: str | None = None, opener=None):
        self._key_id = key_id or os.environ.get("APCA_API_KEY_ID", "")
        self._secret_key = secret_key or os.environ.get("APCA_API_SECRET_KEY", "")
        if not self._key_id or not self._secret_key:
            raise BrokerError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for your Alpaca PAPER account.")
        if not 0 < timeout <= 60:
            raise ValueError("Broker timeout must be between 0 and 60 seconds.")
        self.timeout = timeout
        self._opener = opener or request.build_opener(_NoRedirect())

    def request(self, method: str, path: str, *, params: dict | None = None,
                payload: dict | None = None, data_api: bool = False) -> Any:
        """Request a relative endpoint; market-data requests are read-only."""
        method = method.upper()
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValueError("Use an absolute API path without host or query string.")
        if method not in {"GET", "POST"} or (data_api and method != "GET"):
            raise ValueError("Only GET reads and paper POST orders are supported.")
        if method == "POST" and path != "/v2/orders":
            raise ValueError("Only the PAPER order endpoint accepts POST requests.")
        base = DATA_BASE_URL if data_api else PAPER_BASE_URL
        required_host = "data.alpaca.markets" if data_api else "paper-api.alpaca.markets"
        if parse.urlsplit(base).scheme != "https" or parse.urlsplit(base).netloc != required_host:
            raise BrokerError("Unexpected API host; request refused.")
        url = base + path
        if params:
            url += "?" + parse.urlencode({k: v for k, v in params.items() if v is not None})
        encoded = json.dumps(payload, allow_nan=False).encode("utf-8") if payload is not None else None
        req = request.Request(url, data=encoded, method=method, headers={
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            open_request = self._opener.open if hasattr(self._opener, "open") else self._opener
            with open_request(req, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except error.HTTPError as exc:
            raise BrokerHTTPError(exc.code) from None
        except (error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            raise BrokerUnavailable("Alpaca request did not return a reliable response.") from None
        except (ValueError, UnicodeDecodeError):
            raise BrokerError("Alpaca returned invalid JSON.") from None
        if not isinstance(result, (dict, list)):
            raise BrokerError("Alpaca returned an unexpected JSON shape.")
        return result

    def account(self) -> dict:
        return self.request("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self.request("GET", "/v2/positions")

    def orders(self, status: str = "open") -> list[dict]:
        if status not in {"open", "closed", "all"}:
            raise ValueError("Invalid order status filter.")
        result = self.request("GET", "/v2/orders", params={"status": status, "limit": 500, "nested": "true"})
        if not isinstance(result, list) or len(result) >= 500:
            raise BrokerError("Order listing is malformed or may be truncated; refused.")
        return result

    def get_order(self, client_order_id: str) -> dict | None:
        try:
            return self.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        except BrokerHTTPError as exc:
            if exc.status == 404:
                return None
            raise

    def submit_order(self, *, symbol: str, qty: int, side: str,
                     client_order_id: str) -> dict:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ValueError("Only positive whole-share quantities are supported.")
        if side not in {"buy", "sell"}:
            raise ValueError("Order side must be buy or sell.")
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
            raise ValueError("Invalid US stock symbol.")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", client_order_id):
            raise ValueError("Invalid client order ID.")
        return self.request("POST", "/v2/orders", payload={
            "symbol": symbol, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day",
            "extended_hours": False, "client_order_id": client_order_id,
        })

    def calendar(self, start: str, end: str) -> list[dict]:
        return self.request("GET", "/v2/calendar", params={"start": start, "end": end})

    def clock(self) -> dict:
        return self.request("GET", "/v2/clock")

    def snapshot(self, symbol: str, feed: str = "iex") -> dict:
        if feed not in {"iex", "sip"}:
            raise ValueError("Paper execution needs a real-time IEX or SIP feed.")
        return self.request("GET", f"/v2/stocks/{parse.quote(symbol, safe='')}/snapshot",
                            params={"feed": feed}, data_api=True)

    def market_data(self, path: str, params: dict | None = None):
        """Read-only historical bars/corporate-action adapter for the data layer."""
        return self.request("GET", path, params=params, data_api=True)
