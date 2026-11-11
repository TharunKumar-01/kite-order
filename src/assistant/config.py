"""Configuration loading (config.yaml + .env) and project paths."""
from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("TRADING_ASSISTANT_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"
DB_PATH = DATA_DIR / "journal.db"
UNIVERSE_CSV = DATA_DIR / "universe.csv"
HOLIDAYS_CSV = DATA_DIR / "holidays.csv"


class Signals(BaseModel):
    sma_fast: int = 50
    sma_slow: int = 200
    breakout_window: int = 20
    volume_mult: float = 1.5
    rsi_period: int = 14
    rsi_max: float = 75
    rel_strength_months: int = 3
    require: list[str] = ["trend", "not_overextended"]
    min_total_score: float = 3.5
    news_candidates: int = 15


class Weights(BaseModel):
    trend: float = 2.0
    breakout: float = 1.5
    volume: float = 1.0
    not_overextended: float = 1.0
    rel_strength: float = 0.1
    rs_clip: float = 20.0
    news: float = 0.5
    sector_tailwind: float = 0.5
    sector_news: float = 0.25


class Paper(BaseModel):
    hold_days: int = 5
    stop_loss_pct: float = 6.0
    slippage_pct: float = 0.10
    entry_mode: str = "open"


class Costs(BaseModel):
    brokerage_delivery: float = 0.0
    stt_pct: float = 0.1
    stt_round_to_rupee: bool = True
    exchange_txn_pct: float = 0.00297
    sebi_per_crore: float = 10
    stamp_buy_pct: float = 0.015
    gst_pct: float = 18
    dp_charge_per_sell: float = 15.34


class News(BaseModel):
    lookback_hours: int = 20
    max_headlines_per_stock: int = 3
    exclude_at_or_below: int = -2
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    sector_queries: dict[str, str] = Field(default_factory=dict)


class Market(BaseModel):
    benchmark: str = "^NSEI"
    history_days: int = 400
    sector_index: dict[str, str] = Field(default_factory=dict)
    global_cues: dict[str, str] = Field(default_factory=dict)
    gift_nifty_ticker: str | None = None
    sector_cues: dict[str, dict[str, float]] = Field(default_factory=dict)


class Scheduler(BaseModel):
    premarket_time: str = "08:15"
    premarket_cutoff: str = "09:10"
    pick_expiry_time: str = "11:00"
    evening_time: str = "16:30"
    weekly_report_time: str = "08:00"

    @staticmethod
    def t(s: str) -> time:
        h, m = s.split(":")
        return time(int(h), int(m))


class LLM(BaseModel):
    enabled: bool = False
    model: str = "claude-opus-5-5"
    timeout_seconds: float = 20
    max_headline_chars: int = 200


class Kite(BaseModel):
    max_order_value: float = 2500     # hard cap per BUY GTT (qty * limit), enforced in kite.py
    login_port: int = 8765            # redirect URL: http://127.0.0.1:<port>/kite
    max_orders_per_day: int = 2       # confirmed BUY orders per IST day
    default_sl_pct: float = 6.0       # stop-loss when none is given ("sl none" to skip)
    default_target_pct: float = 10.0  # fixed target (target_mode "fixed", or fallback when history is missing)
    target_mode: str = "auto"         # "auto" = per-stock target from its own history; "fixed" = default_target_pct;
                                      # "none" = stop-loss only unless the message says "tp ..."
    auto_target_quantile: float = 0.5 # auto: the rise reached in this share of past windows (0.5 = typical best move)
    auto_target_days: int = 0         # auto: window in trading days (0 = paper.hold_days)
    auto_target_min_pct: float = 4.0
    auto_target_max_pct: float = 25.0
    now_trigger_gap_pct: float = 0.3  # "@now": trigger this far above the last price
    now_limit_buffer_pct: float = 0.5 # "@now": limit this far above the trigger so it fills
    sl_limit_buffer_pct: float = 1.0  # stop-loss sell limit this far below its trigger
    max_price_deviation_pct: float = 10.0  # fat-finger guard: price must be within this of last price
    max_sl_pct: float = 25.0
    max_target_pct: float = 50.0      # a target SELL may be at most this far above the last price
    draft_ttl_seconds: int = 180      # a confirmation button is valid this long
    buy_valid_until: str = "15:30"    # untriggered BUY GTTs (+ their stop-loss) are deleted at this IST time
                                      # on the order's trading day; "" keeps them up to Kite's 1 year


class Review(BaseModel):
    """Rules for the /scan check of your Kite holdings (flags only; nothing is sold automatically)."""
    take_profit_pct: float = 10.0     # gain from your average price that counts as "decent profit"
    fast_sma: int = 20                # momentum fading = close below this many days' average
    weak_drop_5d_pct: float = 5.0     # a 5-day fall this big while below the 50-day average = weak
    weakest_count: int = 5            # weakest universe stocks listed as "avoid buying"


class Config(BaseModel):
    timezone: str = "Asia/Kolkata"
    capital_total: float = 5000
    max_position_value: float = 2500
    max_open_positions: int = 2
    shortlist_size: int = 5
    min_avg_traded_value_20d: float = 2e7
    signals: Signals = Signals()
    weights: Weights = Weights()
    paper: Paper = Paper()
    costs: Costs = Costs()
    news: News = News()
    market: Market = Market()
    scheduler: Scheduler = Scheduler()
    llm: LLM = LLM()
    kite: Kite = Kite()
    review: Review = Review()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Secrets(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    anthropic_api_key: str | None = None
    kite_api_key: str | None = None
    kite_api_secret: str | None = None


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> Config:
    p = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Config.model_validate(raw or {})


@lru_cache(maxsize=1)
def load_secrets() -> Secrets:
    load_dotenv(ROOT / ".env")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return Secrets(
        telegram_bot_token=_real(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
        telegram_chat_id=int(chat) if chat.lstrip("-").isdigit() else None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        kite_api_key=_real(os.environ.get("KITE_API_KEY", "")),
        kite_api_secret=_real(os.environ.get("KITE_API_SECRET", "")),
    )


def _real(v: str) -> str | None:
    """Treat empty or .env.example placeholder values as unset."""
    v = (v or "").strip()
    return None if not v or "replace-with" in v else v
