"""Prediction engine: turns live Polymarket prices into a match report.

Approach
--------
- 1X2: moneyline Yes prices are already probabilities; normalise them.
- Over/Under goal lines: live market prices give P(total > line).  Fit an
  expected total "lam" (Poisson) so the model reproduces the traded prices,
  then output model probabilities for the full ladder: 0.5, 1, 1.5, 2, 2.5,
  3, 3.5 ... including lines the exchange never lists.
- BTTS: market price directly, cross-checked against the fitted Poisson
  model with goals split between teams proportionally to 1X2 strength.
- Expected score shown from the fitted lambdas and team strength split.
"""

import math


def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def cdf(k_max, lam):
    return sum(poisson_pmf(i, lam) for i in range(k_max + 1))


def over_prob(lam, line):
    """P(total goals > line) under Poisson(lam).  Line may be .5 or integer."""
    if line == 0:
        return 1.0
    top = int(math.floor(line))
    return max(0.0, min(1.0, 1.0 - cdf(top, lam)))


def fit_lambda(lines_prices):
    """Least-squares Poisson fit of expected total goals over line prices."""
    pts = [(line, p) for line, p in lines_prices.items() if 0.02 <= p <= 0.98]
    if len(pts) < 2:
        return None

    def err(lam):
        return sum((over_prob(lam, line) - p) ** 2 for line, p in pts)

    # grid scan then golden refine (robust, no monotonicity assumptions)
    best_lam, best_err = 1.0, float("inf")
    grid = [x / 100 for x in range(5, 801)]
    for lam in grid:
        e = err(lam)
        if e < best_err:
            best_err, best_lam = e, lam
    lo2, hi2 = max(0.01, best_lam - 0.05), best_lam + 0.05
    for _ in range(60):
        m1 = lo2 + (hi2 - lo2) / 3
        m2 = hi2 - (hi2 - lo2) / 3
        if err(m1) < err(m2):
            hi2 = m2
        else:
            lo2 = m1
    return round((lo2 + hi2) / 2, 4)


def normalise_1x2(ml, draw_price):
    total = sum(p for _, p in ml) + (draw_price or 0.0)
    if total <= 0:
        return None
    probs = [(team, p / total) for team, p in ml]
    if draw_price is not None:
        probs.append(("Draw", draw_price / total))
    return probs


def strength_split(ml, lam):
    """Split lam between teams ~ proportional to their implied share."""
    total = sum(p for _, p in ml) or 1.0
    if len(ml) >= 2:
        s1 = ml[0][1] / total
    else:
        s1 = 0.5
    s1 = min(0.75, max(0.25, s1))
    return lam * s1, lam * (1 - s1)


def btts_model(lh, la):
    return (1 - math.exp(-lh)) * (1 - math.exp(-la))


def most_likely_result(probs_1x2):
    if not probs_1x2:
        return None
    return max(probs_1x2, key=lambda kv: kv[1])


def build_prediction(rec):
    """Prediction dict from a MatchRecord."""
    ml = rec.get("moneyline") or []
    draw = rec.get("draw")
    totals = rec.get("totals") or {}
    lam = fit_lambda(totals)
    probs_1x2 = normalise_1x2(ml, draw)
    lh, la = (None, None)
    btts_m = None
    if lam is not None and probs_1x2 and len(ml) >= 2:
        lh, la = strength_split(ml, lam)
        btts_m = btts_model(lh, la)

    ladder = []
    for line in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        row = {"line": line}
        if line in totals:
            row["market"] = totals[line]
        if lam is not None:
            row["model"] = over_prob(lam, line)
        if row.get("market") is not None or row.get("model") is not None:
            ladder.append(row)

    if lam is not None and probs_1x2 and len(ml) >= 2:
        s1 = ml[0][1] / sum(p for _, p in ml)
        s1 = min(0.75, max(0.25, s1))
        exp_score = (round(lh, 1), round(la, 1))
    else:
        exp_score = None

    return {
        "probs_1x2": probs_1x2,
        "lam": lam,
        "ladder": ladder,
        "btts_market": rec.get("btts"),
        "btts_model": btts_m,
        "exp_score": exp_score,
        "best": most_likely_result(probs_1x2),
    }


def pct(x):
    return "--" if x is None else f"{round(x * 100)}%"


def bar(p, width=18):
    if p is None:
        return "·" * width
    if p <= 0.01:
        return "·" * width
    blocks = max(1, min(width, round(p * width)))
    return "▓" * blocks + "·" * (width - blocks)
