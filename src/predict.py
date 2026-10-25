"""Prediction engine — joint Poisson/Skellam model from live Polymarket prices.

Stage 1: fit expected total goals `lam` against the traded O/U goal lines.
Stage 2: split lam between teams with share `s`, searching the s that best
         reproduces the traded 1X2 moneyline through the Skellam goal model.
Stage 3: build a correct-score matrix from (lam_home, lam_away) and derive
         every market from it: the full over ladder (0.5…5.5), integer
         lines, BTTS, raw expected score, and top exact scores.
"""

import math

K = 8  # max goals per side considered in the score matrix
_pmf_cache = {}


def poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def pmf_vec(lam, k_max=K):
    key = round(lam, 4)
    vec = _pmf_cache.get(key)
    if vec is None:
        vec = [poisson_pmf(i, key) for i in range(k_max + 1)]
        _pmf_cache[key] = vec
    return vec


def over_prob(lam, line):
    if line == 0:
        return 1.0
    top = int(math.floor(line))
    return max(0.0, min(1.0, 1.0 - sum(pmf_vec(lam)[: top + 1])))


def fit_lambda(totals):
    """Least-squares Poisson fit of expected total goals from O/U prices."""
    pts = [(line, p) for line, p in totals.items() if 0.02 <= p <= 0.98]
    if len(pts) < 2:
        return None

    def err(lam):
        return sum((over_prob(lam, line) - p) ** 2 for line, p in pts)

    best_lam, best_err = 1.0, float("inf")
    for lam in [x / 100 for x in range(5, 801)]:
        e = err(lam)
        if e < best_err:
            best_err, best_lam = e, lam
    lo, hi = max(0.01, best_lam - 0.05), best_lam + 0.05
    for _ in range(60):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if err(m1) < err(m2):
            hi = m2
        else:
            lo = m1
    return round((lo + hi) / 2, 4)


def score_matrix(lh, la, k_max=K):
    ph, pa = pmf_vec(lh, k_max), pmf_vec(la, k_max)
    return [[ph[i] * pa[j] for j in range(k_max + 1)] for i in range(k_max + 1)]


def matrix_1x2(M):
    home = draw = away = 0.0
    for i, row in enumerate(M):
        for j, p in enumerate(row):
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
    total = home + draw + away
    return home / total, draw / total, away / total


def matrix_over(M, line):
    """P(total > line) from the score matrix (works for .5 and integer lines)."""
    num = 0.0
    for i, row in enumerate(M):
        for j, p in enumerate(row):
            if i + j > line:
                num += p
    tot = sum(sum(row) for row in M)
    return max(0.0, min(1.0, num / (tot or 1.0)))


def matrix_btts(M):
    """P(both teams score): every cell (i>0 and j>0)."""
    num = sum(
        p for i, row in enumerate(M) for j, p in enumerate(row) if i > 0 and j > 0
    )
    tot = sum(sum(row) for row in M)
    return max(0.0, min(1.0, num / (tot or 1.0)))


def matrix_top_scores(M, n=3):
    flat = [((i, j), p) for i, row in enumerate(M) for j, p in enumerate(row) if p > 0]
    return sorted(flat, key=lambda kv: kv[1], reverse=True)[:n]


def normalise_1x2(ml, draw_price):
    total = sum(p for _, p in ml) + (draw_price or 0.0)
    if total <= 0:
        return None
    probs = [(team, p / total) for team, p in ml]
    if draw_price is not None:
        probs.append(("Draw", draw_price / total))
    return probs


def search_share(lam, targets):
    """Find goal-share s so Skellam(lam*s, lam*(1-s)) best matches 1X2 targets."""
    home_t, draw_t, away_t = targets

    def err(s):
        M = score_matrix(lam * s, lam * (1 - s), k_max=K)
        h, d, a = matrix_1x2(M)
        return (h - home_t) ** 2 + (d - draw_t) ** 2 + (a - away_t) ** 2

    best_s, best_e = 0.5, float("inf")
    for s in [x / 100 for x in range(20, 81)]:
        e = err(s)
        if e < best_e:
            best_e, best_s = e, s
    lo, hi = best_s - 0.01, best_s + 0.01
    for _ in range(50):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if err(m1) < err(m2):
            hi = m2
        else:
            lo = m1
    s = round((lo + hi) / 2, 4)
    return s, min(best_e, err(s))


def build_prediction(rec):
    """Full prediction dict from a MatchRecord."""
    ml = rec.get("moneyline") or []
    draw = rec.get("draw")
    totals = rec.get("totals") or {}
    probs_1x2 = normalise_1x2(ml, draw)
    lam = fit_lambda(totals)

    matrix = None
    if lam is not None and probs_1x2 and len(ml) >= 2:
        home_t, draw_t, away_t = [p for t, p in probs_1x2]
        share, fit_err = search_share(lam, (home_t, draw_t, away_t))
        lh, la = lam * share, lam * (1 - share)
        matrix = score_matrix(lh, la, k_max=K)

    ladder = []
    lines_for_model = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]
    for line in lines_for_model:
        row = {"line": line, "market": totals.get(line)}
        if matrix is not None:
            row["model"] = matrix_over(matrix, line)
        elif row["market"] is not None:
            row["model"] = None
        if row["market"] is not None or row.get("model") is not None:
            ladder.append(row)

    exp_score = (round(lh, 2), round(la, 2)) if matrix is not None else None

    return {
        "probs_1x2": probs_1x2,
        "lam": lam,
        "ladder": ladder,
        "btts_market": rec.get("btts"),
        "btts_model": matrix_btts(matrix) if matrix is not None else None,
        "exp_score": exp_score,
        "top_scores": matrix_top_scores(matrix) if matrix is not None else [],
        "best": max(probs_1x2, key=lambda kv: kv[1]) if probs_1x2 else None,
    }
