import logging
import os
import csv
from datetime import datetime
from pathlib import Path
from config.settings import LOG_DIR, TRADE_LOG_FILE


def get_logger(name: str) -> logging.Logger:
    Path(LOG_DIR).mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    fh = logging.FileHandler(f"{LOG_DIR}/{name}.log")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


_TRADE_HEADER = [
    "timestamp", "symbol", "side", "quantity", "price", "value_usd",
    "reason", "regime", "order_id", "strategy",
]


def log_trade(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    reason: str,
    regime: str = "unknown",
    order_id: str = "",
    strategy: str = "",
):
    Path(LOG_DIR).mkdir(exist_ok=True)
    _ensure_trade_header()
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcnow().isoformat(),
            symbol, side, quantity, price,
            round(quantity * price, 2),
            reason, regime, order_id, strategy,
        ])


def _ensure_trade_header():
    """Create the file with the canonical header if missing; upgrade the
    header in place if an older 9-column file is present so mixed writes
    don't misalign columns."""
    if not os.path.isfile(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(_TRADE_HEADER)
        return
    # Fast check: read only the first line
    with open(TRADE_LOG_FILE, "r", newline="") as f:
        first = f.readline().strip()
    current = first.split(",") if first else []
    if current == _TRADE_HEADER:
        return
    # Upgrade: rewrite file with new header, padding existing rows
    with open(TRADE_LOG_FILE, "r", newline="") as f:
        rows = list(csv.reader(f))
    body = rows[1:] if rows else []
    padded = [(r + [""] * (len(_TRADE_HEADER) - len(r)))[: len(_TRADE_HEADER)] for r in body]
    with open(TRADE_LOG_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_TRADE_HEADER)
        w.writerows(padded)
