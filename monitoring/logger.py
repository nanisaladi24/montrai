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


def log_trade(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    reason: str,
    regime: str = "unknown",
    order_id: str = "",
):
    Path(LOG_DIR).mkdir(exist_ok=True)
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "side", "quantity", "price", "value_usd", "reason", "regime", "order_id"])
        writer.writerow([
            datetime.utcnow().isoformat(),
            symbol, side, quantity, price,
            round(quantity * price, 2),
            reason, regime, order_id,
        ])
