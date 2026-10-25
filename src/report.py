"""Message rendering: professional emoji-rich Telegram cards."""

import html

EVENT_URL = "https://polymarket.com/event/{slug}"

MAX_TG_TEXT = 4000


def esc(s):
    return html.escape(str(s or ""), quote=False)


def pct(x):
    return "·" if x is None else f"{round(x * 100)}%"


def bar(p, width=16):
    if p is None:
        return "·" * width
    if p <= 0.01:
        return "·" * width
    blocks = max(1, min(width, round(p * width)))
    return "▓" * blocks + "·" * (width - blocks)


def fmt_ko(ko):
    return ko.strftime("%a %d %b · %H:%M UTC") if ko else "⏳ TBA"


def vol_str(v):
    if not v:
        return ""
    if v >= 1000:
        return f" · 💰 ${v/1000:.1f}K"
    return f" · 💰 ${v:.0f}"


def trade_buttons(rec):
    rows = []
    row = [{"text": f"🔗 Trade", "url": EVENT_URL.format(slug=rec["base_slug"])}]
    if rec.get("extra_slug"):
        row.append({"text": "📊 Markets", "url": EVENT_URL.format(slug=rec["extra_slug"])})
    rows.append(row)
    rows.append([
        {"text": "🔄 Refresh", "callback_data": f"ref:{rec['base_slug']}"},
        {"text": "📲 Full ladder", "callback_data": f"lad:{rec['base_slug']}"},
    ])
    return rows


def toggle_button(rec):
    return [[{"text": "↩️ Compact view", "callback_data": f"ref:{rec['base_slug']}"}]]


def header(rec, emoji="⚽"):
    league = rec.get("league") or "Football"
    return (
        f"{emoji} <b>{esc(rec['team1'])} vs. {esc(rec['team2'])}</b>\n"
        f"🏆 {esc(league)} · ⏰ {fmt_ko(rec['kickoff'])}{vol_str(rec.get('vol24'))}"
    )


def _value_marker(market, model):
    if market is None or model is None:
        return ""
    d = model - market
    if market <= 0.02 or market >= 0.98:
        return " 💤"
    if d >= 0.15:
        return " 🔼"
    if d <= -0.15:
        return " 🔽"
    if abs(d) < 0.05:
        return " ✅"
    return ""


def card(rec, pred, compact=False, ladder_view=False):
    lines = [header(rec)]

    if pred["best"]:
        team, p = pred["best"]
        emoji = "🤝" if team == "Draw" else "⚡"
        lines.append("")
        lines.append(f"{emoji} <b>PREDICTION:</b> {esc(team)} — <b>{pct(p)}</b>")

    if pred["probs_1x2"]:
        rows = " · ".join(f"{esc(t)} <b>{pct(p)}</b>" for t, p in pred["probs_1x2"])
        lines.append(f"🧮 1X2 — {rows}")

    ladder = pred.get("ladder") or []
    shown = ladder if (ladder_view and not compact) else [r for r in ladder if r.get("market") is not None][:4]
    if shown:
        lines.append("")
        lines.append("🎯 <b>Goals ladder</b> <i>(market | model)</i>")
    for row in shown:
        line_s = f"{row['line']:g}"
        mark = _value_marker(row.get("market"), row.get("model"))
        bar_p = row.get("market") if row.get("market") is not None else row.get("model")
        lines.append(
            f"  O{line_s} {bar(bar_p)} {pct(row.get('market'))} | "
            f"{pct(row.get('model'))}{mark}"
        )

    btts = pred.get("btts_market") if pred.get("btts_market") is not None else pred.get("btts_model")
    if btts is not None:
        lines.append(f"🥅 <b>BTTS:</b> {pct(btts)}")

    ts = pred.get("top_scores") or []
    if ts:
        best_scores = [
            f"{i}-{j} {pct(p)}" for (i, j), p in ts[:3]
        ]
        lines.append(f"🎯 <b>Exact scores:</b> {esc(' · '.join(best_scores))}")

    if pred.get("exp_score"):
        lh, la = pred["exp_score"]
        lines.append(f"🧊 <i>xG ≈ {lh:g} — {la:g}</i>")

    lines.append("")
    lines.append("<i>market-implied + Poisson model · not financial advice</i>")
    text = "\n".join(lines)
    return text[:MAX_TG_TEXT]


def results_msg(rec, winners):
    title = rec["title"] or f"{rec.get('team1','')} vs. {rec.get('team2','')}"
    return (
        "✅ <b>MATCH RESOLVED</b>\n\n"
        f"⚽ <b>{esc(title)}</b>\n"
        f"🏆 {esc(rec.get('league') or 'Football')}\n"
        f"🎯 Outcome: <b>{esc(', '.join(winners) or 'none')}</b>\n"
        f"🔗 {EVENT_URL.format(slug=rec['base_slug'])}"
    )


def reminder_msg(rec, mins):
    text = f"⏰ <b>KICKOFF IN ~{mins} MINUTES</b>\n\n" + card(rec, _pred_local(rec))
    return text, trade_buttons(rec)


def _pred_local(rec):
    import predict
    return predict.build_prediction(rec)


def digest_msg(recs):
    parts = ["🌅 <b>DAILY DIGEST — Football on Polymarket</b>", ""]
    for rec in recs:
        pred = _pred_local(rec)
        extra = ""
        if pred["best"]:
            t, p = pred["best"]
            extra = f" · <b>{esc(t)}</b> {pct(p)}"
        if pred.get("lam") is not None:
            extra += f" · xG {pred['lam']:.1f}"
        league = esc(rec.get("league") or "")
        parts.append(
            f"⚽ <b>{esc(rec['team1'])} vs. {esc(rec['team2'])}</b>\n"
            f"    🏆 {league} · ⏰ {fmt_ko(rec['kickoff'])}{extra}"
        )
    parts.append("")
    parts.append("Use /matches for direct links 🔗")
    return "\n".join(parts)[:MAX_TG_TEXT]
