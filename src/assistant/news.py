"""Google News RSS fetch + deterministic keyword scoring.

All headline text is UNTRUSTED input: it is sanitized, length-capped, only ever
matched against keyword lists, HTML-escaped before display, and passed to the
optional LLM only as clearly delimited data."""
from __future__ import annotations

import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests

from .config import Config
from .util import now_utc

log = logging.getLogger(__name__)
MAX_TITLE = 300
UA = "trading-research-assistant/0.1 (personal, non-commercial)"


@dataclass(frozen=True)
class Headline:
    title: str
    source: str
    link: str
    published: datetime  # UTC

    @property
    def key(self) -> str:
        return re.sub(r"\W+", " ", self.title.lower()).strip()


def sanitize(text: str, limit: int = MAX_TITLE) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def rss_url(query: str) -> str:
    return (f"https://news.google.com/rss/search?q={quote_plus(query + ' when:2d')}"
            "&hl=en-IN&gl=IN&ceid=IN:en")


def fetch_query(query: str, since: datetime, timeout: float = 10) -> list[Headline]:
    try:
        r = requests.get(rss_url(query), timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("news fetch failed for %r: %s", query, e)
        return []
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries:
        try:
            pub = parsedate_to_datetime(e.get("published", "")).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        if pub < since:
            continue
        src = sanitize((e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict)
                       else getattr(e.get("source"), "title", ""), 80)
        title = sanitize(e.get("title", ""))
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        link = e.get("link", "")
        if not link.startswith("https://"):
            link = ""
        if title:
            out.append(Headline(title, src or "unknown", link[:500], pub))
    return out


def fetch_many(queries: dict[str, str], cfg: Config) -> dict[str, list[Headline]]:
    since = now_utc() - timedelta(hours=cfg.news.lookback_hours)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(fetch_query, q, since) for k, q in queries.items()}
        return {k: dedupe(f.result()) for k, f in futs.items()}


def dedupe(items: list[Headline]) -> list[Headline]:
    seen, out = set(), []
    for h in sorted(items, key=lambda h: h.published, reverse=True):
        if h.key in seen or (h.link and h.link in seen):
            continue
        seen.update({h.key, h.link} - {""})
        out.append(h)
    return out


# ---------------- scoring ----------------
def _kw_regex(kw: str) -> re.Pattern:
    words = [re.escape(w) for w in kw.lower().split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"(?:s|es|ed|d)?\b")


def headline_score(title: str, cfg: Config) -> int:
    t = title.lower()
    pos = sum(1 for k in cfg.news.positive if _kw_regex(k).search(t))
    neg = sum(1 for k in cfg.news.negative if _kw_regex(k).search(t))
    return pos - neg


def net_score(headlines: list[Headline], cfg: Config) -> int:
    return max(-3, min(3, sum(headline_score(h.title, cfg) for h in headlines)))


def mentions(h: Headline, name: str, symbol: str, aliases: tuple[str, ...] = ()) -> bool:
    """Does the headline name this company? Case-sensitive for short all-caps tokens
    (so 'HAL' doesn't match 'hal' inside other words), case-insensitive otherwise."""
    short = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|company|of)\b", "", name, flags=re.I)
    short = re.sub(r"\s+", " ", short).strip()
    pats = [name, short, *aliases] + ([symbol] if len(symbol) >= 3 else [])
    for p in pats:
        if not p or len(p) < 2:
            continue
        flags = 0 if (p.isupper() and len(p) <= 5) else re.I
        if re.search(r"(?<![\w&])" + re.escape(p) + r"(?![\w&])", h.title, flags):
            return True
    return False
