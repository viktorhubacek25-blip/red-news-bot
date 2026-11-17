#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Red News bot – posílá do Telegramu jen zprávy s velkým dopadem na USD, indexy a komodity.

Jeden běh = stáhne zdroje, ohodnotí headliny, pošle nové důležité zprávy,
zkontroluje ekonomický kalendář (ranní přehled + upozornění před událostí) a uloží stav.
Bez externích knihoven – jen standardní Python 3.10+.

Použití:
  python news_bot.py            # běžný běh (GitHub Actions každých 5 min)
  python news_bot.py --test     # testovací zpráva + stav zdrojů + ukázka skórování
  python news_bot.py --dry-run  # nic neposílá, jen vypíše do konzole
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import config as C

TZ = ZoneInfo(C.TIMEZONE)
UTC = timezone.utc
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 RedNewsBot/1.0"
)
FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "CNY": "🇨🇳", "CAD": "🇨🇦",
    "AUD": "🇦🇺", "NZD": "🇳🇿", "CHF": "🇨🇭", "ALL": "🌐",
}
CZ_DAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
STOPWORDS = set(
    "a an the of to in on for at by with from as is are was were be been it its this that and or "
    "but not no vs versus says said say after before over under amid into than more less up down "
    "us u.s. new year m/m y/y q/q mm yy qq".split()
)
FLAGS_I = re.IGNORECASE

# ---------------------------------------------------------------------------
# Předkompilované vzory
# ---------------------------------------------------------------------------
MUST_RE = [re.compile(p, FLAGS_I) for p in C.MUST_SEND]
GROUPS_RE = [(w, [re.compile(p, FLAGS_I) for p in pats]) for w, pats in C.KEYWORD_GROUPS]
NEG_RE = [re.compile(p, FLAGS_I) for p in C.NEGATIVE_PATTERNS]
DATA_RE = re.compile(C.DATA_RELEASE_PATTERN, FLAGS_I)
TAGS_RE = [(label, re.compile(p, FLAGS_I)) for label, p in C.ASSET_TAGS]
CAL_ALWAYS_RE = [re.compile(p, FLAGS_I) for p in C.CALENDAR_ALWAYS]


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def now_utc():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Stav (ukládá se mezi běhy přes GitHub Actions cache)
# ---------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("initialized", False)
    st.setdefault("seen", {})
    st.setdefault("sent", [])
    st.setdefault("reminded", {})
    st.setdefault("summary_date", "")
    st.setdefault("evening_date", "")
    st.setdefault("weekly_key", "")
    st.setdefault("week_log", [])
    st.setdefault("calendar", {"fetched_at": 0, "events": []})
    st.setdefault("health", {})
    return st


def save_state(st, path):
    t = time.time()
    st["seen"] = {k: v for k, v in st["seen"].items() if t - v < 4 * 86400}
    st["sent"] = [s for s in st["sent"] if t - s["t"] < 2 * 86400][-300:]
    st["reminded"] = {k: v for k, v in st["reminded"].items() if t - v < 8 * 86400}
    st["week_log"] = [w for w in st.get("week_log", []) if t - w["t"] < 9 * 86400][-500:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Stahování a parsování feedů
# ---------------------------------------------------------------------------
def fetch(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _text(el):
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def parse_feed(raw, source):
    """Vrátí seznam položek {id,title,link,published,publisher,source,trust}."""
    root = ET.fromstring(raw)
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    nodes = root.iter("item")
    is_atom = False
    nodes = list(nodes)
    if not nodes:
        nodes = list(root.iter(atom + "entry"))
        is_atom = True
    for n in nodes:
        if is_atom:
            title = _clean(_text(n.find(atom + "title")))
            link_el = n.find(atom + "link")
            link = link_el.get("href", "") if link_el is not None else ""
            guid = _text(n.find(atom + "id")) or link
            pub = _parse_date(_text(n.find(atom + "updated")) or _text(n.find(atom + "published")))
            publisher = ""
        else:
            title = _clean(_text(n.find("title")))
            desc = _clean(_text(n.find("description")))
            if source.get("trump"):
                # Truth Social: text příspěvku je v popisu, titulek bývá generický
                title = desc or title
                if not title or re.fullmatch(r"(RT[: ].*|https?://\S+|\[No Title\].*)", title):
                    continue
            link = _text(n.find("link"))
            guid = _text(n.find("guid")) or link
            pub = _parse_date(_text(n.find("pubDate")) or _text(n.find("{http://purl.org/dc/elements/1.1/}date")))
            publisher = _text(n.find("source"))
        if not title:
            continue
        # Úklid titulků
        title = re.sub(r"^FinancialJuice:\s*", "", title, flags=FLAGS_I)
        if source.get("google") and publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)].strip()
        items.append(
            {
                "id": (guid or link or title)[:300],
                "title": title,
                "link": link.strip(),
                "published": pub.timestamp() if pub else None,
                "publisher": publisher or source["name"],
                "source": source["name"],
                "trust": source.get("trust", 0),
                "trump": bool(source.get("trump")),
            }
        )
    return items


def collect_items(st, alerts_out):
    """Stáhne všechny zdroje, aktualizuje jejich zdraví. Vrátí seznam položek."""
    all_items = []
    for src in C.SOURCES:
        h = st["health"].setdefault(src["name"], {"fails": 0, "alerted": False, "error": "", "count": 0})
        if src.get("google"):
            time.sleep(1.5)  # Google News při rychlých dotazech vrací 429
        try:
            items = parse_feed(fetch(src["url"]), src)
            all_items.extend(items)
            if h.get("alerted"):
                alerts_out.append(f"✅ Zdroj <b>{html.escape(src['name'])}</b> zase funguje.")
            h.update(fails=0, alerted=False, error="", count=len(items))
            log(f"OK   {src['name']}: {len(items)} položek")
        except Exception as e:  # noqa: BLE001 – jeden rozbitý zdroj nesmí shodit bota
            h["fails"] = h.get("fails", 0) + 1
            h["error"] = f"{type(e).__name__}: {e}"[:200]
            h["count"] = 0
            log(f"FAIL {src['name']}: {h['error']}")
            # Po ~1 hodině nepřetržitých chyb (12 běhů) jednou upozorni
            if h["fails"] >= 12 and not h.get("alerted"):
                h["alerted"] = True
                alerts_out.append(
                    f"⚠️ Zdroj <b>{html.escape(src['name'])}</b> hodinu nefunguje: "
                    f"<code>{html.escape(h['error'])}</code>"
                )
    return all_items


# ---------------------------------------------------------------------------
# Skórování
# ---------------------------------------------------------------------------
NORMALIZE = [
    (r"\bper ?cent\b", "%"), (r"(\d)\s+%", r"\1%"), (r"\beuropean union\b", "eu"),
    (r"\bfederal reserve\b", "fed"), (r"\bunited states\b|\bu\.s\.\b", "us"),
    (r"\bbasis points?\b|\bbps?\b", "bp"), (r"\bcrude oil\b", "oil"), (r"\bbillion\b", "bn"),
    (r"\bmillion\b", "mn"), (r"\bannounces?\b|\bannounced\b|\bunveils?\b", "announce"),
    (r"\bimposes?\b|\bslaps?\b", "impose"), (r"\bcuts\b", "cut"), (r"\bhikes\b|\braises\b", "hike"),
]
NORMALIZE_RE = [(re.compile(p, FLAGS_I), r) for p, r in NORMALIZE]
ENTITIES = {
    "fed", "fomc", "ecb", "boj", "boe", "pboc", "snb", "rba", "boc", "rbnz", "opec", "opec+",
    "us", "eu", "china", "japan", "uk", "germany", "france", "canada", "russia", "iran", "israel",
    "ukraine", "india", "mexico", "eurozone", "oil", "gold", "silver", "copper", "gas",
}


def tokens(s):
    s = s.lower()
    for r, rep in NORMALIZE_RE:
        s = r.sub(rep, s)
    words = re.findall(r"[a-z0-9][a-z0-9\-\.%/+]*", s)
    return {w.strip(".") for w in words if w.strip(".") and w not in STOPWORDS and len(w) > 1}


def similarity(a, b):
    """Max z Jaccardu a 'overlap' koeficientu (krátký titulek obsažený v delším)."""
    if not a or not b:
        return 0.0
    # Různé instituce/země = různé zprávy (Fed vs ECB)
    ea, eb = a & ENTITIES, b & ENTITIES
    if ea and eb and not (ea & eb):
        return 0.0
    # Obě mají čísla, ale žádné společné = různá data (CPI m/m 0.4% vs y/y 3.1%)
    na = {w for w in a if re.search(r"\d", w)}
    nb_ = {w for w in b if re.search(r"\d", w)}
    if na and nb_ and not (na & nb_):
        return 0.0
    inter = len(a & b)
    jac = inter / len(a | b)
    small = min(len(a), len(b))
    overlap = inter / small if small >= 4 else 0.0
    return max(jac, overlap * 0.85)


def calendar_keywords(title):
    junk = {"m/m", "y/y", "q/q", "change", "rate", "flash", "prelim", "final", "s", "the", "and", "of", "index"}
    return {w for w in re.findall(r"[a-z][a-z\-]+", title.lower()) if w not in junk and len(w) >= 3}


REL_RE = re.compile(C.REQUIRE_RELEVANCE, FLAGS_I) if getattr(C, "REQUIRE_RELEVANCE", None) else None


def score_item(item, active_events, relevance=True):
    t = item["title"]
    reasons = []
    if relevance and REL_RE and not item.get("trump") and not REL_RE.search(t):
        return 0, ["mimo USD/komodity"]
    if relevance and getattr(C, "SKIP_DATA_RELEASES", False):
        if DATA_RE.search(t) and re.search(r"\d", t):
            return 0, ["výsledek dat"]
        low0 = t.lower()
        for ev in active_events:
            kws = calendar_keywords(ev["title"])
            if kws and any(k in low0 for k in kws):
                return 0, ["výsledek dat (kalendář)"]
    for r in MUST_RE:
        if r.search(t):
            return 99, ["MUST"]
    score = 0
    for weight, pats in GROUPS_RE:
        hits = sum(1 for r in pats if r.search(t))
        if hits:
            score += min(hits * weight, 2 * weight)
            reasons.append(f"+{min(hits * weight, 2 * weight)}")
    if DATA_RE.search(t) and re.search(r"\d", t):
        score += C.DATA_RELEASE_BONUS
        reasons.append("data")
    score += item.get("trust", 0)
    pub = (item.get("publisher") or "").lower()
    for name, bonus in C.PUBLISHER_TRUST.items():
        if name in pub:
            score += bonus
            reasons.append(f"src+{bonus}")
            break
    low = t.lower()
    for ev in active_events:
        kws = calendar_keywords(ev["title"])
        if kws and any(k in low for k in kws) and re.search(r"\d", t):
            score += C.CALENDAR_MATCH_BONUS
            reasons.append("kalendář")
            break
    if any(r.search(t) for r in NEG_RE):
        score -= C.NEGATIVE_PENALTY
        reasons.append("šum")
    if item.get("trump"):
        if score >= 4:  # jen příspěvky s tržním tématem
            score += getattr(C, "TRUMP_BONUS", 3)
            reasons.append("trump")
        else:
            score = min(score, 0)
    return score, reasons


def asset_tags(title):
    return [label for label, r in TAGS_RE if r.search(title)]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class Telegram:
    def __init__(self, dry_run=False):
        self.dry = dry_run
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not self.dry and (not self.token or not self.chat):
            sys.exit(
                "Chybí TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID. "
                "Nastav je v GitHubu: Settings → Secrets and variables → Actions."
            )

    def send(self, text, silent=False):
        if self.dry:
            print("\n----- TELEGRAM -----\n" + text + "\n--------------------")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chunk in split_message(text):
            body = json.dumps(
                {
                    "chat_id": self.chat,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": not C.LINK_PREVIEW,
                    "disable_notification": silent,
                }
            ).encode()
            for attempt in range(3):
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        r.read()
                    break
                except urllib.error.HTTPError as e:
                    detail = e.read().decode(errors="ignore")
                    if e.code == 429:
                        wait = json.loads(detail).get("parameters", {}).get("retry_after", 3)
                        time.sleep(min(int(wait), 30))
                        continue
                    log(f"Telegram chyba {e.code}: {detail}")
                    return False
                except Exception as e:  # noqa: BLE001
                    log(f"Telegram chyba: {e}")
                    time.sleep(2)
            time.sleep(0.4)
        return True


    def api(self, method, payload=None):
        """Obecné volání Telegram Bot API (vrací 'result' nebo None)."""
        if self.dry:
            return getattr(self, "fake_" + method, None)
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read()).get("result")
        except Exception as e:  # noqa: BLE001
            log(f"Telegram {method} chyba: {e}")
            return None


def split_message(text, limit=3900):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def is_quiet(local_now):
    if not C.QUIET_HOURS:
        return False
    a, b = C.QUIET_HOURS
    h = local_now.hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def fmt_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M") if ts else ""


# ---------------------------------------------------------------------------
# Překlad (bezplatný Google Translate, záloha MyMemory, jinak originál)
# ---------------------------------------------------------------------------
_TR_CACHE = {}


def translate(text):
    target = getattr(C, "TRANSLATE_TO", None)
    if not target or not text:
        return text
    if text in _TR_CACHE:
        return _TR_CACHE[text]
    q = urllib.parse.quote(text)
    out = None
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl={target}&dt=t&q={q}"
        data = json.loads(fetch(url, timeout=10))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:  # noqa: BLE001
        log(f"Překlad Google selhal: {e}")
        try:
            url = f"https://api.mymemory.translated.net/get?q={q}&langpair=en|{target}"
            data = json.loads(fetch(url, timeout=10))
            cand = (data.get("responseData") or {}).get("translatedText") or ""
            if cand and "MYMEMORY WARNING" not in cand.upper():
                out = html.unescape(cand).strip()
        except Exception as e2:  # noqa: BLE001
            log(f"Překlad MyMemory selhal: {e2}")
    out = out or text
    _TR_CACHE[text] = out
    return out


def display_title(item):
    """Vrátí (titulek k zobrazení, originál nebo None)."""
    tr = translate(item["title"])
    if tr != item["title"] and getattr(C, "SHOW_ORIGINAL", False):
        return tr, item["title"]
    return tr, None


def _short(text, limit=450):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def format_alert(item, score):
    if item.get("trump"):
        item = dict(item, title=_short(item["title"]))
    shown, original = display_title(item)
    if item.get("trump"):
        shown = "Trump (Truth Social): " + shown
    tags = asset_tags(item["title"])
    meta = " · ".join(x for x in [html.escape(item["publisher"]), fmt_time(item.get("published"))] if x)
    lines = [f"🔴 <b>{html.escape(shown)}</b>"]
    if original:
        lines.append(f"<i>{html.escape(original)}</i>")
    if tags:
        lines.append(" · ".join(tags))
    tail = f"<i>{meta}</i>"
    if item.get("link"):
        tail += f' · <a href="{html.escape(item["link"], quote=True)}">otevřít</a>'
    tail += f" · <i>skóre {score if score < 99 else 'MAX'}</i>"
    lines.append(tail)
    return "\n".join(lines)


def format_digest(scored):
    lines = [f"🔴 <b>{len(scored)} důležitých zpráv</b>"]
    for score, item in scored:
        link = html.escape(item.get("link", ""), quote=True)
        if item.get("trump"):
            item = dict(item, title=_short(item["title"], 250))
        shown, _ = display_title(item)
        if item.get("trump"):
            shown = "Trump: " + shown
        t = html.escape(shown)
        src = html.escape(item["publisher"])
        entry = f'• <a href="{link}">{t}</a>' if link else f"• {t}"
        lines.append(f"{entry} <i>({src}, {fmt_time(item.get('published'))})</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ekonomický kalendář
# ---------------------------------------------------------------------------
def refresh_calendar(st, force=False):
    cal = st["calendar"]
    if not force and time.time() - cal.get("fetched_at", 0) < C.CALENDAR_REFRESH_MINUTES * 60:
        return cal["events"], True
    h = st["health"].setdefault("Kalendář (ForexFactory)", {"fails": 0, "alerted": False, "error": "", "count": 0})
    try:
        events = parse_calendar(json.loads(fetch(C.CALENDAR_URL)))
        cal.update(fetched_at=time.time(), events=events)
        h.update(fails=0, error="", count=len(events))
        log(f"OK   Kalendář: {len(events)} sledovaných událostí")
        return events, True
    except Exception as e:  # noqa: BLE001
        h["fails"] = h.get("fails", 0) + 1
        h["error"] = f"{type(e).__name__}: {e}"[:200]
        # Při chybě zkus znovu za 10 min, ne hned (limit feedu)
        cal["fetched_at"] = time.time() - (C.CALENDAR_REFRESH_MINUTES - 10) * 60
        log(f"FAIL Kalendář: {h['error']}")
        return cal["events"], False


def parse_calendar(data):
    """Z ForexFactory JSON vybere sledované červené události a svátky."""
    events = []
    for e in data:
        title = e.get("title", "")
        cur = e.get("country", "")
        holiday = e.get("impact") == "Holiday"
        if holiday:
            if cur not in getattr(C, "HOLIDAY_CURRENCIES", []):
                continue
        else:
            always = any(r.search(title) for r in CAL_ALWAYS_RE)
            if not always and (e.get("impact") not in C.CALENDAR_IMPACTS or cur not in C.CALENDAR_CURRENCIES):
                continue
            if always and cur not in C.CALENDAR_CURRENCIES and cur != "ALL":
                continue
        try:
            dt = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        events.append(
            {
                "key": f"{cur}|{title}|{e['date']}",
                "title": title,
                "country": cur,
                "ts": dt.astimezone(UTC).timestamp(),
                # 00:00 v čase zdroje = "celý den / tentative"
                "all_day": holiday or (dt.hour == 0 and dt.minute == 0),
                "holiday": holiday,
                # datum události v čase zdroje (svátky platí celý den daného data)
                "date": dt.date().isoformat(),
                "impact": e.get("impact", ""),
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
                "actual": e.get("actual", ""),
            }
        )
    events.sort(key=lambda x: x["ts"])
    return events


# ---------------------------------------------------------------------------
# ForexFactory web (libovolný týden včetně skutečných hodnot)
# ---------------------------------------------------------------------------
NY_TZ = ZoneInfo("America/New_York")
FF_IMPACT = {"high": "High", "medium": "Medium", "low": "Low", "holiday": "Holiday", "non-economic": "Non-Economic"}


def ff_week_url(monday):
    sunday = monday - timedelta(days=1)
    return f"https://www.forexfactory.com/calendar?week={sunday.strftime('%b').lower()}{sunday.day}.{sunday.year}"


def _extract_days_json(page):
    k = page.find("days: [")
    if k < 0:
        raise ValueError("na stránce chybí data kalendáře")
    i = page.index("[", k)
    depth, in_s, esc = 0, False, False
    for p in range(i, len(page)):
        c = page[p]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_s = False
            continue
        if c == '"':
            in_s = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(page[i:p + 1])
    raise ValueError("nedokončená data kalendáře")


def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def ff_week_events(monday):
    """Stáhne týden z forexfactory.com a vrátí události ve formátu parse_calendar (i s Actual)."""
    page = fetch_html(ff_week_url(monday))
    raw = []
    for day in _extract_days_json(page):
        for e in day.get("events", []):
            dl = e.get("dateline")
            if not dl:
                continue
            dt = datetime.fromtimestamp(dl, NY_TZ)
            if not re.search(r"\d:\d\d", e.get("timeLabel") or ""):
                dt = dt.replace(hour=0, minute=0)  # All Day / Tentative
            raw.append({
                "title": e.get("name", ""),
                "country": e.get("currency", ""),
                "date": dt.isoformat(),
                "impact": FF_IMPACT.get((e.get("impactName") or "").lower(), e.get("impactName", "")),
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
                "actual": e.get("actual", ""),
            })
    return parse_calendar(raw)


def event_date(ev):
    """Datum události v místním čase (svátky/celodenní podle data zdroje)."""
    if ev.get("all_day") and ev.get("date"):
        return datetime.fromisoformat(ev["date"]).date()
    return datetime.fromtimestamp(ev["ts"], TZ).date()


def daily_only(events):
    """Události pro denní přehledy/upozornění – jen měny z DAILY_CURRENCIES."""
    cur = set(getattr(C, "DAILY_CURRENCIES", C.CALENDAR_CURRENCIES)) | {"ALL"}
    return [e for e in events if e["country"] in cur]


def with_actuals(events, local_now):
    """Doplní skutečné hodnoty z forexfactory.com (aktuální týden)."""
    monday = local_now.date() - timedelta(days=local_now.weekday())
    try:
        web = ff_week_events(monday)
    except Exception as e:  # noqa: BLE001
        log(f"FAIL ForexFactory web (výsledky): {e}")
        return events
    out = []
    for ev in events:
        m = next((w for w in web if w["country"] == ev["country"] and w["title"] == ev["title"]
                  and abs(w["ts"] - ev["ts"]) < 300), None)
        out.append(dict(ev, actual=m["actual"]) if m and m.get("actual") else ev)
    return out


def _num(v):
    m = re.search(r"-?\d+(\.\d+)?", (v or "").replace(",", ""))
    return float(m.group()) if m else None


def fmt_result_line(ev):
    a, f = ev.get("actual", ""), ev.get("forecast", "")
    na, nf = _num(a), _num(f)
    if na is not None and nf is not None:
        cmp_ = "▲ nad odhadem" if na > nf else ("▼ pod odhadem" if na < nf else "= v souladu s odhadem")
    else:
        cmp_ = ""
    flag = FLAGS.get(ev["country"], "")
    line = f"{flag} <b>{html.escape(ev['title'])}: {html.escape(a)}</b>"
    extra = [x for x in [f"odhad {html.escape(f)}" if f else "", f"předch. {html.escape(ev.get('previous', ''))}" if ev.get("previous") else ""] if x]
    if extra:
        line += f" <i>({', '.join(extra)})</i>"
    if cmp_:
        line += f"\n   {cmp_}"
    return line


def fmt_event_line(ev, with_day=False):
    d = event_date(ev)
    flag = FLAGS.get(ev["country"], "")
    prefix = f"{CZ_DAYS[d.weekday()][:2]} {d.day}. " if with_day else ""
    if ev.get("holiday"):
        return (f"🏦 <b>{prefix}celý den</b> {flag} {ev['country']} – {html.escape(ev['title'])} "
                f"<i>(svátek, banky zavřené → nižší likvidita)</i>")
    when = "celý den" if ev["all_day"] else datetime.fromtimestamp(ev["ts"], TZ).strftime("%H:%M")
    extra = []
    if ev.get("actual"):
        extra.append(f"<b>skutečnost {html.escape(ev['actual'])}</b>")
    if ev.get("forecast"):
        extra.append(f"odhad {html.escape(ev['forecast'])}")
    if ev.get("previous"):
        extra.append(f"předch. {html.escape(ev['previous'])}")
    dot = "🔴" if ev["impact"] == "High" else "🟠"
    line = f"{dot} <b>{prefix}{when}</b> {flag} {html.escape(ev['title'])}"
    if extra:
        line += f" <i>({', '.join(extra)})</i>"
    return line


def day_summary(events, day, label, week_outlook=False):
    """Přehled jednoho dne: svátky nahoře, pak události podle času."""
    todays = [e for e in events if event_date(e) == day]
    hol = [e for e in todays if e.get("holiday")]
    evs = [e for e in todays if not e.get("holiday")]
    lines = [f"📅 <b>Červené události {label} – {CZ_DAYS[day.weekday()]} {day.day}. {day.month}.</b>"]
    lines += [fmt_event_line(e) for e in hol]
    if evs:
        lines += [fmt_event_line(e) for e in evs]
    else:
        lines.append("Žádné červené události pro sledované měny.")
    if week_outlook:
        rest = [e for e in events if event_date(e) > day]
        if rest:
            lines += ["", "🗓 <b>Zbytek týdne</b>"] + [fmt_event_line(e, with_day=True) for e in rest]
    lines.append('\n<a href="https://www.forexfactory.com/calendar">ForexFactory kalendář</a>')
    return "\n".join(lines)


def daily_summary(events, local_now):
    today = local_now.date()
    return day_summary(events, today, "dnes",
                       week_outlook=C.WEEKLY_OUTLOOK_ON_MONDAY and today.weekday() == 0)


def evening_summary(events, local_now, with_markets=False):
    """Večerní přehled: v pracovní dny výsledky trhů + kalendář na zítřek."""
    tomorrow = local_now.date() + timedelta(days=1)
    parts = []
    if with_markets and local_now.weekday() < 5 and getattr(C, "EVENING_MARKETS", True):
        try:
            parts.append(markets_today_text(local_now))
        except Exception as e:  # noqa: BLE001
            log(f"Trhy dnes selhaly: {e}")
    has_any = any(event_date(e) == tomorrow for e in events)
    if has_any or tomorrow.weekday() < 5:
        # V neděli večer přidej výhled celého týdne
        week = C.WEEKLY_OUTLOOK_ON_MONDAY and tomorrow.weekday() == 0
        parts.append(day_summary(events, tomorrow, "zítra", week_outlook=week))
    elif parts:
        parts.append("📅 <b>Zítra víkend</b> – žádné červené události.")
    return "\n\n".join(parts) if parts else None


def send_results(st, events, local_now, tg, silent):
    """Po zveřejnění červené události pošle skutečnost vs. odhad (z forexfactory.com)."""
    if not getattr(C, "SEND_RESULTS", True):
        return
    t = time.time()
    sent = st.setdefault("results_sent", {})
    pending = [e for e in events if not e["all_day"] and e["impact"] == "High" and e["key"] not in sent
               and 60 <= t - e["ts"] <= 3 * 3600]
    if not pending:
        return
    enriched = {e["key"]: e for e in with_actuals(pending, local_now)}
    by_time = {}
    for e in pending:
        ev = enriched.get(e["key"], e)
        if ev.get("actual"):
            by_time.setdefault(ev["ts"], []).append(ev)
        elif t - e["ts"] > 2.5 * 3600:
            sent[e["key"]] = t  # výsledek se neobjevil, vzdát
    for ts, evs in sorted(by_time.items()):
        lines = [f"📊 <b>Výsledky ({fmt_time(ts)})</b>"] + [fmt_result_line(e) for e in evs]
        if tg.send("\n".join(lines), silent=silent):
            for e in evs:
                sent[e["key"]] = t


def calendar_tasks(st, all_events, local_now, tg, silent):
    events = daily_only(all_events)
    today = local_now.date().isoformat()
    # Ranní přehled
    if C.DAILY_SUMMARY_HOUR is not None:
        if local_now.hour >= C.DAILY_SUMMARY_HOUR and st["summary_date"] != today:
            if tg.send(daily_summary(events, local_now), silent=silent):
                st["summary_date"] = today
    # Večerní přehled na zítřek
    eh = getattr(C, "EVENING_SUMMARY_HOUR", None)
    if eh is not None and local_now.hour >= eh and st.get("evening_date") != today:
        msg = evening_summary(events, local_now, with_markets=True)
        if msg is None or tg.send(msg, silent=silent):
            st["evening_date"] = today
    # Upozornění před událostí (seskupené podle času)
    t = time.time()
    due = [
        e
        for e in events
        if not e["all_day"]
        and e["key"] not in st["reminded"]
        and 0 < (e["ts"] - t) / 60 <= C.REMIND_MINUTES_BEFORE + 5
    ]
    by_time = {}
    for e in due:
        by_time.setdefault(e["ts"], []).append(e)
    for ts, evs in sorted(by_time.items()):
        mins = max(1, round((ts - t) / 60))
        lines = [f"⏰ <b>Za {mins} min ({fmt_time(ts)})</b>"] + [fmt_event_line(e) for e in evs]
        if tg.send("\n".join(lines), silent=silent):
            for e in evs:
                st["reminded"][e["key"]] = t
    send_results(st, events, local_now, tg, silent)
    st["results_sent"] = {k: v for k, v in st.get("results_sent", {}).items() if t - v < 8 * 86400}
    # Události, které právě vycházejí → bonus pro headliny s výsledkem
    return [e for e in events if not e["all_day"] and -5 * 60 <= t - e["ts"] <= 60 * 60]


# ---------------------------------------------------------------------------
# Týdenní souhrn
# ---------------------------------------------------------------------------
def _closes_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1mo&interval=1d"
    data = json.loads(fetch(url, timeout=15))
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    out = [(datetime.fromtimestamp(t, UTC).date(), c) for t, c in zip(ts, closes) if c is not None]
    if not out:
        raise ValueError("prázdná data")
    return out


def _closes_stooq(symbol):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i=d"
    text = fetch(url, timeout=15).decode("utf-8", "ignore").strip().splitlines()
    out = []
    for line in text[1:]:
        parts = line.split(",")
        if len(parts) >= 5:
            try:
                out.append((datetime.fromisoformat(parts[0]).date(), float(parts[4])))
            except ValueError:
                continue
    if not out:
        raise ValueError("prázdná data")
    return out[-40:]


def get_closes(ysym, ssym):
    """Denní závěry (Yahoo, záloha Stooq). Vrací (closes, chyba)."""
    err = ""
    for fn, sym in ((_closes_yahoo, ysym), (_closes_stooq, ssym)):
        if not sym:
            continue
        try:
            return fn(sym), ""
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:80]
    return None, err


def daily_changes():
    """Změna posledního (i rozběhnutého dnešního) dne proti předchozímu závěru."""
    result = []
    for group, assets in C.MARKETS:
        rows = []
        for name, ysym, ssym, kind in assets:
            closes, err = get_closes(ysym, ssym)
            if not closes or len(closes) < 2:
                rows.append((name, None, err or "málo dat", kind))
                continue
            last, ref = closes[-1][1], closes[-2][1]
            if kind == "yield":
                if last > 20:
                    last, ref = last / 10, ref / 10
                rows.append((name, last, (last - ref) * 100, kind))
            else:
                rows.append((name, last, (last / ref - 1) * 100, kind))
        result.append((group, rows))
    return result


def markets_today_text(local_now):
    lines = [f"📈 <b>Trhy dnes – stav k {local_now.strftime('%H:%M')}</b> <i>(USA ještě obchoduje)</i>"]
    for group, rows in daily_changes():
        lines += ["", f"<b>{group}</b>"] + [fmt_change_row(*r) for r in rows]
    return "\n".join(lines)


def weekly_changes(local_now, weeks_back=0):
    """Vrátí [(skupina, [(název, poslední, změna, typ) | (název, None, chyba, typ)])]."""
    monday = local_now.date() - timedelta(days=local_now.weekday() + 7 * weeks_back)
    friday = monday + timedelta(days=4)
    result = []
    for group, assets in C.MARKETS:
        rows = []
        for name, ysym, ssym, kind in assets:
            closes, err = None, ""
            for fn, sym in ((_closes_yahoo, ysym), (_closes_stooq, ssym)):
                if not sym:
                    continue
                try:
                    closes = fn(sym)
                    break
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"[:80]
            if not closes:
                rows.append((name, None, err, kind))
                continue
            upto = [(d, c) for d, c in closes if d <= friday]
            if not upto:
                rows.append((name, None, "chybí data", kind))
                continue
            last_d, last = upto[-1]
            prev = [c for d, c in closes if d < monday]
            if not prev:
                rows.append((name, None, "chybí data za minulý týden", kind))
                continue
            ref = prev[-1]
            if kind == "yield":
                if last > 20:  # některé zdroje uvádějí výnos ×10
                    last, ref = last / 10, ref / 10
                rows.append((name, last, (last - ref) * 100, kind))
            else:
                rows.append((name, last, (last / ref - 1) * 100, kind))
        result.append((group, rows))
    return result


def fmt_change_row(name, last, chg, kind):
    if last is None:
        return f"▫️ {html.escape(name)}: <i>data nedostupná</i>"
    arrow = "🟢 ▲" if chg > 0.005 else ("🔴 ▼" if chg < -0.005 else "⚪ ►")
    if kind == "yield":
        return f"{arrow} {html.escape(name)}: <b>{last:.2f} %</b> ({chg:+.0f} bp)"
    val = f"{last:,.4f}" if last < 10 else f"{last:,.2f}"
    return f"{arrow} {html.escape(name)}: <b>{val.replace(',', ' ')}</b> ({chg:+.2f} %)"


def _md_to_html(text):
    """Minimalistický převod **tučně** z AI textu do Telegram HTML."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"^#+\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    return t


def call_claude(prompt, max_tokens=1800):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    body = json.dumps({
        "model": getattr(C, "WEEKLY_AI_MODEL", "claude-sonnet-5-5"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip() or None
    except urllib.error.HTTPError as e:
        log(f"Claude API chyba {e.code}: {e.read().decode(errors='ignore')[:300]}")
    except Exception as e:  # noqa: BLE001
        log(f"Claude API chyba: {e}")
    return None


def week_headlines(monday, friday):
    """Titulky top agentur za daný týden z Google News (after/before)."""
    items = []
    for q in getattr(C, "WEEKLY_NEWS_QUERIES", []):
        url = C.gnews_range(q, monday.isoformat(), (friday + timedelta(days=1)).isoformat())
        try:
            time.sleep(1.5)
            items += parse_feed(fetch(url), {"name": "Google News", "trust": 0, "google": True})
        except Exception as e:  # noqa: BLE001
            log(f"FAIL týdenní titulky: {e}")
    scored, toks = [], []
    for it in sorted(items, key=lambda x: -score_item(x, [], relevance=False)[0]):
        sc = score_item(it, [], relevance=False)[0]
        if sc < 5:
            continue
        tk = tokens(it["title"])
        if any(similarity(tk, t) >= C.DUPLICATE_SIMILARITY for t in toks):
            continue
        toks.append(tk)
        scored.append((sc, it))
    return scored[:45]


def _fmt_cal_for_ai(evs):
    out = []
    for e in evs:
        if e.get("holiday"):
            out.append(f"- {event_date(e).strftime('%a %d.%m.')} {e['country']} {e['title']} (svátek)")
        else:
            act = f", skutečnost {e['actual']}" if e.get("actual") else ""
            out.append(f"- {event_date(e).strftime('%a %d.%m.')} {e['country']} {e['title']} "
                       f"(odhad {e.get('forecast') or '-'}, předch. {e.get('previous') or '-'}{act})")
    return "\n".join(out)


def weekly_report(st, events, local_now, weeks_back=0):
    monday = local_now.date() - timedelta(days=local_now.weekday() + 7 * weeks_back)
    friday = monday + timedelta(days=4)
    next_monday = monday + timedelta(days=7)
    changes = weekly_changes(local_now, weeks_back)
    t0 = datetime.combine(monday, datetime.min.time(), TZ).timestamp()
    t1 = t0 + 7 * 86400
    news = sorted([w for w in st.get("week_log", []) if t0 <= w["t"] < t1], key=lambda w: -w.get("score", 0))
    agency = week_headlines(monday, friday)

    # Kalendář hodnoceného a příštího týdne: nejdřív forexfactory.com (má i skutečné hodnoty),
    # záloha = JSON feed aktuálního týdne
    pool = []
    for wk in (monday, next_monday):
        try:
            evs = ff_week_events(wk)
            pool += evs
            log(f"OK   ForexFactory web {wk}: {len(evs)} událostí")
        except Exception as e:  # noqa: BLE001
            log(f"FAIL ForexFactory web {wk}: {type(e).__name__}: {e}")
    keys = {e["key"] for e in pool}
    pool += [e for e in events if e["key"] not in keys]
    pool = list({e["key"]: e for e in pool}.values())
    pool.sort(key=lambda e: e["ts"])
    week_events = [e for e in pool if monday <= event_date(e) <= friday]
    next_events = [e for e in pool if next_monday <= event_date(e) <= next_monday + timedelta(days=4)]

    head = f"📊 <b>Týden {monday.day}. {monday.month}. – {friday.day}. {friday.month}. v kostce</b>"
    lines = [head]
    for group, rows in changes:
        lines += ["", f"<b>{group}</b>"] + [fmt_change_row(*r) for r in rows]

    # Podklady pro AI
    price_txt = "\n".join(
        f"{group}: " + "; ".join(
            f"{n} {('n/a' if l is None else (f'{l:.2f} % ({c:+.0f} bp)' if k == 'yield' else f'{l:.4g} ({c:+.2f} %)'))}"
            for n, l, c, k in rows)
        for group, rows in changes)
    bot_txt = "\n".join(
        f"- [{datetime.fromtimestamp(w['t'], TZ).strftime('%a %H:%M')}] {w['title']} ({w.get('pub', '')})"
        for w in news[:40]) or "(bot v tomto týdnu ještě nic nezachytil)"
    ag_txt = "\n".join(
        f"- [{datetime.fromtimestamp(it['published'], TZ).strftime('%a %d.%m.') if it.get('published') else '?'}] "
        f"{it['title']} ({it.get('publisher', '')})"
        for sc, it in agency) or "(nedostupné)"
    cal_txt = _fmt_cal_for_ai(week_events) or "(kalendář tohoto týdne není k dispozici)"
    next_txt = (_fmt_cal_for_ai(next_events) if next_events else
                "(kalendář příštího týdne zatím NENÍ k dispozici – netvrď, že žádné události nebudou; "
                "zmiň jen obecně, co je dobré sledovat)")

    prompt = (
        "Jsi zkušený makro/FX analytik. Napiš česky stručné týdenní shrnutí trhů pro tradera, "
        "který sleduje dolar, indexy a komodity. Vycházej z týdenních změn cen, ze zpráv a z kalendáře níže. "
        "Pro každou skupinu (💵 Dolar, 📈 Indexy, 🛢 Komodity) napiš 2–4 věty: co se pohnulo, které konkrétní "
        "zprávy nebo data (např. CPI, PPI, Fed) to nejspíš způsobily – u dat uveď skutečnost vs. odhad, pokud ji "
        "zprávy obsahují – a co to znamená pro dané aktivum (mechanismus, např. vyšší inflace → jestřábí Fed → "
        "vyšší výnosy → silnější dolar). Pak 1–3 věty 'Na co si dát pozor příští týden'. Opírej se jen o uvedené "
        "zprávy a data; když souvislost není jasná, napiš to. Žádné investiční doporučení. Formát: nadpis "
        "skupiny na samostatném řádku tučně (**…**), pak text. Max. ~2000 znaků.\n\n"
        f"TÝDENNÍ ZMĚNY (pátek vs. předchozí pátek):\n{price_txt}\n\n"
        f"ČERVENÉ UDÁLOSTI HODNOCENÉHO TÝDNE:\n{cal_txt}\n\n"
        f"ZPRÁVY, KTERÉ BOT POSLAL BĚHEM TÝDNE (squawk, často s výsledky dat):\n{bot_txt}\n\n"
        f"TITULKY TOP AGENTUR ZA TÝDEN (Reuters, Bloomberg, WSJ, FT, CNBC, AP):\n{ag_txt}\n\n"
        f"ČERVENÉ UDÁLOSTI PŘÍŠTÍHO TÝDNE:\n{next_txt}\n"
    )
    ai = call_claude(prompt)
    if ai:
        lines += ["", "🧠 <b>Co za tím bylo</b>", _md_to_html(ai)]
    else:
        lines += ["", "📰 <b>Hlavní zprávy týdne</b>"]
        pool_news = [{"title": w["title"], "cz": w.get("cz"), "link": w.get("link", ""),
                      "tags": w.get("tags", [])} for w in news]
        pool_news += [{"title": it["title"], "cz": None, "link": it.get("link", ""),
                       "tags": asset_tags(it["title"])} for sc, it in agency]
        any_news = False
        for group, _ in changes:
            emoji = group.split(" ", 1)[0]
            items = [w for w in pool_news if any(tg.split(" ", 1)[0] == emoji for tg in w.get("tags", []))][:3]
            if items:
                any_news = True
                lines.append(f"<b>{group}</b>")
                for w in items:
                    lnk = html.escape(w.get("link", ""), quote=True)
                    txt = html.escape(w.get("cz") or translate(w["title"]))
                    lines.append(f'• <a href="{lnk}">{txt}</a>' if lnk else f"• {txt}")
        if not any_news:
            lines.append("Žádné zaznamenané zprávy.")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            lines.append("\n<i>Tip: s API klíčem Claude (ANTHROPIC_API_KEY v GitHub Secrets) sem přibude "
                         "vysvětlení, co zprávy znamenaly pro jednotlivá aktiva.</i>")
    lines.append("\n<i>Není investiční doporučení.</i>")
    return "\n".join(lines)


def weekly_due(st, local_now):
    if not getattr(C, "WEEKLY_REPORT", False):
        return False, ""
    key = f"{local_now.isocalendar()[0]}-W{local_now.isocalendar()[1]}"
    return (local_now.weekday() == C.WEEKLY_REPORT_WEEKDAY and local_now.hour >= C.WEEKLY_REPORT_HOUR
            and st.get("weekly_key") != key), key


# ---------------------------------------------------------------------------
# Příkazy z Telegramu (/dnes, /zitra, /tyden, /minuly, /top, /stav)
# ---------------------------------------------------------------------------
COMMANDS = [
    ("dnes", "Dnešní červené události a svátky"),
    ("zitra", "Zítřejší červené události a svátky"),
    ("tyden", "Souhrn tohoto týdne (ceny + komentář)"),
    ("minuly", "Souhrn minulého týdne"),
    ("top", "Nejdůležitější headliny posledních hodin"),
    ("stav", "Stav zdrojů bota"),
    ("pomoc", "Seznam příkazů"),
]


def help_text():
    lines = ["🤖 <b>Příkazy</b> (odpověď do ~5 min):"]
    lines += [f"/{c} – {d}" for c, d in COMMANDS]
    return "\n".join(lines)


def health_text(st):
    lines = ["<b>Zdroje:</b>"]
    for name, h in st["health"].items():
        if h.get("fails"):
            lines.append(f"❌ {html.escape(name)} – <code>{html.escape(h.get('error', ''))}</code>")
        else:
            lines.append(f"✅ {html.escape(name)} – {h.get('count', 0)} položek")
    return "\n".join(lines)


def top_text(items, limit=10):
    t = time.time()
    fresh = [it for it in items if not it.get("published") or t - it["published"] <= C.MAX_AGE_HOURS * 3600]
    scored = sorted(((score_item(it, [])[0], it) for it in fresh), key=lambda x: -x[0])
    uniq, seen = [], set()
    for sc, it in scored:
        if it["title"] not in seen:
            uniq.append((sc, it))
            seen.add(it["title"])
    lines = [f"<b>Top headliny za posledních {C.MAX_AGE_HOURS} h</b> (práh {C.SCORE_THRESHOLD}):"]
    for sc, it in uniq[:limit]:
        mark = "✅" if sc >= C.SCORE_THRESHOLD else "▫️"
        lines.append(f"{mark} [{sc if sc < 99 else 'MAX'}] {html.escape(it['title'])} <i>({html.escape(it['publisher'])})</i>")
    if not uniq:
        lines.append("— nic čerstvého —")
    return "\n".join(lines)


def handle_commands(st, tg, events, items, local_now):
    # Jednou zaregistruj menu příkazů v Telegramu
    if st.get("commands_v") != 1:
        if tg.api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}) is not None:
            st["commands_v"] = 1
    updates = tg.api("getUpdates", {"offset": st.get("tg_offset", 0), "timeout": 0,
                                    "allowed_updates": ["message"]}) or []
    for u in updates:
        st["tg_offset"] = max(st.get("tg_offset", 0), u.get("update_id", 0) + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(tg.chat):
            continue  # odpovídáme jen tobě
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        cmd = {"kalendar": "dnes", "kalendář": "dnes", "zítra": "zitra", "týden": "tyden", "minulý": "minuly",
               "help": "pomoc", "start": "pomoc"}.get(cmd, cmd)
        log(f"CMD  {cmd}")
        if cmd == "dnes":
            tg.send(day_summary(with_actuals(daily_only(events), local_now), local_now.date(), "dnes"))
        elif cmd == "zitra":
            tg.send(day_summary(daily_only(events), local_now.date() + timedelta(days=1), "zítra"))
        elif cmd == "tyden":
            tg.send("⏳ Připravuji souhrn tohoto týdne…")
            tg.send(weekly_report(st, events, local_now, 0))
        elif cmd == "minuly":
            tg.send("⏳ Připravuji souhrn minulého týdne…")
            tg.send(weekly_report(st, events, local_now, 1))
        elif cmd == "top":
            tg.send(top_text(items))
        elif cmd == "stav":
            tg.send(health_text(st))
        else:
            tg.send(("Tomu nerozumím. " if cmd != "pomoc" else "") + help_text())


# ---------------------------------------------------------------------------
# Hlavní běh
# ---------------------------------------------------------------------------
def select_alerts(st, items, active_events):
    t = time.time()
    recent_sent = [set(s["tok"]) for s in st["sent"] if t - s["t"] < C.DUPLICATE_WINDOW_HOURS * 3600]
    picked, picked_tok, seen_links = [], [], set()
    scored_all = []
    for it in items:
        if it["id"] in st["seen"]:
            continue
        st["seen"][it["id"]] = t
        if it.get("published") and t - it["published"] > C.MAX_AGE_HOURS * 3600:
            continue
        score, reasons = score_item(it, active_events)
        scored_all.append((score, it, reasons))
    scored_all.sort(key=lambda x: -x[0])
    midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sent_today = sum(1 for s_ in st["sent"] if s_["t"] >= midnight)
    for score, it, reasons in scored_all:
        cap = getattr(C, "DAILY_SOFT_CAP", None)
        limit = C.SCORE_THRESHOLD
        if cap and sent_today + len(picked) >= cap:
            limit = max(limit, getattr(C, "SCORE_THRESHOLD_AFTER_CAP", limit))
        if score < limit:
            continue
        tok = tokens(it["title"])
        if it.get("link") in seen_links:
            continue
        if any(similarity(tok, s) >= C.DUPLICATE_SIMILARITY for s in recent_sent + picked_tok):
            log(f"DUP  {it['title']}")
            continue
        picked.append((score, it))
        picked_tok.append(tok)
        seen_links.add(it.get("link"))
        log(f"SEND [{score}] {it['title']}  {reasons}")
    return picked


def run(args):
    st = load_state(args.state)
    tg = Telegram(dry_run=args.dry_run)
    local_now = datetime.now(TZ)
    silent = is_quiet(local_now)
    health_alerts = []

    events, _ = refresh_calendar(st, force=args.test)
    items = collect_items(st, health_alerts)

    if getattr(args, "weekly", False):
        tg.send(weekly_report(st, events, local_now, getattr(args, "weeks_back", 0)))
        if not args.test:
            save_state(st, args.state)
            return

    if args.test:
        run_test(st, tg, items, events, local_now)
        save_state(st, args.state)
        return

    handle_commands(st, tg, events, items, local_now)

    # První spuštění: jen si zapamatuj aktuální zprávy, ať nepřijde lavina starých
    if not st["initialized"]:
        for it in items:
            st["seen"][it["id"]] = time.time()
        if C.DAILY_SUMMARY_HOUR is not None and local_now.hour >= C.DAILY_SUMMARY_HOUR:
            st["summary_date"] = local_now.date().isoformat()
        eh = getattr(C, "EVENING_SUMMARY_HOUR", None)
        if eh is not None and local_now.hour >= eh:
            st["evening_date"] = local_now.date().isoformat()
        st["initialized"] = True
        tg.send(
            "✅ <b>Red News bot běží.</b>\nOd teď ti budu posílat jen zprávy s velkým dopadem "
            "na USD, indexy a komodity + červené události z kalendáře."
        )
        save_state(st, args.state)
        return

    active = calendar_tasks(st, events, local_now, tg, silent)
    picked = select_alerts(st, items, active)

    if picked:
        if len(picked) <= C.MAX_SEPARATE_MESSAGES_PER_RUN:
            for score, it in picked:
                tg.send(format_alert(it, score), silent=silent)
        else:
            tg.send(format_digest(picked), silent=silent)
        t = time.time()
        for score, it in picked:
            st["sent"].append({"t": t, "tok": sorted(tokens(it["title"])), "title": it["title"]})
            st["week_log"].append({
                "t": t, "title": it["title"], "cz": translate(it["title"]), "pub": it["publisher"],
                "score": score, "tags": asset_tags(it["title"]), "link": it.get("link", ""),
            })

    for msg in health_alerts:
        tg.send(msg, silent=True)

    due, wkey = weekly_due(st, local_now)
    if due and tg.send(weekly_report(st, events, local_now), silent=silent):
        st["weekly_key"] = wkey

    save_state(st, args.state)
    log(f"Hotovo: {len(items)} položek, posláno {len(picked)}.")


def run_test(st, tg, items, events, local_now):
    h = st["health"].setdefault("ForexFactory web", {"fails": 0, "alerted": False, "error": "", "count": 0})
    try:
        evs = ff_week_events(local_now.date() - timedelta(days=local_now.weekday() + 7))
        h.update(fails=0, error="", count=len(evs))
    except Exception as e:  # noqa: BLE001
        h.update(fails=1, error=f"{type(e).__name__}: {e}"[:200], count=0)
    tg.send("🧪 <b>Test Red News bota</b>\n\n" + health_text(st) + "\n\n" + top_text(items))
    tg.send(daily_summary(daily_only(events), local_now))
    ev = evening_summary(daily_only(events), local_now)
    if ev:
        tg.send(ev)
    tg.send(help_text())


def main():
    ap = argparse.ArgumentParser(description="Red News Telegram bot")
    ap.add_argument("--test", action="store_true", help="pošle testovací zprávu a stav zdrojů")
    ap.add_argument("--dry-run", action="store_true", help="nic neposílá, jen vypíše")
    ap.add_argument("--weekly", action="store_true", help="pošle týdenní souhrn hned")
    ap.add_argument("--weeks-back", type=int, default=0, help="souhrn za N týdnů zpět (1 = minulý týden)")
    ap.add_argument("--state", default="state.json", help="soubor se stavem")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
