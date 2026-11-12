"""Public Telegram channels as an extra news source (read from the public web preview, t.me/s/<name>).

No Telegram login: only channels with a public page can be read. Most posts are news cards (images), so images
are OCR'd locally with tesseract. Everything read here is UNTRUSTED text: it is sanitized, length-capped, only
matched against stock names and the keyword lists, and HTML-escaped wherever it is shown. Posts are cached in
`channel_posts`, so a flaky connection to t.me only delays them, and each image is OCR'd once.
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import news as newsmod
from .config import Config
from .util import iso_utc, now_utc

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (Macintosh; trading-research-assistant, personal)"
NAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")
IMG_HOSTS = re.compile(r"^https://cdn\d*\.(telesco\.pe|telegram-cdn\.org)/file/[A-Za-z0-9_\-]+(\.[a-z]{3,4})?$")
MAX_TEXT = 1500


@dataclass
class Post:
    channel: str
    post_id: int
    posted_at: datetime   # UTC
    text: str             # caption / message text (sanitized)
    images: list[str]


def _get(url: str, tries: int = 4, timeout: float = 15) -> requests.Response | None:
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
            if r.status_code == 200:
                return r
            log.warning("channel GET %s -> %s", url, r.status_code)
        except requests.RequestException as e:
            log.info("channel GET %s failed (try %d): %s", url, i + 1, type(e).__name__)
        time.sleep(2 * (i + 1))
    return None


def _keep_lines(text: str) -> str:
    """Sanitize line by line (newsmod.sanitize flattens newlines, which news cards need)."""
    lines = (newsmod.sanitize(ln, 300) for ln in (text or "").splitlines())
    return "\n".join(ln for ln in lines if ln)[:MAX_TEXT]


def _clean(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return _keep_lines(t)


def parse_page(channel: str, page: str) -> list[Post]:
    out = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        m = re.search(r'data-post="' + re.escape(channel) + r'/(\d+)"', block, re.I)
        d = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not m or not d:
            continue
        try:
            when = datetime.fromisoformat(d.group(1)).astimezone(timezone.utc)
        except ValueError:
            continue
        t = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        imgs = [u for u in re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
                if IMG_HOSTS.match(u)]
        out.append(Post(channel, int(m.group(1)), when, _clean(t.group(1)) if t else "", imgs[:4]))
    return out


def fetch_posts(channel: str, since: datetime, max_pages: int = 8) -> list[Post] | None:
    """Posts newer than `since`, newest pages first. None if the channel couldn't be reached at all."""
    if not NAME_RE.match(channel):
        raise ValueError(f"bad channel name {channel!r}")
    posts: dict[int, Post] = {}
    url = f"https://t.me/s/{channel}"
    for page_no in range(max_pages):
        r = _get(url)
        if r is None:
            return list(posts.values()) if posts else None
        got = parse_page(channel, r.text)
        if not got:
            break
        for p in got:
            posts[p.post_id] = p
        oldest = min(got, key=lambda p: p.post_id)
        if oldest.posted_at < since:
            break
        url = f"https://t.me/s/{channel}?before={oldest.post_id}"
    return [p for p in posts.values() if p.posted_at >= since]


def ocr_image(url: str, timeout: float = 30) -> str:
    """Download one image into a fresh temp dir and OCR it with tesseract. '' on any failure."""
    exe = shutil.which("tesseract") or ("/opt/homebrew/bin/tesseract" if Path("/opt/homebrew/bin/tesseract").exists()
                                         else None)
    if not exe or not IMG_HOSTS.match(url):
        return ""
    r = _get(url, tries=2, timeout=20)
    if r is None or len(r.content) > 5_000_000:
        return ""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "img"
        f.write_bytes(r.content)
        big = Path(d) / "big.png"
        try:  # news cards are ~600px wide: upscaling ~3x (macOS sips) makes tesseract far more accurate
            subprocess.run(["/usr/bin/sips", "-s", "format", "png", "-Z", "2400", str(f), "--out", str(big)],
                           capture_output=True, timeout=30, cwd=d)
            src = big if big.exists() else f
            res = subprocess.run([exe, str(src), "-", "-l", "eng", "--psm", "4"], capture_output=True,
                                 timeout=timeout, cwd=d)
        except (subprocess.TimeoutExpired, OSError):
            return ""
    return _keep_lines(res.stdout.decode("utf-8", "replace"))


def poll(conn: sqlite3.Connection, cfg: Config, hours: float | None = None, budget_s: float = 150) -> int:
    """Cache new posts from every configured channel, then OCR pending images within `budget_s` seconds
    (the rest are OCR'd on the next poll). Returns posts added."""
    ch = cfg.channels
    if not ch.enabled or not ch.names:
        return 0
    t0 = time.monotonic()
    since = now_utc() - timedelta(hours=hours or ch.lookback_hours)
    added = 0
    for name in ch.names:
        try:
            posts = fetch_posts(name, since, ch.max_pages)
        except Exception as e:  # noqa: BLE001
            log.warning("channel %s fetch error: %s", name, e)
            continue
        if posts is None:
            log.warning("channel %s unreachable", name)
            continue
        for p in sorted(posts, key=lambda p: p.post_id):
            imgs = p.images[: ch.max_images_per_post] if ch.ocr else []
            cur = conn.execute(
                "INSERT OR IGNORE INTO channel_posts(channel,post_id,posted_at,text,images,ocr,fetched_at) "
                "VALUES(?,?,?,?,?,?,?)", (name, p.post_id, p.posted_at.isoformat(), p.text,
                                          json.dumps(imgs), None if imgs else "", iso_utc()))
            added += cur.rowcount
        conn.commit()
    pending = conn.execute("SELECT channel, post_id, images FROM channel_posts WHERE ocr IS NULL "
                           "ORDER BY posted_at DESC").fetchall()
    for r in pending:
        if time.monotonic() - t0 > budget_s:
            break
        text = "\n".join(t for t in (ocr_image(u) for u in json.loads(r["images"] or "[]")) if t)
        conn.execute("UPDATE channel_posts SET ocr=? WHERE channel=? AND post_id=?",
                     (text, r["channel"], r["post_id"]))
        conn.commit()
    conn.execute("DELETE FROM channel_posts WHERE posted_at < ?",
                 ((now_utc() - timedelta(days=ch.keep_days)).isoformat(),))
    conn.commit()
    return added


def maybe_poll(conn: sqlite3.Connection, cfg: Config, now: datetime) -> int | None:
    """Called by the 5-minute tick: poll every `poll_minutes` between 07:00 and 22:00 IST."""
    from .db import kv_get, kv_set
    ch = cfg.channels
    if not ch.enabled or not ch.names or not (7 <= now.hour < 22):
        return None
    last = kv_get(conn, "channel_last_poll")
    if last and (now_utc() - datetime.fromisoformat(last)) < timedelta(minutes=ch.poll_minutes):
        return None
    kv_set(conn, "channel_last_poll", iso_utc())
    return poll(conn, cfg, budget_s=150)


BULLET = re.compile(r"^\s*[•¢*+\-–«»·]+\s*")


def _post_lines(text: str) -> list[tuple[bool, str]]:
    """(is_bullet, line) for each non-trivial line of a post."""
    out = []
    for ln in text.splitlines():
        b = bool(BULLET.match(ln))
        ln = newsmod.sanitize(BULLET.sub("", ln), 200).strip(" ‘’'\"|")
        if len(ln) >= 3:
            out.append((b, ln))
    return out


def _is_table(text: str) -> bool:
    """Price tables (gainers/losers, index levels) name many stocks but say nothing about them."""
    chars = [c for c in text if not c.isspace()]
    return bool(chars) and sum(c.isdigit() for c in chars) / len(chars) > 0.22


@dataclass
class CachedPost:
    channel: str
    post_id: int
    posted_at: datetime
    body: str


def cached_posts(conn: sqlite3.Connection, since: datetime) -> list[CachedPost]:
    out = []
    for r in conn.execute("SELECT * FROM channel_posts WHERE posted_at >= ? ORDER BY posted_at",
                          (since.isoformat(),)):
        body = "\n".join(x for x in (r["text"], r["ocr"]) if x and x.strip())
        if body:
            out.append(CachedPost(r["channel"], r["post_id"], datetime.fromisoformat(r["posted_at"]), body))
    return out


# Words that turn a group name into a different listed company ("Reliance Power", "SBI Life", "Adani Green").
GROUP_WORDS = {"power", "life", "green", "ports", "power,", "infra", "infrastructure", "capital", "home", "finance",
               "general", "energy", "total", "gas", "wilmar", "cards", "securities", "pharma", "motors", "steel",
               "chemicals", "consumer", "realty", "housing", "communications", "comm", "money", "insurance",
               "elxsi", "investments", "technologies", "tech", "transmission", "solutions", "holdings"}


def _names_it(text: str, st) -> bool:
    """newsmod.mentions, but an occurrence followed by a group-company word names a different company
    ("Reliance Power" is not Reliance Industries), unless that word is part of this stock's own name."""
    h = newsmod.Headline(text, "", "", datetime.now(timezone.utc))
    if not newsmod.mentions(h, st.name, st.symbol, st.aliases):
        return False
    own = {w.lower() for w in re.findall(r"[A-Za-z]+", st.name)}
    short = re.sub(r"\b(ltd|limited|india|industries|corp|corporation|company|of)\b", "", st.name, flags=re.I)
    pats = {st.name, re.sub(r"\s+", " ", short).strip(), *st.aliases}
    if len(st.symbol) >= 3:
        pats.add(st.symbol)
    for p in filter(None, pats):
        flags = 0 if (p.isupper() and len(p) <= 5) else re.I
        for m in re.finditer(r"(?<![\w&])" + re.escape(p) + r"(?![\w&])", text, flags):
            nxt = re.match(r"\s+([A-Za-z]+)", text[m.end():])
            word = nxt.group(1).lower() if nxt else ""
            if word not in GROUP_WORDS or word in own:
                return True
    return False


def stock_mentions(posts: list[CachedPost], universe) -> dict[str, list[newsmod.Headline]]:
    """symbol -> one Headline per post naming that stock. The title is the line naming it plus the bullet
    points under it (news cards list "Company" then its bullets), or the next line if there are none."""
    out: dict[str, list[newsmod.Headline]] = {}
    for p in posts:
        if _is_table(p.body):
            continue
        lines = _post_lines(p.body)
        for st in universe:
            at = next((i for i, (_, ln) in enumerate(lines)   # + next line: OCR splits names across lines
                       if _names_it(ln + (" " + lines[i + 1][1] if i + 1 < len(lines) else ""), st)
                       and _names_it(ln, st)), None)
            if at is None:
                continue
            if lines[at][1].count(",") >= 4:   # a list of companies (earnings today, index changes...)
                title = newsmod.sanitize(f"listed with others: {lines[at][1]}", 250)
                out.setdefault(st.symbol, []).append(newsmod.Headline(
                    title, f"Telegram @{p.channel}", f"https://t.me/{p.channel}/{p.post_id}", p.posted_at))
                continue
            take = [lines[at][1]]
            nxt = lines[at + 1:at + 6]
            bullets = []
            for b, ln in nxt:
                if not b:
                    break
                bullets.append(ln)
            take += bullets[:4] if bullets else [ln for _, ln in nxt[:1]]
            title = newsmod.sanitize(" · ".join(take), 250)
            out.setdefault(st.symbol, []).append(newsmod.Headline(
                title, f"Telegram @{p.channel}", f"https://t.me/{p.channel}/{p.post_id}", p.posted_at))
    return out
