#!/usr/bin/env python3
import argparse
import json
import os
import re
from datetime import datetime

BUY_RE = re.compile(r"BUY\s+(?P<symbol>[A-Z]+USDT)\s+~(?P<qty>[0-9.]+)\s+@\s+~(?P<price>[0-9.]+)")


def load_ledger(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(description="Build entry_ledger.json from BUY lines in bot.log")
    parser.add_argument("--log", default="bot.log", help="Path to bot.log")
    parser.add_argument("--out", default="entry_ledger.json", help="Output ledger path")
    parser.add_argument("--merge", action="store_true", help="Merge into existing ledger if file exists")
    args = parser.parse_args()

    if not os.path.exists(args.log):
        print(f"Log file not found: {args.log}")
        return

    ledger = load_ledger(args.out) if args.merge else {}

    with open(args.log, "r", encoding="utf-8", errors="ignore") as f:
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

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(ledger)} symbols to {args.out}")


if __name__ == "__main__":
    main()
