"""Polymarket Gamma API: fetch events, build match records, price snapshots.

A football match is a family of events sharing a base slug:
    base   : mls-nyr-orl-2026-08-01              (moneyline 1X2)
    extra  : mls-nyr-orl-2026-08-01-more-markets (totals / spreads / BTTS)
This module normalises them into one MatchRecord per match, including a
labelled price snapshot used for odds-move detection.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
PAGE_SIZE = 500
MAX_PAGES = 6
RETRY = 3
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

DATE_RE = re.compile(r"^(?P<base>.+)-(?P<date>\d{4}-\d{2}-\d{2})(?P<suffix>-.*)?$")

GENERIC_TAGS = {"sports", "games", "soccer", "sports-betting", "soccer-gtm",
                "soccer-trsk", "boxing"}


def gamma_events(params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{GAMMA_EVENTS}?{query}", headers=UA)
    last_err = None
    for attempt in range(RETRY):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(2 + attempt)
    raise RuntimeError(f"gamma request failed after retries: {last_err}")


def fetch_open_soccer_events():
    events = []
    for page in range(MAX_PAGES):
        batch = gamma_events({
            "tag_slug": "soccer", "active": "true", "closed": "false",
            "archived": "false", "limit": PAGE_SIZE, "offset": page * PAGE_SIZE,
        })
        events.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return events


def fetch_events_for_base(base):
    """Fetch every event belonging to one match family (fresh)."""
    for suffix in ("", "-more-markets"):
        slug = base + suffix
        yield from gamma_events({"slug": slug, "limit": 2})


def split_slug(slug):
    m = DATE_RE.match(slug or "")
    if not m:
        return None
    return m.group("base"), (m.group("suffix") or "")


def _prices(market):
    try:
        return [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return []


def _outcomes(market):
    try:
        return list(json.loads(market.get("outcomes") or "[]"))
    except (ValueError, TypeError):
        return []


def parse_dt(value):
    if not value:
        return None
    try:
        value = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
        if not re.search(r"[+-]\d{2}:?\d{2}$", value):
            value += "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def league_of(event):
    for t in event.get("tags", []):
        if t.get("slug") not in GENERIC_TAGS:
            return t.get("label") or ""
    return ""


def get_kickoff(event):
    for market in event.get("markets", []):
        dt = parse_dt(market.get("gameStartTime"))
        if dt:
            return dt
    return parse_dt(event.get("endDate"))


def new_record(base):
    return {
        "base_slug": base, "event_id": None, "title": "", "team1": "", "team2": "",
        "icon": "", "league": "", "kickoff": None, "extra_slug": "",
        "moneyline": [], "draw": None, "totals": {}, "btts": None,
        "prices": {}, "vol24": 0.0,
    }


def build_one(event, rec):
    """Fold one Gamma event into a Record."""
    parts = split_slug(event.get("slug"))
    if not parts:
        return rec
    _base, suffix = parts
    rec["created_at"] = rec.get("created_at") or event.get("createdAt")

    vol = sum(
        float(m.get("volume24hr") or 0) for m in event.get("markets", [])
    )
    rec["vol24"] = rec.get("vol24", 0.0) + vol

    if suffix == "":
        rec["event_id"] = str(event.get("id"))
        rec["title"] = event.get("title", "")
        rec["icon"] = event.get("icon", "")
        rec["league"] = league_of(event)
        ko = get_kickoff(event)
        if ko and not rec["kickoff"]:
            rec["kickoff"] = ko
        for market in event.get("markets", []):
            if (market.get("sportsMarketType") or "") != "moneyline":
                continue
            if not market.get("active") or market.get("closed"):
                continue
            name = (market.get("groupItemTitle") or "").strip()
            prices = _prices(market)
            if not name or not prices:
                continue
            label = re.sub(r"\s*\(.*?\)\s*", "", name).strip()
            if label.lower().startswith("draw"):
                rec["draw"] = prices[0]
                rec["prices"]["ML Draw"] = prices[0]
            else:
                label = re.sub(r"\s*\(.*\)\s*$", "", label).strip()
                rec["moneyline"].append((label, prices[0]))
                rec["prices"]["ML " + label] = prices[0]
    elif suffix == "-more-markets":
        rec["extra_slug"] = event.get("slug", "")
        ko = get_kickoff(event)
        if ko and not rec["kickoff"]:
            rec["kickoff"] = ko
        for market in event.get("markets", []):
            mtype = market.get("sportsMarketType") or ""
            prices = _prices(market)
            if not prices or not market.get("active") or market.get("closed"):
                continue
            try:
                line = float(market.get("line"))
            except (TypeError, ValueError):
                line = None
            if mtype == "totals" and line is not None:
                outs = _outcomes(market)
                if outs and outs[0].lower().startswith("over"):
                    rec["totals"][round(line, 2)] = prices[0]
                    rec["prices"]["Over %g" % line] = prices[0]
            elif mtype == "both_teams_to_score":
                rec["btts"] = prices[0]
                rec["prices"]["BTTS"] = prices[0]
    return rec


def build_matches(events, league_tags=None):
    """Return {base_slug: Record} from raw Gamma events."""
    wanted = set(league_tags or [])
    by_base = {}
    for event in events:
        parts = split_slug(event.get("slug"))
        if not parts:
            continue
        base, _suffix = parts
        if wanted:
            tags = {t.get("slug", "") for t in event.get("tags", [])}
            if not (wanted & tags):
                continue
        by_base[base] = build_one(event, by_base.get(base) or new_record(base))
    return by_base


def finalize_match(rec):
    ml = rec.get("moneyline") or []
    if len(ml) >= 2 and not rec.get("team1"):
        rec["team1"], rec["team2"] = ml[0][0], ml[1][0]
    return rec


def is_valid_match(rec):
    if not rec.get("event_id"):
        return False
    if len(rec.get("moneyline") or []) < 2:
        return False
    return rec.get("draw") is not None or " vs. " in (rec.get("title") or "")
