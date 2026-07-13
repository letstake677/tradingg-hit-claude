"""
Bitget API v2 client for Kehlo Trading.

Covers what the bot needs: candles, account/positions, leverage, order
placement, and individual TP/SL plan orders (used to place MULTIPLE TP
levels on one open position, one call per level).

Endpoints below are taken directly from Bitget's official v2 API docs
(bitget.com/api-doc) — auth scheme, candle format, place-order body, and
place-tpsl-order body are all confirmed against live documentation examples.

SECURITY — read this before anything else:
  - NEVER commit real keys to git or paste them into a chat.
  - Load credentials from environment variables only (see .env.example).
  - Create the API key with "Trade" + "Read" permission ONLY.
    Never tick "Withdraw" permission for a trading-bot key.
  - Whitelist your server's IP in the Bitget API key settings.
  - For demo/paper trading you need a SEPARATE Demo API Key, created
    while your Bitget account is switched to Demo mode
    (Futures -> Demo Trading -> Personal Center -> API Key Management).
    Live keys will not work in demo mode and vice versa.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Optional

import requests

BASE_URL = "https://api.bitget.com"


@dataclass
class BitgetCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    demo: bool = True  # True -> sends the 'paptrading' header (demo/paper trading)


class BitgetAPIError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Bitget API error {code}: {message}")


class BitgetClient:
    def __init__(self, creds: BitgetCredentials):
        self.creds = creds
        self.session = requests.Session()

    # ---------------- signing ----------------

    @staticmethod
    def _timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.creds.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, request_path_with_query: str, body: str = "") -> dict:
        ts = self._timestamp_ms()
        sign = self._sign(ts, method, request_path_with_query, body)
        headers = {
            "ACCESS-KEY": self.creds.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.creds.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        if self.creds.demo:
            headers["paptrading"] = "1"
        return headers

    # ---------------- core request ----------------

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None):
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            query = "?" + "&".join(f"{k}={v}" for k, v in clean.items())
        body_str = json.dumps(body) if body else ""
        full_path = f"{path}{query}"
        headers = self._headers(method, full_path, body_str)

        resp = self.session.request(
            method,
            BASE_URL + full_path,
            headers=headers,
            data=body_str if body else None,
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise BitgetAPIError("HTTP_" + str(resp.status_code), resp.text[:200])

        if data.get("code") != "00000":
            raise BitgetAPIError(data.get("code", "unknown"), data.get("msg", "unknown error"))
        return data.get("data")

    # ---------------- market data ----------------

    def get_candles(self, symbol: str, granularity: str, product_type: str = "usdt-futures",
                     limit: int = 100) -> list:
        """
        Returns a list of [ts_ms, open, high, low, close, base_volume, quote_volume]
        as strings, oldest candle first.
        granularity examples: 1m, 5m, 15m, 30m, 1H, 4H, 1D
        """
        return self._request("GET", "/api/v2/mix/market/candles", params={
            "symbol": symbol,
            "granularity": granularity,
            "productType": product_type,
            "limit": limit,
        })

    def get_contract_config(self, symbol: str, product_type: str = "usdt-futures") -> dict:
        """
        Per-symbol precision and limits — volumePlace (decimal places the
        size/quantity must be rounded to), pricePlace, sizeMultiplier (the
        actual step increment), minTradeNum. Every symbol has DIFFERENT
        precision (e.g. BTCUSDT wants 2 decimals, DOGEUSDT wants 0) —
        sending a size with the wrong number of decimals is rejected with
        error 40808. Public data, cache this per symbol rather than
        fetching on every order.
        """
        data = self._request("GET", "/api/v2/mix/market/contracts",
                              params={"symbol": symbol, "productType": product_type})
        if isinstance(data, list) and data:
            return data[0]
        raise BitgetAPIError("NO_CONTRACT_CONFIG", f"No contract config returned for {symbol}")

    def get_history_candles(self, symbol: str, granularity: str,
                             product_type: str = "usdt-futures", limit: int = 200) -> list:
        """Deeper history than get_candles — useful for backtesting."""
        return self._request("GET", "/api/v2/mix/market/history-candles", params={
            "symbol": symbol,
            "granularity": granularity,
            "productType": product_type,
            "limit": limit,
        })

    # ---------------- account ----------------

    def get_accounts(self, product_type: str = "USDT-FUTURES") -> list:
        return self._request("GET", "/api/v2/mix/account/accounts", params={
            "productType": product_type,
        })

    def get_positions(self, product_type: str = "USDT-FUTURES", margin_coin: str = "USDT") -> list:
        return self._request("GET", "/api/v2/mix/position/all-position", params={
            "productType": product_type,
            "marginCoin": margin_coin,
        })

    def set_leverage(self, symbol: str, leverage: str, product_type: str = "USDT-FUTURES",
                      margin_coin: str = "USDT", hold_side: Optional[str] = None) -> dict:
        body = {
            "symbol": symbol,
            "productType": product_type,
            "marginCoin": margin_coin,
            "leverage": leverage,
        }
        if hold_side:
            body["holdSide"] = hold_side
        return self._request("POST", "/api/v2/mix/account/set-leverage", body=body)

    # ---------------- trading ----------------

    def place_order(self, symbol: str, side: str, trade_side: str, order_type: str, size: str,
                     price: Optional[str] = None, product_type: str = "USDT-FUTURES",
                     margin_coin: str = "USDT", margin_mode: str = "isolated",
                     force: str = "gtc", client_oid: Optional[str] = None) -> dict:
        """
        side: 'buy' or 'sell'
        trade_side: 'open' or 'close'
        order_type: 'limit' or 'market'
        (open long = side buy + tradeSide open; open short = side sell + tradeSide open)
        """
        body = {
            "symbol": symbol,
            "productType": product_type,
            "marginMode": margin_mode,
            "marginCoin": margin_coin,
            "size": size,
            "side": side,
            "tradeSide": trade_side,
            "orderType": order_type,
            "force": force,
        }
        if price:
            body["price"] = price
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/mix/order/place-order", body=body)

    def place_tpsl_leg(self, symbol: str, plan_type: str, trigger_price: str, hold_side: str,
                        size: str, product_type: str = "usdt-futures", margin_coin: str = "USDT",
                        trigger_type: str = "mark_price", client_oid: Optional[str] = None) -> dict:
        """
        ONE take-profit or stop-loss leg tied to an open position.
        Call this once per TP level (TP1/TP2/TP3) with a partial `size` each
        time, plus once more with plan_type='loss_plan' for the full-size SL.
        This is how multiple TP levels are built on Bitget.

        plan_type: 'profit_plan' (take-profit) or 'loss_plan' (stop-loss)
        hold_side: 'long' or 'short'
        """
        body = {
            "marginCoin": margin_coin,
            "productType": product_type,
            "symbol": symbol,
            "planType": plan_type,
            "triggerPrice": trigger_price,
            "triggerType": trigger_type,
            "executePrice": "0",  # 0 => executes as a market order once triggered
            "holdSide": hold_side,
            "size": size,
        }
        if client_oid:
            body["clientOid"] = client_oid
        return self._request("POST", "/api/v2/mix/order/place-tpsl-order", body=body)

    def close_position(self, symbol: str, product_type: str = "USDT-FUTURES",
                        margin_coin: str = "USDT", hold_side: Optional[str] = None) -> dict:
        body = {"symbol": symbol, "productType": product_type, "marginCoin": margin_coin}
        if hold_side:
            body["holdSide"] = hold_side
        return self._request("POST", "/api/v2/mix/order/close-positions", body=body)

    def cancel_tpsl_order(self, symbol: str, order_id: str, product_type: str = "USDT-FUTURES",
                           margin_coin: str = "USDT") -> dict:
        """Cancels a single plan/TP-SL order — used to pull the old SL before
        placing a fresh one at breakeven."""
        body = {
            "symbol": symbol,
            "productType": product_type,
            "marginCoin": margin_coin,
            "orderIdList": [{"orderId": order_id}],
        }
        return self._request("POST", "/api/v2/mix/order/cancel-plan-order", body=body)

    def get_history_positions(self, symbol: Optional[str] = None, product_type: str = "USDT-FUTURES",
                               limit: int = 20) -> list:
        """
        Closed-position records with the ACTUAL realised PnL (`netProfit`,
        net of fees/funding) — order placement responses don't carry this,
        so this is the only accurate source for what a closed trade really
        made or lost.
        """
        params = {"productType": product_type, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/api/v2/mix/position/history-position", params=params)
        if isinstance(data, dict):
            return data.get("list", [])
        return data or []
