"""Polymarket Football Bot — Telegram notifier with match predictions.

What it sends (professional, emoji-rich messages):
  🆕  new match listed            (with prediction card)
  ⏰  kickoff reminders           (30 & 5 min before, with prediction)
  ✅  results                     (when a match market resolves)
  🌅  daily digest                (today's matches + predictions)

Commands:
  /start     register this chat
  /stop      unregister
  /matches   upcoming matches list
  /predict quarter united      predictions for a specific match
  /leagues   leagues currently tracked
  /help      help

Setup: paste bot token into config.json, then:  python src/bot.py
Stdlib only.
"""

import html
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import gamma  # noqa: E402
import predict  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
POLL_INTERVAL_DEFAULT = 90


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


CONFIG = load_json(CONFIG_PATH, {})
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG.get("bot_token") or ""
POLL_INTERVAL = int(CONFIG.get("poll_interval_seconds", POLL_INTERVAL_DEFAULT))
REMINDER_MINUTES = CONFIG.get("reminder_minutes", [30, 5])
LEAGUE_TAGS = CONFIG.get("league_tags", [])
DIGEST_HOUR_UTC = CONFIG.get("digest_hour_utc")

STATE = load_json(STATE_PATH, {"chats": [], "matches": {}, "offset": 0, "digest": ""})


# ---------------------------------------------------------------- telegram

def tg(method, payload=None, timeout=35):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(body)
    return body["result"]


def send(chat_id, text, buttons=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg("sendMessage", payload)
        return True
    except Exception as exc:
        log(f"send to {chat_id} failed: {exc}")
        return False


def broadcast(text, buttons=None):
    for chat_id in list(STATE["chats"]):
        send(chat_id, text, buttons)


# ---------------------------------------------------------------- rendering

EVENT_URL = "https://polymarket.com/event/{slug}"


def link_btn(text, slug):
    return {"text": f"🔗 {text}", "url": EVENT_URL.format(slug=slug)}


def match_buttons(rec, label="Trade this match"):
    slug = rec["extra_slug"] or rec["base_slug"]
    return [[link_btn(label, slug)]]


def kickoff_str(dt):
    return dt.strftime("%a %d %b · %H:%M UTC") if dt else "⏳ TBA"


def prediction_card(rec, pred, compact=False):
    lines = []
    lines.append("📊 <b>PREDICTION</b>")

    if pred["best"]:
        team, p = pred["best"]
        emoji = "🤝" if team == "Draw" else "⚡"
        lines.append(f"{emoji} <b>{html.escape(team)}</b> — <b>{predict.pct(p)}</b> win probability")

    if pred["probs_1x2"] and not compact:
        probs = " · ".join(
            f"{html.escape(t if t != 'Draw' else 'Draw')} {predict.pct(p)}"
            for t, p in pred["probs_1x2"]
        )
        lines.append(f" ➜ <i>1X2 market:</i> {probs}")

    if pred["ladder"]:
        lines.append("<b>Goals — over line (market | model):</b>")
        for row in pred["ladder"][:5]:
            mprice, model = row.get("market"), row.get("model")
            bar_p = mprice if mprice is not None else model
            mark = ""
            if mprice is not None and model is not None:
                d = abs(mprice - model)
                if d <= 0.05:
                    mark = " ✅"
                elif model > mprice + 0.12:
                    mark = " 🔼"
                elif mprice > model + 0.12:
                    mark = " 🔽"
            tag = f"{row['line']:g}"
            lines.append(
                f" <code>Over</code> <b>{tag}</b> {predict.bar(bar_p)} "
                f"{predict.pct(mprice)} | <i>{predict.pct(model)}</i>{mark}"
            )

    if pred["btts_market"] is not None or pred["btts_model"] is not None:
        b = pred["btts_market"] if pred["btts_market"] is not None else pred["btts_model"]
        lines.append(f"🥅 <b>Both teams to score:</b> {predict.pct(b)}")

    if pred["exp_score"]:
        t1, t2 = pred["exp_score"]
        lines.append(f"📌 <i>Model expected score ≈ {t1} : {t2}</i>")

    lines.append(f"<i>Source: live Polymarket odds + Poisson model · not betting advice</i>")
    return "\n".join(lines)


def match_header(rec, emoji="⚽"):
    league = rec.get("league") or "Football"
    return (
        f"{emoji} <b>{html.escape(rec['team1'])} vs. {html.escape(rec['team2'])}</b>\n"
        f"🏆 {html.escape(league)} · ⏰ {kickoff_str(rec['kickoff'])}"
    )


def render_match(rec, compact=False, buttons=True):
    pred = predict.build_prediction(rec)
    text = match_header(rec)
    if pred["best"] or pred["ladder"]:
        text += "\n\n" + prediction_card(rec, pred, compact=compact)
    kb = match_buttons(rec) if buttons else None
    return text, kb


# ---------------------------------------------------------------- flow

def collect_matches():
    events = gamma.fetch_open_soccer_events()
    raw = gamma.build_matches(events, LEAGUE_TAGS)
    now = datetime.now(timezone.utc)
    matches = {}
    for base in raw:
        gamma.finalize_match(raw[base])
        rec = raw[base]
        has_1x2 = rec.get("event_id") and len(rec.get("moneyline") or []) >= 2 and (
            rec.get("draw") is not None or " vs. " in (rec.get("title") or "")
        )
        stale = rec.get("kickoff") and rec["kickoff"] < now - timedelta(hours=1)
        if has_1x2 and not stale:
            matches[base] = rec
    return matches


def upcoming_first():
    now = datetime.now(timezone.utc)
    items = [
        (rec["kickoff"] or now, rec["base_slug"], rec)
        for rec in STATE["matches"].values()
        if not rec.get("resolved") and (rec["kickoff"] is None or rec["kickoff"] >= now - timedelta(hours=3))
    ]
    items.sort(key=lambda x: (x[0] is None, x[0]))
    return [r for _, _, r in items]


def notify_new_match(rec):
    text, kb = render_match(rec)
    broadcast("🆕 <b>NEW MATCH AVAILABLE</b>\n\n" + text, kb)


def notify_reminder(rec, mins):
    text, kb = render_match(rec, compact=True)
    broadcast(f"⏰ <b>KICKOFF IN ~{mins} MINUTES</b>\n\n" + text, kb)


def notify_result(rec, winners):
    title = rec["title"] or f"{rec.get('team1','')} vs. {rec.get('team2','')}"
    text = (
        "✅ <b>MATCH RESOLVED</b>\n\n"
        f"⚽ <b>{html.escape(title)}</b>\n"
        f"🏆 {html.escape(rec.get('league') or 'Football')}\n"
        f"🎯 Outcome: <b>{html.escape(', '.join(winners) or 'see market')}</b>\n"
        f"🔗 {EVENT_URL.format(slug=rec['base_slug'])}"
    )
    broadcast(text)


def iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def serializable_state():
    data = dict(STATE)
    data["matches"] = {
        base: {k: iso(v) for k, v in rec.items()}
        for base, rec in STATE["matches"].items()
    }
    return data


def normalize_state():
    for rec in STATE["matches"].values():
        rec["kickoff"] = gamma.parse_dt(rec.get("kickoff"))


def poll_once():
    matches = collect_matches()

    for base, rec in matches.items():
        saved = STATE["matches"].get(base, {})
        fresh = not saved and STATE["chats"]
        merged = dict(rec)
        merged.update({k: saved[k] for k in ("reminders_sent", "resolved", "resolved_at", "announced", "resolve_failures") if k in saved})
        STATE["matches"][base] = merged
        rec = merged
        if fresh and not rec.get("announced"):
            rec["announced"] = True
            notify_new_match(rec)
            log(f"announced new match: {rec['title']}")

        kickoff = rec.get("kickoff")
        if kickoff and not rec.get("resolved"):
            minutes_left = (kickoff - datetime.now(timezone.utc)).total_seconds() / 60
            if minutes_left > 0:
                sent = rec.setdefault("reminders_sent", [])
                for mins in REMINDER_MINUTES:
                    if mins in sent:
                        continue
                    if minutes_left <= mins:
                        sent.append(mins)
                        if STATE["chats"]:
                            notify_reminder(rec, mins)
                            log(f"reminder {mins}m: {rec['title']}")

    for base, saved in list(STATE["matches"].items()):
        if base in matches or saved.get("resolved"):
            continue
        try:
            final_event = gamma.fetch_event_by_slug(saved["base_slug"])
        except Exception as exc:
            log(f"resolve check failed {base}: {exc}")
            continue
        closed = bool(final_event and final_event.get("closed"))
        winners = []
        if closed:
            for market in final_event.get("markets", []):
                if (market.get("sportsMarketType") or "") != "moneyline":
                    continue
                try:
                    prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
                except ValueError:
                    continue
                if prices and prices[0] >= 0.999:
                    winners.append((market.get("groupItemTitle") or "").strip())
        if closed and winners:
            saved["resolved"] = True
            saved["resolved_at"] = time.time()
            if STATE["chats"]:
                notify_result(saved, winners)
                log(f"result sent: {saved['title']}")
        elif closed:
            saved["resolved"] = True
            saved["resolved_at"] = time.time()
        elif final_event is None:
            saved["resolve_failures"] = saved.get("resolve_failures", 0) + 1
            if saved["resolve_failures"] >= 5:
                saved["resolved"] = True
                saved["resolved_at"] = time.time()

    for base, rec in list(STATE["matches"].items()):
        if rec.get("resolved") and rec.get("resolved_at", 0) < time.time() - 14 * 86400:
            del STATE["matches"][base]

    STATE["time_of_last_poll"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_PATH, serializable_state())


# ---------------------------------------------------------------- digest

def send_digest():
    matches = upcoming_first()[:12]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not matches:
        return
    parts = ["🌅 <b>DAILY DIGEST — football on Polymarket</b>", ""]
    for rec in matches:
        pred = predict.build_prediction(rec)
        best = ""
        if pred["best"]:
            team, p = pred["best"]
            best = f" · <b>{html.escape(team)}</b> {predict.pct(p)}"
        if pred["lam"] is not None:
            best += f" · xG≈{pred['lam']:.1f}"
        parts.append(
            f"⚽ <b>{html.escape(rec['team1'])} vs. {html.escape(rec['team2'])}</b>\n"
            f"   🏆 {html.escape(rec.get('league') or '')} · ⏰ {kickoff_str(rec['kickoff'])}{best}"
        )
    parts.append("")
    parts.append("Use /matches for links 🔗")
    broadcast("\n".join(parts), [[{"text": "📅 Open Polymarket Soccer", "url": "https://polymarket.com/sports/soccer"}]])
    STATE["digest"] = today
    log("daily digest sent")


def digest_check():
    if DIGEST_HOUR_UTC is None:
        return
    now = datetime.now(timezone.utc)
    if now.hour != int(DIGEST_HOUR_UTC):
        return
    if STATE.get("digest") >= now.strftime("%Y-%m-%d"):
        return
    send_digest()


# ---------------------------------------------------------------- commands

HELP = (
    "🤖 <b>Polymarket Football Bot</b>\n\n"
    "I notify you about every football match market on Polymarket with predictions:\n"
    "🆕 new match listed\n⏰ kickoff reminders (30 &amp; 5 min)\n"
    "✅ results when markets resolve\n🌅 daily digest\n\n"
    "<b>Commands</b>\n"
    "/start — register this chat\n"
    "/stop — unregister\n"
    "/matches — upcoming matches\n"
    "/predict &lt;team&gt; — predictions for a match\n"
    "/leagues — leagues I track\n"
    "/help — this message\n\n"
    "⚠️ Probabilities are market-implied + Poisson model. Not financial advice."
)


def handle_command(chat_id, text):
    cmd = (text.split()[0].split("@")[0].lower() if text else "/help")
    args = text.strip().split(None, 1)
    arg = args[1] if len(args) > 1 else ""

    if cmd == "/start":
        if chat_id not in STATE["chats"]:
            STATE["chats"].append(chat_id)
        send(chat_id, HELP)
        send(chat_id, matches_text())
        log(f"chat registered: {chat_id}")
    elif cmd == "/stop":
        if chat_id in STATE["chats"]:
            STATE["chats"].remove(chat_id)
            send(chat_id, "👋 Unregistered. Goodbye!")
        else:
            send(chat_id, "You are not registered yet.")
    elif cmd == "/help":
        send(chat_id, HELP)
    elif cmd == "/matches":
        send(chat_id, matches_text())
    elif cmd == "/leagues":
        leagues = sorted({(rec.get("league") or "?") for rec in STATE["matches"].values() if not rec.get("resolved")})
        if leagues:
            send(chat_id, "🏆 <b>Tracked leagues</b>\n" + "\n".join(f"  {l}" for l in leagues))
        else:
            send(chat_id, "No leagues tracked yet.")
    elif cmd == "/predict":
        if not arg:
            send(chat_id, "ℹ️ Usage: /predict <team name>\ne.g. /predict galatasaray")
        else:
            cands = [
                rec for rec in upcoming_first()
                if arg.lower() in rec["title"].lower()
                or arg.lower() in (rec.get("team1", "") + rec.get("team2", "")).lower()
            ]
            if not cands:
                send(chat_id, f"🤷 No upcoming match found for '<b>{html.escape(arg)}</b>'. Try /matches first.")
            else:
                for rec in cands[:3]:
                    txt, kb = render_match(rec)
                    send(chat_id, txt, kb)
    else:
        send(chat_id, HELP)


def matches_text(limit=20):
    now = datetime.now(timezone.utc)
    recs = upcoming_first()[:limit]
    if not recs:
        return "🤷 No upcoming matches tracked yet."
    lines = ["⚽ <b>UPCOMING MATCHES</b>", ""]
    for rec in recs:
        pred = predict.build_prediction(rec)
        best = ""
        if pred["best"]:
            t, p = pred["best"]
            best = f" · {html.escape(t)} {predict.pct(p)}"
        ko = rec['kickoff'].strftime("%d %b %H:%M") if rec.get("kickoff") else "TBA"
        if rec.get("kickoff") and rec["kickoff"] < now:
            ko = "LIVE/ENDED"
        lines.append(f"<b>{html.escape(rec['team1'])} vs. {html.escape(rec['team2'])}</b>")
        lines.append(f"  🏆 {html.escape(rec.get('league') or '')} · ⏰ {ko} UTC{best}")
    lines.append("")
    lines.append("🔗 Use /predict or click links in match cards")
    return "\n".join(lines)


# ---------------------------------------------------------------- listener

def listener_loop():
    log("listener started")
    while True:
        try:
            updates = tg("getUpdates", {"offset": STATE.get("offset", 0), "timeout": 30}, timeout=60)
        except Exception as exc:
            log(f"getUpdates failed: {exc}")
            time.sleep(5)
            continue
        for update in updates:
            STATE["offset"] = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat_id is None:
                continue
            try:
                handle_command(chat_id, text)
            except Exception as exc:
                log(f"handler error: {exc}")
            save_json(STATE_PATH, STATE)


# ---------------------------------------------------------------- main

def main():
    if not TOKEN:
        print(
            "Bot token missing.\n"
            "1. Open @BotFather in Telegram\n2. /newbot\n"
            "3. Paste the token into config.json or set TELEGRAM_BOT_TOKEN."
        )
        sys.exit(1)
    try:
        me = tg("getMe", timeout=15)
        log(f"connected as @{me['username']} 🤖")
    except Exception as exc:
        log(f"token rejected: {exc}")
        sys.exit(1)

    if not STATE["matches"]:
        log("first run: backfilling existing matches (no announcements)")
        STATE["matches"] = collect_matches()
        for rec in STATE["matches"].values():
            rec["announced"] = True
        save_json(STATE_PATH, STATE)
        log(f"backfilled {len(STATE['matches'])} matches")
    normalize_state()
    threading.Thread(target=listener_loop, daemon=True).start()
    log(f"polling every {POLL_INTERVAL}s — watching {len(STATE['matches'])} matches")

    while True:
        try:
            poll_once()
            digest_check()
        except KeyboardInterrupt:
            log("stopped by user")
            break
        except Exception as exc:
            log(f"poll error: {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
