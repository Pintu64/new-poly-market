"""Polymarket Football Bot — advanced Telegram notifier with predictions.

Notifications:
  🆕 new match listed            ⏰ kickoff reminders
  ✅ results                     🌅 daily digest
  📈 big odds moves (threshold + cooldown configurable)

Inline buttons on every card: 🔄 live reprice refresh, 📲 full over-ladder,
🔗 trade links.

Commands:
  /start /stop /help
  /matches              upcoming matches
  /today                matches scheduled today
  /live                 matches probably playing right now
  /top                  hottest matches by 24h volume
  /predict <team>       predictions for one match
  /follow <team>        personalised feed for this chat
  /unfollow <team>      remove a follow
  /follows              show follows for this chat
  /leagues              leagues tracked
  /go                   send the digest now
  /stats                bot statistics

Setup: paste the bot token into config.json, then:  python src/bot.py
Stdlib only.
"""

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer


def keepalive_server():
    """Serve '/'; Render free web services need a bound port + pings."""
    if not os.environ.get("RENDER"):
        return
    port = int(os.environ.get("PORT") or 10000)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = TCPServer(("0.0.0.0", port), Handler)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"keepalive server on :{port}")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import gamma  # noqa: E402
import predict  # noqa: E402
import report  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

STARTED_AT = time.time()


def persistent_state():
    """STATE copy safe for JSON (datetimes serialised, no live-only keys)."""
    data = dict(STATE)
    data["matches"] = {
        base: {k: iso(v) for k, v in rec.items() if k != "last_prices"}
        for base, rec in STATE["matches"].items()
    }
    return data


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"warn: cannot read {path}: {exc}")
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


CONFIG = load_json(CONFIG_PATH, {})
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG.get("bot_token") or ""
POLL_INTERVAL = int(CONFIG.get("poll_interval_seconds", 90))
REMINDER_MINUTES = CONFIG.get("reminder_minutes", [30, 5])
LEAGUE_TAGS = CONFIG.get("league_tags", [])
DIGEST_HOUR_UTC = CONFIG.get("digest_hour_utc")
MOVE_ALERT_PCT = float(CONFIG.get("odds_move_alert_pct", 15))
MOVE_COOLDOWN_MIN = int(CONFIG.get("odds_move_cooldown_minutes", 30))

STATE = load_json(STATE_PATH, {"chats": [], "follows": {}, "matches": {},
                               "offset": 0, "digest": ""})

BOT_USERNAME = ""


# ------------------------------------------------------------- telegram api

def tg(method, payload=None, timeout=35):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(body)
    return body["result"]


def chat_wants(chat_id, teams):
    """Respect the chat's follows: pass a team hint list (may be empty)."""
    follows = STATE["follows"].get(str(chat_id)) or []
    if not follows:
        return True
    haystack = " ".join(teams or []).lower()
    return any(f.lower() in haystack for f in follows)


def broadcast(text, buttons=None, teams=None):
    teams = teams or []
    for chat_id in list(STATE["chats"]):
        if chat_wants(chat_id, teams):
            send(chat_id, text, buttons)


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


def edit_and_reshow(chat_id, message_id, text, buttons):
    try:
        tg("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons or []},
        })
    except Exception as exc:
        msg = str(exc)
        if "message is not modified" not in msg:
            log(f"edit failed: {exc}")


# ------------------------------------------------------------- match flow

def iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def collect_matches():
    now = datetime.now(timezone.utc)
    raw = gamma.build_matches(gamma.fetch_open_soccer_events(), LEAGUE_TAGS)
    matches = {}
    for base in raw:
        gamma.finalize_match(raw[base])
        rec = raw[base]
        stale = rec.get("kickoff") and rec["kickoff"] < now - timedelta(hours=1)
        if gamma.is_valid_match(rec) and not stale:
            matches[base] = rec
    return matches


def fresh_match(base):
    """Re-fetch a single match family for live button refresh."""
    try:
        rec = gamma.new_record(base)
        for event in gamma.fetch_events_for_base(base):
            rec = gamma.build_one(event, rec)
        gamma.finalize_match(rec)
        if gamma.is_valid_match(rec):
            return rec
    except Exception as exc:
        log(f"fresh_match {base}: {exc}")
    return STATE["matches"].get(base)


def deserialize_state():
    for rec in STATE["matches"].values():
        rec["kickoff"] = gamma.parse_dt(rec.get("kickoff"))
        rec["created_at"] = rec.get("created_at")


def upcoming(recs=None, min_age_hours=3):
    now = datetime.now(timezone.utc)
    out = []
    for rec in (recs if recs is not None else STATE["matches"].values()):
        if rec.get("resolved"):
            continue
        ko = rec.get("kickoff")
        if ko and ko < now - timedelta(hours=min_age_hours):
            continue
        out.append((datetime.now(timezone.utc) - timedelta(days=3650) if ko is None else ko,
                    rec["base_slug"], rec))
    out.sort(key=lambda x: x[0])
    return [r for _, _, r in out]


def notify_new_match(rec):
    pred = predict.build_prediction(rec)
    text = "🆕 <b>NEW MATCH ON POLYMARKET</b>\n\n" + report.card(rec, pred)
    broadcast(text, report.trade_buttons(rec), teams=[rec["team1"], rec["team2"]])


def notify_reminder(rec, mins):
    pred = predict.build_prediction(rec)
    text = f"⏰ <b>KICKOFF IN ~{mins} MIN</b>\n\n" + report.card(rec, pred)
    broadcast(text, report.trade_buttons(rec), teams=[rec["team1"], rec["team2"]])


def notify_result(rec, winners):
    text = report.results_msg(rec, winners)
    broadcast(text, teams=[rec.get("team1") or "", rec.get("team2") or ""])


def notify_moves(rec, deltas):
    lines = [f"📈 <b>ODDS MOVING — {report.esc(rec['team1'])} vs. {report.esc(rec['team2'])}</b>", ""]
    for label, old, new in deltas[:8]:
        arrow = "🔼" if new > old else "🔽"
        lines.append(f"  {arrow} {report.esc(label)}: {old*100:.0f}% → <b>{new*100:.0f}%</b>")
    pred = predict.build_prediction(rec)
    lines.append("")
    lines.append("⚡ <b>Prediction now:</b> " + (
        f"{report.esc(pred['best'][0])} {report.pct(pred['best'][1])}" if pred["best"] else "—"))
    text = "\n".join(lines)
    broadcast(text, report.trade_buttons(rec), teams=[rec["team1"], rec["team2"]])


def snapshot_deltas(rec, saved):
    """Compare price snapshots; return list of (label, old, new) moves."""
    old = saved.get("prices") or {}
    new = rec.get("prices") or {}
    deltas = []
    for label, price in new.items():
        prev = old.get(label)
        if prev is None or not isinstance(prev, (int, float)):
            continue
        if abs(price - prev) >= MOVE_ALERT_PCT / 100:
            deltas.append((label, prev, price))
    return deltas


def poll_once():
    matches = collect_matches()

    for base, rec in matches.items():
        saved = STATE["matches"].get(base, {})
        merged = dict(rec)
        for k in ("reminders_sent", "resolved", "resolved_at", "announced",
                  "resolve_failures", "move_alert_at"):
            if k in saved:
                merged[k] = saved[k]
        if merged.get("prices"):
            saved_prices = saved.get("prices") or {}
            if saved_prices and not merged.get("move_alert_at"):
                merged["move_alert_at"] = 0
            merged["prices"] = rec["prices"]
            merged["last_prices"] = saved_prices
        else:
            merged["prices"] = saved.get("prices") or {}
        STATE["matches"][base] = merged
        rec = merged

        is_new = not saved and STATE["chats"]
        if is_new and not rec.get("announced"):
            rec["announced"] = True
            notify_new_match(rec)
            log(f"announced new match: {rec['title']}")

        ko = rec.get("kickoff")
        now = datetime.now(timezone.utc)
        if ko and not rec.get("resolved"):
            if now - timedelta(hours=1) <= ko:
                minutes_left = (ko - now).total_seconds() / 60
                sent = rec.setdefault("reminders_sent", [])
                for mins in REMINDER_MINUTES:
                    if mins in sent:
                        continue
                    if minutes_left <= mins:
                        sent.append(mins)
                        notify_reminder(rec, mins)
                        log(f"reminder {mins}m: {rec['title']}")

        deltas = snapshot_deltas(rec, saved)
        if deltas:
            at = rec.get("move_alert_at") or 0
            if time.time() - at >= MOVE_COOLDOWN_MIN * 60:
                rec["move_alert_at"] = time.time()
                notify_moves(rec, deltas)
                log(f"odds move: {rec['title']} ({len(deltas)} markets)")

    # resolution sweep for matches no longer open
    for base, saved in list(STATE["matches"].items()):
        if base in matches or saved.get("resolved"):
            continue
        try:
            final_event = gamma.fetch_event_by_slug(saved["base_slug"])
        except Exception as exc:
            log(f"resolve check failed {base}: {exc}")
            continue
        if not final_event:
            saved["resolve_failures"] = saved.get("resolve_failures", 0) + 1
            if saved["resolve_failures"] >= 5:
                saved["resolved"], saved["resolved_at"] = True, time.time()
            continue
        if final_event.get("closed"):
            winners = []
            for market in final_event.get("markets", []):
                if (market.get("sportsMarketType") or "") != "moneyline":
                    continue
                try:
                    prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
                except ValueError:
                    continue
                if prices and prices[0] >= 0.999:
                    winners.append((market.get("groupItemTitle") or "").strip())
            saved["resolved"], saved["resolved_at"] = True, time.time()
            if winners and STATE["chats"]:
                notify_result(saved, winners)
                log(f"result sent: {saved['title']}")

    # prune resolved > 14d then serialise save
    cutoff = time.time() - 14 * 86400
    for base, rec in list(STATE["matches"].items()):
        if rec.get("resolved") and rec.get("resolved_at", 0) < cutoff:
            del STATE["matches"][base]

    STATE["last_poll"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_PATH, persistent_state())


# ------------------------------------------------------------- digest

def send_digest():
    recs = upcoming()[:12]
    if not recs:
        return
    STATE["digest"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    broadcast(report.digest_msg(recs), teams=None)
    log("daily digest sent")


def digest_check():
    if DIGEST_HOUR_UTC is None:
        return
    now = datetime.now(timezone.utc)
    if now.hour == int(DIGEST_HOUR_UTC) and STATE.get("digest") != now.strftime("%Y-%m-%d"):
        send_digest()


# ------------------------------------------------------------- commands

HELP = (
    "🤖 <b>Polymarket Football Bot — ADVANCED</b>\n\n"
    "⚠️ I send: 🆕 new match markets  · ⏰ kickoff reminders  · "
    "✅ match results  · 🌅 daily digest  · 📈 odds-move alerts\n"
    "Every card has 🔄 live refresh and 📲 full ladder buttons.\n\n"
    "<b>Predictions on every card:</b>\n"
    "⚡ winner probability (1X2)\n"
    "🎯 over lines 0.5 → 5.5 («you may never see it listed»)\n"
    "🥅 both-teams-to-score  ·  🎯 top exact scores  ·  🧊 xG\n\n"
    "<b>Commands</b>\n"
    "⚽ /matches — upcoming\n"
    "📅 /today — today's schedule\n"
    "🔴 /live — probably playing now\n"
    "🔥 /top — biggest 24h volume\n"
    "🔍 /predict &lt;team&gt;\n"
    "➕ /follow &lt;team&gt; — personalised feed\n"
    "➖ /unfollow &lt;team&gt;  ·  👤 /follows\n"
    "🌍 /leagues\n"
    "🌅 /go — digest now  ·  📊 /stats\n"
    "ℹ️ /help\n\n"
    "<i>Not financial advice. Bet responsibly.</i>"
)


def matches_text(limit=20, title="⚽ <b>UPCOMING MATCHES</b>", extra_filter=None, sort_by_vol=False):
    recs = [r for r in upcoming() if extra_filter is None or extra_filter(r)][:limit]
    if sort_by_vol:
        recs = sorted(recs, key=lambda r: -(r.get("vol24") or 0))
    if not recs:
        return "🤷 Nothing found right now."
    lines = [title, ""]
    for rec in recs:
        pred = predict.build_prediction(rec)
        extra = ""
        if pred["best"]:
            t, p = pred["best"]
            extra = f" · <b>{report.esc(t)}</b> {report.pct(p)}"
        if pred.get("lam") is not None:
            extra += f" · xG {pred['lam']:.1f}"
        ko = rec["kickoff"].strftime("%d %b %H:%M") if rec.get("kickoff") else "TBA"
        league = report.esc(rec.get("league") or "")
        lines.append(f"<b>{report.esc(rec['team1'])} vs. {report.esc(rec['team2'])}</b>")
        lines.append(f"  🏆 {league} · ⏰ {ko} UTC{extra}{report.vol_str(rec.get('vol24'))}")
    lines.append("")
    lines.append("🔍 /predict <code>&lt;team&gt;</code> for the full card 🔗")
    return "\n".join(lines)[:4000]


def handle_command(chat_id, text):
    parts = (text or "").strip().split(None, 1)
    cmd = (parts[0].split("@")[0].lower() if parts else "/help")
    arg = (parts[1] if len(parts) > 1 else "").strip()

    if cmd == "/start":
        if chat_id not in STATE["chats"]:
            STATE["chats"].append(chat_id)
        send(chat_id, HELP)
        send(chat_id, matches_text())
        log(f"chat registered: {chat_id}")
    elif cmd == "/stop":
        if chat_id in STATE["chats"]:
            STATE["chats"].remove(chat_id)
            STATE["follows"].pop(str(chat_id), None)
            send(chat_id, "👋 Unregistered.")
        else:
            send(chat_id, "Not registered yet.")
    elif cmd == "/help":
        send(chat_id, HELP)
    elif cmd == "/matches":
        send(chat_id, matches_text())
    elif cmd == "/today":
        now = datetime.now(timezone.utc)
        send(chat_id, matches_text(
            title="📅 <b>TODAY'S MATCHES (UTC)</b>",
            extra_filter=lambda r: r["kickoff"] and r["kickoff"].date() == now.date()))
    elif cmd == "/live":
        now = datetime.now(timezone.utc)
        send(chat_id, matches_text(
            title="🔴 <b>LIVE / IN-PLAY LIKELY</b>",
            extra_filter=lambda r: r["kickoff"] and now - timedelta(hours=3) < r["kickoff"] < now + timedelta(hours=2)))
    elif cmd == "/top":
        send(chat_id, matches_text(
            title="🔥 <b>HOTTEST BY 24H VOLUME</b>",
            extra_filter=lambda r: True, sort_by_vol=True))
    elif cmd == "/leagues":
        leagues = sorted({(r.get("league") or "?") for r in upcoming()})
        send(chat_id, "🌍 <b>Tracked leagues</b>\n" + "\n".join("• " + report.esc(l) for l in leagues) if leagues else "🤷 None yet.")
    elif cmd == "/follow":
        if not arg:
            follows = STATE["follows"].get(str(chat_id)) or []
            send(chat_id, "👤 <b>Your follows</b>: " + (", ".join(follows) or "none") +
                          "\nAdd with /follow &lt;team or league&gt;")
        else:
            fl = STATE["follows"].setdefault(str(chat_id), [])
            if arg.lower() not in [f.lower() for f in fl]:
                fl.append(arg)
                send(chat_id, f"➕ Following <b>{report.esc(arg)}</b>.\n"
                              f"Only matches matching your follows ping this chat.\n"
                              f"Tip: /follows shows the list.")
            else:
                send(chat_id, f"Already following {report.esc(arg)}.")
    elif cmd == "/unfollow":
        fl = STATE["follows"].get(str(chat_id)) or []
        keep = [f for f in fl if f.lower() != arg.lower()]
        STATE["follows"][str(chat_id)] = keep
        send(chat_id, "➖ Removed." if len(keep) < len(fl) else f"Was not following {report.esc(arg)}.")
    elif cmd == "/go":
        STATE["digest"] = ""
        send(chat_id, "🚀 Sending digest for this UTC day…")
        send_digest()
    elif cmd == "/stats":
        matches = STATE["matches"]
        resolved = sum(1 for r in matches.values() if r.get("resolved"))
        up = time.time() - STARTED_AT
        send(chat_id,
             "📊 <b>Bot stats</b>\n"
             f"⚽ Matches tracked (all-time since reset): <b>{len(matches)}</b>\n"
             f"✅ Resolved recorded: <b>{resolved}</b>\n"
             f"👥 Chats: <b>{len(STATE['chats'])}</b>\n"
             f"⏱ Uptime: <b>{int(up//3600)}h {int(up%3600//60)}m</b>\n"
             f"🔁 Poll interval: {POLL_INTERVAL}s")
    else:
        if cmd == "/predict":
            if not arg:
                send(chat_id, "ℹ️ Usage: /predict <code>&lt;team&gt;</code>")
            else:
                cands = [r for r in upcoming()
                         if arg.lower() in r["title"].lower()
                         or arg.lower() in (r.get("team1", "") + " " + r.get("team2", "")).lower()]
                if not cands:
                    send(chat_id, f"🤷 No upcoming match for '<b>{report.esc(arg)}</b>'. Try /matches.")
                else:
                    for rec in cands[:3]:
                        fresh = fresh_match(rec["base_slug"])
                        pred = predict.build_prediction(fresh or rec)
                        send(chat_id, report.card(fresh or rec, pred),
                             report.trade_buttons(fresh or rec))
        else:
            send(chat_id, HELP)


# ------------------------------------------------------------- callbacks

def handle_callback(cb):
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
    except Exception:
        pass
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    view, _, _base = (cb.get("data") or "").partition(":")
    base = _base
    if chat_id is None or not base:
        return
    rec = fresh_match(base)
    if not rec:
        edit_and_reshow(chat_id, message_id, "🤷 Match vanished from Polymarket.", [])
        return
    pred = predict.build_prediction(rec)
    if view == "lad":
        text = report.card(rec, pred, ladder_view=True)
        buttons = [[{"text": "↩️ Compact view", "callback_data": f"ref:{base}"},
                    {"text": "🔄 Refresh", "callback_data": f"ref:{base}"}]]
    else:
        text = report.card(rec, pred)
        buttons = report.trade_buttons(rec)
    edit_and_reshow(chat_id, message_id, text, buttons)
    log(f"callback {view} on {base} by {chat_id}")


def listener_loop():
    log("listener started")
    while True:
        try:
            updates = tg("getUpdates",
                         {"offset": STATE.get("offset", 0), "timeout": 30}, timeout=60)
        except Exception as exc:
            log(f"getUpdates failed: {exc}")
            time.sleep(5)
            continue
        for update in updates:
            STATE["offset"] = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                    save_json(STATE_PATH, persistent_state())
                    continue
                msg = update.get("message")
                if not msg:
                    continue
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text") or ""
                if chat_id is None:
                    continue
                log(f"command from {chat_id}: {text}")
                handle_command(chat_id, text)
            except Exception as exc:
                log(f"update handler error: {exc}")
            save_json(STATE_PATH, persistent_state())


# ------------------------------------------------------------- main

def main():
    global TOKEN, BOT_USERNAME
    if not TOKEN:
        print(
            "Bot token missing.\n"
            "1. Open @BotFather in Telegram\n2. /newbot\n"
            "3. Paste the token into config.json or set TELEGRAM_BOT_TOKEN."
        )
        sys.exit(1)
    try:
        me = tg("getMe", timeout=15)
        BOT_USERNAME = me.get("username", "")
        log(f"connected as @{BOT_USERNAME} 🤖")
    except Exception as exc:
        log(f"token rejected: {exc}")
        sys.exit(1)

    if not STATE["matches"]:
        log("first run: backfilling existing matches (no announcements)")
        STATE["matches"] = collect_matches()
        for rec in STATE["matches"].values():
            rec["announced"] = True
        save_json(STATE_PATH, persistent_state())
        log(f"backfilled {len(STATE['matches'])} matches")
    deserialize_state()
    keepalive_server()

    threading.Thread(target=listener_loop, daemon=True).start()
    log(f"polling every {POLL_INTERVAL}s — {len(STATE['matches'])} matches in memory")

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
