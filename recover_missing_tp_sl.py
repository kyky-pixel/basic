#!/usr/bin/env python3
import argparse
import json
import os
import re
import time
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

STOP_LOSS_PCT = 0.015
MIN_TP_PCT = 0.02
FEE_BUFFER = 0.997  # reserve for fee/slippage so sell qty <= available balance
BUY_RE = re.compile(r"BUY\s+(?P<symbol>[A-Z]+USDT)\s+~(?P<qty>[0-9.]+)\s+@\s+~(?P<price>[0-9.]+)")


def _to_decimal(v) -> Decimal:
    return Decimal(str(v))


def floor_to_step(value: float, step: str) -> float:
    d_value = _to_decimal(value)
    d_step = _to_decimal(step)
    if d_step <= 0:
        return float(d_value)
    floored = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    return float(floored)


def parse_log(log_path: str):
    latest = {}
    if not os.path.exists(log_path):
        return latest
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = BUY_RE.search(line)
            if m:
                latest[m.group("symbol")] = {
                    "entry_price": float(m.group("price")),
                    "buy_qty_hint": float(m.group("qty")),
                    "source": "log",
                }
    return latest


def load_ledger(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = {}
        for symbol, entry in data.items():
            if isinstance(entry, dict) and "entry_price" in entry:
                out[symbol] = {
                    "entry_price": float(entry["entry_price"]),
                    "source": "ledger",
                }
        return out
    except Exception:
        return {}


def get_wallet_positions(bybit: HTTP):
    resp = bybit.get_wallet_balance(accountType="UNIFIED")
    coins = resp["result"]["list"][0]["coin"]
    found = {}
    for c in coins:
        coin = c["coin"]
        if coin == "USDT":
            continue
        qty = float(c["walletBalance"])
        if qty <= 0:
            continue
        found[f"{coin}USDT"] = qty
    return found


def resolve_entry_price(symbol: str, ledger: dict, log_entries: dict):
    if symbol in ledger:
        return ledger[symbol]["entry_price"], ledger[symbol]["source"]
    if symbol in log_entries:
        return log_entries[symbol]["entry_price"], log_entries[symbol]["source"]
    return None, "none"


def get_symbol_rules(bybit: HTTP, symbol: str):
    resp = bybit.get_instruments_info(category="spot", symbol=symbol)
    info = resp["result"]["list"][0]
    lot = info.get("lotSizeFilter", {})
    price = info.get("priceFilter", {})
    return {
        "qty_step": lot.get("basePrecision") or lot.get("qtyStep") or "0.00000001",
        "min_order_qty": lot.get("minOrderQty") or "0",
        "tick_size": price.get("tickSize") or "0.00000001",
    }


def round_qty_price(qty: float, entry_price: float, rules: dict):
    qty_safe = qty * FEE_BUFFER
    qty_final = floor_to_step(qty_safe, rules["qty_step"])
    min_order_qty = float(rules["min_order_qty"])
    if qty_final < min_order_qty:
        return None, None, None

    sl_raw = entry_price * (1 - STOP_LOSS_PCT)
    tp_raw = entry_price * (1 + MIN_TP_PCT)
    sl = floor_to_step(sl_raw, rules["tick_size"])
    tp = floor_to_step(tp_raw, rules["tick_size"])
    return qty_final, sl, tp


def get_open_sell_orders(bybit: HTTP, symbol: str):
    tp_exists = sl_exists = False
    resp = bybit.get_open_orders(category="spot", symbol=symbol)
    for o in resp["result"]["list"]:
        if o.get("side") != "Sell":
            continue
        if o.get("orderType") == "Limit":
            tp_exists = True
        if o.get("orderType") == "Market" and float(o.get("triggerPrice", 0) or 0) > 0:
            sl_exists = True
    return tp_exists, sl_exists


def place_with_retry(bybit: HTTP, payload: dict, retries: int = 3, pause: float = 0.7):
    err = None
    for _ in range(retries):
        try:
            return bybit.place_order(**payload), None
        except Exception as e:
            err = e
            time.sleep(pause)
    return None, err


def place_protection(bybit: HTTP, symbol: str, qty: float, entry_price: float, execute: bool):
    rules = get_symbol_rules(bybit, symbol)
    qty_for_orders, sl, tp = round_qty_price(qty, entry_price, rules)
    if qty_for_orders is None:
        return [f"SKIP {symbol}: qty too small after step/minOrderQty checks"]

    tp_exists, sl_exists = get_open_sell_orders(bybit, symbol)
    actions = []

    if not tp_exists:
        payload = {
            "category": "spot", "symbol": symbol,
            "side": "Sell", "orderType": "Limit",
            "qty": str(qty_for_orders), "price": str(tp), "timeInForce": "GTC",
        }
        actions.append(f"TP {symbol} qty={qty_for_orders} price={tp}")
        if execute:
            _, err = place_with_retry(bybit, payload)
            if err:
                actions.append(f"TP_ERR {symbol}: {err}")

    if not sl_exists:
        payload = {
            "category": "spot", "symbol": symbol,
            "side": "Sell", "orderType": "Market",
            "qty": str(qty_for_orders), "triggerPrice": str(sl), "orderFilter": "StopOrder",
        }
        actions.append(f"SL {symbol} qty={qty_for_orders} trigger={sl}")
        if execute:
            _, err = place_with_retry(bybit, payload)
            if err:
                actions.append(f"SL_ERR {symbol}: {err}")

    return actions if actions else [f"OK {symbol}: TP/SL already exist"]


def main():
    parser = argparse.ArgumentParser(description="Restore missing TP/SL orders using saved entry prices")
    parser.add_argument("--log", default="bot.log", help="Path to bot.log")
    parser.add_argument("--ledger", default="entry_ledger.json", help="Path to saved entry prices JSON")
    parser.add_argument("--execute", action="store_true", help="Actually place orders")
    args = parser.parse_args()

    load_dotenv()
    bybit = HTTP(
        api_key=os.getenv("BYBIT_API_KEY"),
        api_secret=os.getenv("BYBIT_API_SECRET"),
        testnet=False,
    )

    ledger_entries = load_ledger(args.ledger)
    log_entries = parse_log(args.log)
    wallet_positions = get_wallet_positions(bybit)

    if not wallet_positions:
        print("No wallet spot positions found.")
        return

    for symbol, qty in wallet_positions.items():
        entry_price, source = resolve_entry_price(symbol, ledger_entries, log_entries)
        if not entry_price:
            print(f"SKIP {symbol}: no entry_price in ledger/log (save buys first)")
            continue

        print(f"INFO {symbol}: entry={entry_price} source={source} qty={qty}")
        actions = place_protection(bybit, symbol, qty, entry_price, execute=args.execute)
        for a in actions:
            print(("EXEC" if args.execute else "DRY"), "|", a)


if __name__ == "__main__":
    main()
