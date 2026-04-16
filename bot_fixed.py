import os
import time
import json
import sqlite3
import logging
import re
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

import requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import anthropic

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
ENTRY_LEDGER_FILE = "entry_ledger.json"
POSITIONS_FILE = "positions.json"
BUY_RE = re.compile(r"BUY\s+(?P<symbol>[A-Z]+USDT)\s+~(?P<qty>[0-9.]+)\s+@\s+~(?P<price>[0-9.]+)")

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "TRXUSDT"
]
INTERVAL = "60"
CANDLES_LIMIT = 100
MAX_TRADE_USD = 10.0
STOP_LOSS_PCT = 0.015
MIN_TP_PCT = 0.02
MAX_POSITIONS = 6
LOOP_INTERVAL = 60 * 30
FEE_BUFFER = 0.997

bybit = HTTP(
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
    testnet=False,
)
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_symbol_rules_cache = {}


def tg(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def _to_decimal(v) -> Decimal:
    return Decimal(str(v))


def floor_to_step(value: float, step: str) -> float:
    d_value = _to_decimal(value)
    d_step = _to_decimal(step)
    if d_step <= 0:
        return float(d_value)
    return float((d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)


def get_symbol_rules(symbol: str):
    if symbol in _symbol_rules_cache:
        return _symbol_rules_cache[symbol]
    resp = bybit.get_instruments_info(category="spot", symbol=symbol)
    info = resp["result"]["list"][0]
    lot = info.get("lotSizeFilter", {})
    price = info.get("priceFilter", {})
    rules = {
        "qty_step": lot.get("basePrecision") or lot.get("qtyStep") or "0.00000001",
        "min_order_qty": float(lot.get("minOrderQty") or 0),
        "tick_size": price.get("tickSize") or "0.00000001",
    }
    _symbol_rules_cache[symbol] = rules
    return rules


def fmt_qty(symbol: str, qty: float) -> float:
    r = get_symbol_rules(symbol)
    return floor_to_step(qty, r["qty_step"])


def fmt_price(symbol: str, price: float) -> float:
    r = get_symbol_rules(symbol)
    return floor_to_step(price, r["tick_size"])


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_positions():
    return load_json(POSITIONS_FILE, {})


def save_positions(positions):
    save_json(POSITIONS_FILE, positions)


def load_ledger():
    return load_json(ENTRY_LEDGER_FILE, {})


def save_ledger(ledger):
    save_json(ENTRY_LEDGER_FILE, ledger)


def save_entry(symbol: str, entry_price: float, qty: float):
    ledger = load_ledger()
    ledger[symbol] = {
        "entry_price": float(entry_price),
        "qty": float(qty),
        "ts": datetime.now().isoformat(),
    }
    save_ledger(ledger)


def build_ledger_from_log(log_path: str = "bot.log"):
    if not os.path.exists(log_path):
        return {}
    ledger = load_ledger()
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = BUY_RE.search(line)
                if not m:
                    continue
                symbol = m.group("symbol")
                ledger[symbol] = {
                    "entry_price": float(m.group("price")),
                    "qty": float(m.group("qty")),
                    "ts": datetime.now().isoformat(),
                    "source": "log",
                }
        save_ledger(ledger)
    except Exception as e:
        log.warning(f"Failed building ledger from log: {e}")
    return ledger


def init_db():
    conn = sqlite3.connect("trades.db")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, action TEXT, symbol TEXT,
            price REAL, qty REAL, usd_value REAL,
            stop_loss REAL, take_profit REAL,
            reason TEXT, order_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_trade(action, symbol, price, qty, sl, tp, reason, order_id=""):
    conn = sqlite3.connect("trades.db")
    conn.execute(
        """
        INSERT INTO trades (ts, action, symbol, price, qty, usd_value, stop_loss, take_profit, reason, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (datetime.now().isoformat(), action, symbol, price, qty, round(price * qty, 4), sl, tp, reason, order_id),
    )
    conn.commit()
    conn.close()


def get_wallet_positions():
    resp = bybit.get_wallet_balance(accountType="UNIFIED")
    coins = resp["result"]["list"][0]["coin"]
    out = {}
    for c in coins:
        coin = c["coin"]
        if coin == "USDT":
            continue
        symbol = f"{coin}USDT"
        if symbol not in SYMBOLS:
            continue
        qty = float(c["walletBalance"])
        if qty > 0:
            out[symbol] = qty
    return out


def get_coin_balance(symbol: str) -> float:
    coin = symbol.replace("USDT", "")
    resp = bybit.get_wallet_balance(accountType="UNIFIED")
    coins = resp["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == coin:
            return float(c["walletBalance"])
    return 0.0


def get_price(symbol: str) -> float:
    resp = bybit.get_tickers(category="spot", symbol=symbol)
    return float(resp["result"]["list"][0]["lastPrice"])


def get_open_tp_sl(symbol: str):
    tp_id = sl_id = None
    tp = sl = None
    resp = bybit.get_open_orders(category="spot", symbol=symbol)
    for o in resp["result"]["list"]:
        if o.get("side") != "Sell":
            continue
        if o.get("orderType") == "Limit":
            tp_id = o["orderId"]
            tp = float(o["price"])
        elif o.get("orderType") == "Market" and float(o.get("triggerPrice", 0) or 0) > 0:
            sl_id = o["orderId"]
            sl = float(o["triggerPrice"])
    return tp_id, sl_id, tp, sl


def place_with_retry(payload: dict, retries: int = 3, pause: float = 0.8):
    err = None
    for _ in range(retries):
        try:
            return bybit.place_order(**payload), None
        except Exception as e:
            err = e
            time.sleep(pause)
    return None, err


def ensure_protection(symbol: str, qty: float, entry_price: float, existing: dict | None = None):
    rules = get_symbol_rules(symbol)
    qty_for_orders = fmt_qty(symbol, qty * FEE_BUFFER)
    if qty_for_orders < rules["min_order_qty"]:
        return {"tp_order_id": None, "sl_order_id": None, "take_profit": None, "stop_loss": None}

    tp = fmt_price(symbol, entry_price * (1 + MIN_TP_PCT))
    sl = fmt_price(symbol, entry_price * (1 - STOP_LOSS_PCT))

    cur = existing or {}
    tp_id, sl_id = cur.get("tp_order_id"), cur.get("sl_order_id")

    if not tp_id:
        payload = {
            "category": "spot", "symbol": symbol,
            "side": "Sell", "orderType": "Limit",
            "qty": str(qty_for_orders), "price": str(tp), "timeInForce": "GTC",
        }
        res, err = place_with_retry(payload)
        if err:
            log.error(f"TP order error {symbol}: {err}")
        else:
            tp_id = res["result"]["orderId"]

    if not sl_id:
        payload = {
            "category": "spot", "symbol": symbol,
            "side": "Sell", "orderType": "Market",
            "qty": str(qty_for_orders), "triggerPrice": str(sl), "orderFilter": "StopOrder",
        }
        res, err = place_with_retry(payload)
        if err:
            log.error(f"SL order error {symbol}: {err}")
        else:
            sl_id = res["result"]["orderId"]

    return {
        "tp_order_id": tp_id,
        "sl_order_id": sl_id,
        "take_profit": tp,
        "stop_loss": sl,
    }


def place_buy(symbol: str, entry_price: float):
    try:
        resp = bybit.place_order(
            category="spot", symbol=symbol,
            side="Buy", orderType="Market",
            marketUnit="quoteCoin", qty=str(MAX_TRADE_USD),
        )
        buy_id = resp["result"]["orderId"]
        time.sleep(2)

        # use factual wallet qty, not estimated qty
        qty = get_coin_balance(symbol)
        if qty <= 0:
            log.error(f"BUY {symbol} no coin balance after order {buy_id}")
            return None

        protect = ensure_protection(symbol, qty, entry_price)
        save_entry(symbol, entry_price, qty)

        return {
            "qty": qty,
            "entry_price": entry_price,
            "stop_loss": protect["stop_loss"],
            "take_profit": protect["take_profit"],
            "tp_order_id": protect["tp_order_id"],
            "sl_order_id": protect["sl_order_id"],
        }
    except Exception as e:
        log.error(f"BUY error {symbol}: {e}")
        return None


def boot_positions_from_exchange():
    positions = {}
    ledger = load_ledger()
    wallet = get_wallet_positions()

    for symbol, qty in wallet.items():
        price = get_price(symbol)
        tp_id, sl_id, tp, sl = get_open_tp_sl(symbol)

        entry = ledger.get(symbol, {}).get("entry_price", price)
        cur = {
            "qty": qty,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "tp_order_id": tp_id,
            "sl_order_id": sl_id,
        }
        if not tp_id or not sl_id:
            fixed = ensure_protection(symbol, qty, entry, cur)
            cur.update(fixed)

        positions[symbol] = cur
        save_entry(symbol, entry, qty)

    save_positions(positions)
    return positions


def can_open_new_position(positions: dict, symbol: str) -> bool:
    if symbol in positions:
        return False
    return len(positions) < MAX_POSITIONS


def main():
    init_db()
    if not os.path.exists(ENTRY_LEDGER_FILE):
        build_ledger_from_log("bot.log")
    positions = boot_positions_from_exchange()
    log.info(f"Bot started | positions: {len(positions)}")

    while True:
        try:
            # heal missing protection each cycle
            for symbol, pos in list(positions.items()):
                if not pos.get("tp_order_id") or not pos.get("sl_order_id"):
                    fixed = ensure_protection(symbol, pos["qty"], pos["entry_price"], pos)
                    pos.update(fixed)
                    positions[symbol] = pos

            # simplified signal stub: replace with your Claude logic
            for symbol in SYMBOLS:
                if not can_open_new_position(positions, symbol):
                    continue

                # placeholder condition; keep your real decision rules here
                should_buy = False
                if should_buy:
                    price = get_price(symbol)
                    res = place_buy(symbol, price)
                    if res:
                        positions[symbol] = res
                        log_trade("BUY", symbol, price, res["qty"], res["stop_loss"], res["take_profit"], "signal")

            save_positions(positions)

        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            tg(f"⚠️ Bot error: {e}")

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    main()
