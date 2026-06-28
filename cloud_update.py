#!/usr/bin/env python3
"""Cloud (GitHub Actions) updater for the SK Hynix dashboard.

Runs on GitHub's servers (so the site updates even when your PC is off):
  1. fetch market data with yfinance (incl. ~3 months of Hynix closes),
  2. SEED the price history so 20-day MA / RSI work immediately,
  3. do a keyword-based news sentiment scan,
  4. write the daily inputs JSON,
  5. run predict.py (the scoring engine) and build_dashboard.py,
  6. copy dashboard.html -> index.html for GitHub Pages.

Run `python cloud_update.py --selftest` to test offline logic.
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
HISTORY = os.path.join(DATA, "history.json")

TICKERS = {"hynix": "000660.KS", "mu": "MU", "sox": "^SOX",
           "nasdaq": "^IXIC", "vix": "^VIX", "usdkrw": "KRW=X"}

BULL = ["hbm", "ai memory", "demand", "record", "surge", "beat", "upgrade",
        "partnership", "shortage", "tight", "raise", "rally", "jump", "soar",
        "all-time high", "boom", "supercycle", "outperform"]
BEAR = ["oversupply", "downgrade", "cut", "plunge", "selloff", "sell-off",
        "glut", "slump", "fear", "crash", "tumble", "miss", "weak demand",
        "correction", "bubble", "warns", "slowdown"]


def next_business_day(d):
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def pct_and_last(closes):
    closes = [float(c) for c in closes if c == c]
    if len(closes) < 2:
        return None, (closes[-1] if closes else None)
    return (closes[-1] - closes[-2]) / closes[-2] * 100.0, closes[-1]


def keyword_sentiment(titles):
    if not titles:
        return None, 0, 0, 0
    bull = bear = 0
    for t in titles:
        b = any(k in t for k in BULL)
        r = any(k in t for k in BEAR)
        if b and not r:
            bull += 1
        elif r and not b:
            bear += 1
    n = len(titles)
    raw = (bull - bear) / max(3, bull + bear)
    return max(-1.0, min(1.0, raw)), bull, bear, n


def fetch_market():
    import yfinance as yf
    out = {}
    last_date = None
    hynix_series = []
    for key, sym in TICKERS.items():
        try:
            period = "120d" if key == "hynix" else "10d"
            h = yf.Ticker(sym).history(period=period, interval="1d")
            closes = list(h["Close"].values)
            chg, last = pct_and_last(closes)
            out[key + "_pct"] = round(chg, 2) if chg is not None else None
            out[key + "_last"] = last
            if key == "hynix":
                for idx, val in zip(h.index, h["Close"].values):
                    if val == val:
                        hynix_series.append({"date": idx.date().isoformat(),
                                             "close": float(val)})
                if len(h.index):
                    last_date = h.index[-1].date()
        except Exception as e:
            print(f"warn: {sym} fetch failed: {e}")
            out[key + "_pct"] = None
            out[key + "_last"] = None
    return out, last_date, hynix_series


def seed_history(series):
    """Merge fetched daily closes into history.json so the technicals factor
    (20-day MA, RSI-14) has enough data to activate right away."""
    if os.path.exists(HISTORY):
        hist = json.load(open(HISTORY))
    else:
        hist = {"closes": [], "predictions": []}
    have = {c["date"] for c in hist.get("closes", [])}
    added = 0
    for s in series:
        if s["date"] not in have:
            hist.setdefault("closes", []).append(s)
            have.add(s["date"])
            added += 1
    hist["closes"].sort(key=lambda c: c["date"])
    json.dump(hist, open(HISTORY, "w"), indent=2)
    print(f"seeded {added} new closes (total {len(hist['closes'])})")


def fetch_news_titles():
    import yfinance as yf
    titles = []
    for sym in ("000660.KS", "MU"):
        try:
            for n in (yf.Ticker(sym).news or []):
                t = (n.get("title") or n.get("content", {}).get("title") or "")
                if t:
                    titles.append(t.lower())
        except Exception as e:
            print(f"warn: news {sym} failed: {e}")
    return titles


def main():
    if "--selftest" in sys.argv:
        run_selftest()
        return

    mkt, last_date, hynix_series = fetch_market()
    if last_date is None:
        print("ERROR: could not fetch Hynix data; aborting without changes.")
        sys.exit(1)
    target = next_business_day(last_date)

    seed_history(hynix_series)   # <-- makes 20-day MA / RSI work immediately

    titles = fetch_news_titles()
    ns, bull, bear, n = keyword_sentiment(titles)
    news_note = (f"키워드 스캔: 호재 {bull}건 · 악재 {bear}건 (제목 {n}개)"
                 if ns is not None else "뉴스 데이터 없음")

    inp = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "target_date": target.isoformat(),
        "last_session_date": last_date.isoformat(),
        "hynix_prev_close": mkt.get("hynix_last"),
        "last_session_return_pct": mkt.get("hynix_pct"),
        "sox_pct": mkt.get("sox_pct"), "mu_pct": mkt.get("mu_pct"),
        "nvda_pct": None,
        "nasdaq_pct": mkt.get("nasdaq_pct"), "vix_pct": mkt.get("vix_pct"),
        "usdkrw": round(mkt["usdkrw_last"], 2) if mkt.get("usdkrw_last") else None,
        "usdkrw_pct": mkt.get("usdkrw_pct"),
        "news_sentiment": round(ns, 2) if ns is not None else None,
        "news_note": news_note,
        "data_sources": ["yfinance (GitHub Actions); keyword news scan"],
    }
    ipath = os.path.join(DATA, f"inputs_{target.isoformat()}.json")
    with open(ipath, "w") as f:
        json.dump(inp, f, indent=2, ensure_ascii=False)
    print("inputs:", json.dumps(inp, ensure_ascii=False))

    py = sys.executable
    subprocess.run([py, "predict.py", "--inputs", ipath], cwd=HERE, check=True)
    subprocess.run([py, "build_dashboard.py"], cwd=HERE, check=True)
    import shutil
    shutil.copy(os.path.join(HERE, "dashboard.html"), os.path.join(HERE, "index.html"))
    print("wrote index.html")


def run_selftest():
    from datetime import date
    assert next_business_day(date(2026, 6, 26)).isoformat() == "2026-06-29"
    assert next_business_day(date(2026, 6, 29)).isoformat() == "2026-06-30"
    c, last = pct_and_last([100.0, 110.0])
    assert abs(c - 10.0) < 1e-9 and last == 110.0
    s, b, r, n = keyword_sentiment(["sk hynix hbm demand surges to record",
                                    "analyst warns of memory bubble, downgrade",
                                    "micron beat earnings, raises guidance"])
    assert b == 2 and r == 1 and n == 3, (b, r, n)
    assert keyword_sentiment(["memory shortage could last for years"])[0] > 0
    print("SELFTEST OK")


if __name__ == "__main__":
    main()
