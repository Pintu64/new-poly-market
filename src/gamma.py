"""Polymarket Gamma API helpers: fetch events and build match records.

A football match on Polymarket is a family of events sharing a base slug:
    base event   : mls-nyr-orl-2026-08-01            (moneyline: 1X2)
    more markets : mls-nyr-orl-2026-08-01-more-markets (totals/spreads/BTTS)
    corners      : mls-nyr-orl-2026-08-01-total-corners  (ignored here)

This module normalises those into one MatchRecord per match.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
PAGE_SIZE = 500
MAX_PAGES = 6
RETRY = 3

DATE_RE = re.compile(
    r"^(?P<base>.+)-(?P<date>\d{4}-\d{2}-\d{2})(?P<suffix>-.*)?$"
)

GENERIC_TAGS = {"sports", "games", "soccer", "sports-betting", "soccer-gtm",
                "soccer-trsk", "mma", "ufc"}

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def gamma_events(params):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{GAMMA_EVENTS}?{query}", headers=UA)
    last_err = None
    for attempt in range(RETRY):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_err = exc
            time.sleep(2 + attempt)
    raise RuntimeError(f"gamma request failed after retries: {last_err}")


def fetch_open_soccer_events():
    events = []
    for page in range(MAX_PAGES):
        batch = gamma_events({
            "tag_slug": "soccer",
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
        })
        events.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return events


def fetch_event_by_slug(slug):
    found = gamma_events({"slug": slug, "limit": 1})
    return found[0] if found else None


def split_slug(slug):
    m = DATE_RE.match(slug or "")
    if not m:
        return None
    return m.group("base"), m.group("suffix") or ""


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
        import re as _re
        if not _re.search(r"[+-]\d{2}:?\d{2}$", value):
            value += "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def league_of(event):
    labels = [
        t.get("label", "")
        for t in event.get("tags", [])
        if t.get("slug") not in GENERIC_TAGS
    ]
    return labels[0] if labels else ""


def get_kickoff(event):
    for market in event.get("markets", []):
        dt = parse_dt(market.get("gameStartTime"))
        if dt:
            return dt
    return parse_dt(event.get("endDate"))


def build_matches(events, league_tags=None):
    """Group Gamma events into MatchRecord dicts keyed by base slug."""
    wanted = set(league_tags or [])
    by_base = {}

    for event in events:
        parts = split_slug(event.get("slug"))
        if not parts:
            continue
        base, suffix = parts
        tags = {t.get("slug", "") for t in event.get("tags", [])}
        if wanted and not (wanted & tags):
            continue

        rec = by_base.setdefault(base, {
            "base_slug": base,
            "event_id": None,
            "title": "",
            "team1": "",
            "team2": "",
            "icon": "",
            "league": "",
            "kickoff": None,
            "created_at": None,
            "extra_slug": "",
            "moneyline": [],          # [(team, price)] home, away
            "draw": None,
            "totals": {},             # line -> over price
            "spreads": {},
            "btts": None,
        })
        rec["created_at"] = min(rec["created_at"] or event.get("createdAt") or "", event.get("createdAt") or "")

        if suffix == "":
            rec["event_id"] = str(event.get("id"))
            rec["title"] = event.get("title", "")
            rec["icon"] = event.get("icon", "")
            rec["league"] = league_of(event)
            kickoff = get_kickoff(event)
            if kickoff:
                rec["kickoff"] = kickoff
            for market in event.get("markets", []):
                if (market.get("sportsMarketType") or "") != "moneyline":
                    continue
                if not market.get("active") or market.get("closed"):
                    continue
                name = (market.get("groupItemTitle") or "").strip()
                prices = _prices(market)
                if not name or not prices:
                    continue
                label = re.sub(r"\s*\(.*vs\.?.*\)\s*", "", name).strip()
                if label.lower().startswith("draw"):
                    rec["draw"] = prices[0]
                else:
                    label = re.sub(r"\s*\(.*\)\s*$", "", label).strip()
                    rec["moneyline"].append((label, prices[0]))
        elif suffix == "-more-markets":
            rec["extra_slug"] = event.get("slug", "")
            kickoff = get_kickoff(event)
            if kickoff and not rec["kickoff"]:
                rec["kickoff"] = kickoff
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
                elif mtype == "both_teams_to_score":
                    rec["btts"] = prices[0]
                elif mtype == "spreads" and line is not None:
                    name2 = (market.get("groupItemTitle") or "").strip()
                    rec["spreads"][f"{name2} {line:+.1f}"] = prices[0]
    return by_base


def finalize_match(rec):
    ml = rec.get("moneyline") or []
    if len(ml) >= 2 and not rec.get("team1"):
        rec["team1"], rec["team2"] = ml[0][0], ml[1][0]
    return rec
