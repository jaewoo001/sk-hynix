#!/usr/bin/env python3
"""
SK Hynix (KRX:000660) next-session direction estimator.

This is a TRANSPARENT, RULE-BASED scoring model. It is NOT a guarantee.
Daily stock direction is close to a coin flip; the goal here is a disciplined,
explainable signal that aims to be right somewhat more often than chance.

Division of labour:
  - The scheduled task (an LLM agent) fetches market pages, extracts the numbers,
    and writes them into a daily inputs JSON (plus a news_sentiment judgment).
  - THIS script does the deterministic math: scoring, decision, confidence,
    technical indicators, history bookkeeping, and accuracy grading.

Run:
  python3 predict.py --inputs data/inputs_2026-06-29.json

See methodology.md for the full rationale and weights.
"""

import argparse
import json
import math
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")

# ----------------------------------------------------------------------------
# Factor weights (sum to 100). These are now EVIDENCE-INFORMED -- see
# findings_report.md for the measured correlations they are derived from.
# Key data point (MSX/PANews 3-month backtest): SK Hynix daily-return
# correlation is SOX 0.36 / QQQ 0.34 / Nasdaq 0.31, rising to ~0.50 at the US
# open. Inactive factors (e.g. foreign_flows on days with no flow data) have
# their weight redistributed across the active ones, so the score stays -100..100.
# ----------------------------------------------------------------------------
WEIGHTS = {
    "us_semis": 36,        # overnight Micron + SOX   -> highest measured corr (~0.36-0.50)
    "us_broad": 12,        # overnight Nasdaq + VIX   -> corr ~0.31-0.34 but overlaps semis
    "momentum": 12,        # Hynix's own recent move  -> weak daily autocorr; bounce on extremes
    "fx": 8,               # USD/KRW                  -> risk/flow channel (won weak = down)
    "technicals": 10,      # price vs MA20, RSI14     -> warms up as history grows
    "news": 14,            # catalyst sentiment       -> drives the biggest single-day moves
    "foreign_flows": 8,    # KRX foreign net buying   -> high impact, used when data available
}

# Decision thresholds on the final score (range ~ -100..+100)
UP_THRESHOLD = 15
DOWN_THRESHOLD = -15


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ----------------------------------------------------------------------------
# Technical indicators computed from the model's own accumulated close history.
# ----------------------------------------------------------------------------
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(values, n=14):
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ----------------------------------------------------------------------------
# Sub-scores: each returns a value in [-1, +1].
# ----------------------------------------------------------------------------
def score_us_semis(inp):
    """Overnight US semiconductors. Micron is Hynix's closest peer, so it gets
    a slightly higher weight than the broad SOX index."""
    sox = inp.get("sox_pct")
    mu = inp.get("mu_pct")
    parts, weights = [], []
    if mu is not None:
        parts.append(mu); weights.append(0.6)
    if sox is not None:
        parts.append(sox); weights.append(0.4)
    if not parts:
        return None, "no overnight semis data"
    avg = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    # +/-2% overnight is a strong move -> saturate the signal there.
    sub = clip(avg / 2.0)
    return sub, f"semis avg {avg:+.2f}% (MU {mu}, SOX {sox})"


def score_us_broad(inp):
    """Overnight broad-market risk appetite: Nasdaq move, penalised by VIX spikes."""
    ndx = inp.get("nasdaq_pct")
    vix_pct = inp.get("vix_pct")
    if ndx is None and vix_pct is None:
        return None, "no broad-market data"
    sub = 0.0
    note = []
    if ndx is not None:
        sub += clip(ndx / 1.5)
        note.append(f"Nasdaq {ndx:+.2f}%")
    if vix_pct is not None:
        # A big VIX jump = fear = drag; a big VIX drop = calm = tailwind.
        sub += clip(-vix_pct / 30.0) * 0.5
        note.append(f"VIX {vix_pct:+.1f}%")
    return clip(sub), ", ".join(note)


def score_momentum(inp):
    """Hynix's own last session. Small moves -> mild trend-follow. Extreme moves
    (|r|>5%) -> add a mean-reversion tilt, because one-day spikes often partly
    reverse the next session."""
    r = inp.get("last_session_return_pct")
    if r is None:
        return None, "no last-session return"
    trend = clip(r / 4.0) * 0.5            # weak momentum component
    reversion = 0.0
    if r <= -5.0:
        reversion = +0.4                   # deeply down -> oversold bounce tilt
    elif r >= 5.0:
        reversion = -0.4                   # spiked up -> pullback tilt
    sub = clip(trend + reversion)
    return sub, f"last move {r:+.2f}% (trend {trend:+.2f}, reversion {reversion:+.2f})"


def score_fx(inp):
    """USD/KRW. EVIDENCE-CORRECTED: for daily moves the capital-flow / risk
    channel dominates the textbook exporter benefit. Won WEAKNESS (USD/KRW up)
    coincides with foreign outflows and down days; won STRENGTH (USD/KRW down)
    coincides with inflows and up days. So the sub-score is the negative of the
    USD/KRW % change. ~0.7% move saturates it; kept modest (noisy signal)."""
    chg = inp.get("usdkrw_pct")
    if chg is None:
        return None, "no FX data"
    sub = clip(-chg / 0.7)
    direction = "won weaker -> risk-off" if chg > 0 else ("won stronger -> risk-on" if chg < 0 else "flat")
    return sub, f"USD/KRW {chg:+.2f}% ({direction})"


def score_foreign_flows(inp):
    """KRX foreign-investor net buying of Hynix. One of the strongest directional
    drivers, but the data is published after the session, so it is only usable
    pre-open when an early estimate exists. Pass 'foreign_flow_signal' in -1..+1
    (the agent judges it from KRX/news data); otherwise this factor is inactive."""
    s = inp.get("foreign_flow_signal")
    if s is None:
        return None, "no foreign-flow data (published post-session)"
    return clip(float(s)), inp.get("foreign_flow_note", "")


def score_technicals(closes):
    """Price vs 20-day MA (trend) + RSI14 (overbought/oversold). Returns None
    until enough history has accumulated, so the model 'warms up'."""
    ma20 = sma(closes, 20)
    r = rsi(closes, 14)
    if ma20 is None and r is None:
        return None, "warming up (need ~15-20 sessions of history)"
    sub = 0.0
    note = []
    last = closes[-1]
    if ma20 is not None:
        gap = (last - ma20) / ma20
        sub += clip(gap / 0.05) * 0.6      # +/-5% from MA saturates
        note.append(f"px vs MA20 {gap*100:+.1f}%")
    if r is not None:
        if r >= 70:
            sub += -0.4
        elif r <= 30:
            sub += +0.4
        note.append(f"RSI {r:.0f}")
    return clip(sub), ", ".join(note)


def score_news(inp):
    """Catalyst sentiment, judged by the agent and passed in as -1..+1."""
    s = inp.get("news_sentiment")
    if s is None:
        return None, "no news judgment"
    return clip(float(s)), inp.get("news_note", "")


# ----------------------------------------------------------------------------
# Main scoring: combine sub-scores with weights; redistribute weight of any
# inactive factor so the final score stays on a -100..+100 scale.
# ----------------------------------------------------------------------------
def compute(inp, closes):
    subs = {
        "us_semis": score_us_semis(inp),
        "us_broad": score_us_broad(inp),
        "momentum": score_momentum(inp),
        "fx": score_fx(inp),
        "technicals": score_technicals(closes),
        "news": score_news(inp),
        "foreign_flows": score_foreign_flows(inp),
    }
    active = {k: v for k, v in subs.items() if v[0] is not None}
    active_weight = sum(WEIGHTS[k] for k in active)
    if active_weight == 0:
        raise SystemExit("No usable inputs -- cannot score.")

    breakdown = []
    score = 0.0
    for k, (sub, note) in subs.items():
        if sub is None:
            breakdown.append({"factor": k, "weight": WEIGHTS[k], "active": False,
                              "sub": None, "contribution": 0.0, "note": note})
            continue
        eff_weight = WEIGHTS[k] * 100.0 / active_weight   # rescaled
        contribution = sub * eff_weight
        score += contribution
        breakdown.append({"factor": k, "weight": round(eff_weight, 1), "active": True,
                          "sub": round(sub, 3), "contribution": round(contribution, 2),
                          "note": note})

    score = round(score, 2)
    if score >= UP_THRESHOLD:
        call = "UP"
    elif score <= DOWN_THRESHOLD:
        call = "DOWN"
    else:
        call = "NEUTRAL"

    mag = abs(score)
    if mag >= 40:
        conviction = "High"
    elif mag >= 15:
        conviction = "Moderate"
    else:
        conviction = "Low"
    # Edge over a coin flip, deliberately capped -- never claim certainty.
    confidence_pct = round(min(72.0, 50.0 + mag * 0.45), 1)

    return {
        "score": score,
        "call": call,
        "conviction": conviction,
        "confidence_pct": confidence_pct,
        "breakdown": breakdown,
    }


# ----------------------------------------------------------------------------
# History + accuracy bookkeeping
# ----------------------------------------------------------------------------
def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return {"closes": [], "predictions": []}


def grade_pending(history, inp):
    """Fill in realized outcomes for past predictions once their session close
    is known. inp may carry 'last_session_return_pct' tagged with 'last_session_date'."""
    realized_date = inp.get("last_session_date")
    realized_ret = inp.get("last_session_return_pct")
    if realized_date is None or realized_ret is None:
        return
    actual = "UP" if realized_ret > 0 else ("DOWN" if realized_ret < 0 else "FLAT")
    for p in history["predictions"]:
        if p["target_date"] == realized_date and p.get("actual") is None:
            p["actual_return_pct"] = realized_ret
            p["actual"] = actual
            if p["call"] == "NEUTRAL":
                p["correct"] = None       # no directional bet -> not graded
            else:
                p["correct"] = (p["call"] == actual)


def rolling_accuracy(history):
    graded = [p for p in history["predictions"] if p.get("correct") is not None]
    if not graded:
        return {"graded": 0, "hits": 0, "hit_rate": None}
    hits = sum(1 for p in graded if p["correct"])
    return {"graded": len(graded), "hits": hits,
            "hit_rate": round(100.0 * hits / len(graded), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True, help="path to daily inputs JSON")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(args.inputs) as f:
        inp = json.load(f)

    history = load_history()

    # 1) Grade any now-resolved past predictions.
    grade_pending(history, inp)

    # 2) Append the just-completed session's close to the price history (for technicals).
    lsd = inp.get("last_session_date")
    lsc = inp.get("hynix_prev_close")   # close going INTO the predicted session
    if lsd and lsc is not None:
        if not any(c["date"] == lsd for c in history["closes"]):
            history["closes"].append({"date": lsd, "close": lsc})
            history["closes"].sort(key=lambda c: c["date"])
    closes = [c["close"] for c in history["closes"]]

    # 3) Score the upcoming session.
    result = compute(inp, closes)

    record = {
        "as_of": inp.get("as_of", datetime.utcnow().isoformat()),
        "target_date": inp["target_date"],
        "prev_close": inp.get("hynix_prev_close"),
        "call": result["call"],
        "score": result["score"],
        "conviction": result["conviction"],
        "confidence_pct": result["confidence_pct"],
        "actual": None,
        "actual_return_pct": None,
        "correct": None,
    }
    # Replace any existing prediction for the same target date (re-runs).
    history["predictions"] = [p for p in history["predictions"]
                              if p["target_date"] != record["target_date"]]
    history["predictions"].append(record)
    history["predictions"].sort(key=lambda p: p["target_date"])

    acc = rolling_accuracy(history)

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)

    latest = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "target_date": inp["target_date"],
        "stock": "SK Hynix (KRX:000660)",
        "prev_close": inp.get("hynix_prev_close"),
        "inputs": inp,
        "result": result,
        "accuracy": acc,
        "recent": history["predictions"][-30:],
    }
    with open(LATEST_PATH, "w") as f:
        json.dump(latest, f, indent=2)

    # Console summary
    print(f"\n=== SK Hynix (000660) -- estimate for {inp['target_date']} ===")
    print(f"CALL: {result['call']}   score {result['score']:+.1f}   "
          f"({result['conviction']} conviction, ~{result['confidence_pct']}% lean)")
    print("-" * 64)
    for b in result["breakdown"]:
        if b["active"]:
            print(f"  {b['factor']:<11} w{b['weight']:<5} sub {b['sub']:+.2f} "
                  f"-> {b['contribution']:+6.1f}   {b['note']}")
        else:
            print(f"  {b['factor']:<11} (inactive)            {b['note']}")
    print("-" * 64)
    if acc["hit_rate"] is not None:
        print(f"Track record: {acc['hits']}/{acc['graded']} correct "
              f"({acc['hit_rate']}%) on directional calls.")
    else:
        print("Track record: no graded predictions yet (need >=1 completed session).")
    print()


if __name__ == "__main__":
    main()
