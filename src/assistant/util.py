"""Logging (with secret redaction), time helpers and retry."""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Callable, TypeVar

from .config import LOG_DIR, load_config, load_secrets

T = TypeVar("T")
log = logging.getLogger("assistant")


class RedactSecrets(logging.Filter):
    """Removes secrets (bot token, API key) from every log record."""

    def __init__(self) -> None:
        super().__init__()
        s = load_secrets()
        self.secrets = [x for x in (s.telegram_bot_token, s.anthropic_api_key, s.kite_api_secret) if x]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            red = redact(msg, self.secrets)
            if red != msg:
                record.msg, record.args = red, None
        return True


def redact(text: str, secrets: list[str] | None = None) -> str:
    if secrets is None:
        s = load_secrets()
        secrets = [x for x in (s.telegram_bot_token, s.anthropic_api_key, s.kite_api_secret) if x]
    for sec in secrets:
        text = text.replace(sec, "***")
    return text


_configured = False


def setup_logging(name: str = "assistant", verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    flt = RedactSecrets()
    for h in (fh, sh):
        h.addFilter(flt)
        root.addHandler(h)
    # httpx logs full URLs, which contain the bot token.
    for noisy in ("httpx", "httpcore", "telegram.ext", "yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ist() -> datetime:
    return now_utc().astimezone(load_config().tz)


def today_ist() -> date:
    return now_ist().date()


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


def retry(fn: Callable[[], T], tries: int = 3, base_delay: float = 2.0, what: str = "call") -> T:
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("%s failed (attempt %d/%d): %s", what, i + 1, tries, redact(str(e)))
            if i < tries - 1:
                _time.sleep(base_delay * (2**i))
    assert last is not None
    raise last
