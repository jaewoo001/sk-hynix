#!/usr/bin/env python3
"""Cloud (GitHub Actions) updater for the SK Hynix dashboard.

Runs on GitHub's servers (site updates even when your PC is off):
  1. fetch market data with yfinance (incl. ~4 months of Hynix closes),
  2. SEED price history so 20-day MA / RSI work immediately,
  3. fetch prior-session FOREIGN net buying of 000660 via pykrx (KRX),
  4. keyword news scan across many outlets (Google News RSS, EN+KO) + yfinance,
  5. write inputs JSON, run predict.py + build_dashboard.py, copy index.html.

Run `python cloud_update.py --selftest` to test offline logic.
"""
import json, os, re, html, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
HISTORY = os.path.join(DATA, "history.json")

TICKERS = {"hynix": "000660.KS", "mu": "MU", "sox": "^SOX",
           "nasdaq": "^IXIC", "vix": "^VIX", "usdkrw": "KRW=X"}

BULL = ["hbm", "ai memory", "demand", "record", "surge", "beat", "upgrade",
        "partnership", "shortage", "tight", "raise", "rally", "jump", "soar",
        "all-time high", "boom", "supercycle", "outperform", "high",
        "급등", "강세", "수요", "공급 부족", "품귀", "상향", "호재", "신고가",
        "사상 최대", "수주", "낙관", "반등", "돌파", "최대 실적", "흑자"]
BEAR = ["oversupply", "downgrade", "cut", "plunge", "selloff", "sell-off",
        "glut", "slump", "fear", "crash", "tumble", "miss", "weak demand",
        "correction", "bubble", "warns", "slowdown",
        "급락", "약세", "하향", "공급 과잉", "과잉", "우려", "악재", "폭락",
        "부진", "둔화", "경고", "조정", "버블", "매도", "적자", "쇼크"]

NEWS_QUERIES = [
    ("SK Hynix", "en-US", "US", "US:en"),
    ("SK하이닉스", "ko", "KR", "KR:ko"),
    ("Micron memory", "en-US", "US", "US:en"),
    ("HBM memory chip", "en-US", "US", "US:en"),
    ("D램 반도체 가격", "ko", "KR", "KR:ko"),
    ("반도체 수출 메모리", "ko", "KR", "KR:ko"),
]


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
        tl = t.lower()
        b = any(k in tl for k in BULL)
        r = any(k in tl for k in BEAR)
        if b and not r:
            bull += 1
        elif r and not b:
            bear += 1
    n = len(titles)
    raw = (bull - bear) / max(5, bull + bear)
    return max(-1.0, min(1.0, raw)), bull, bear, n


def flow_signal(vals):
    """Map recent foreign net-buy values (KRW) to a -1..+1 signal. Uses the last
    3 sessions vs the recent typical magnitude (persistence of foreign flows)."""
    vals = [float(v) for v in vals if v == v]
    if not vals:
        return None
    unit = (sum(abs(v) for v in vals) / len(vals)) or 1.0
    last3 = sum(vals[-3:])
    return max(-1.0, min(1.0, last3 / (unit * 3 * 1.5)))


def fetch_foreign_flow(last_date):
    """Prior-session foreign net buying of 000660 from KRX (via pykrx)."""
    try:
        from pykrx import stock
    except Exception as e:
        print(f"warn: pykrx unavailable: {e}")
        return None, None
    todate = last_date.strftime("%Y%m%d")
    fromdate = (last_date - timedelta(days=45)).strftime("%Y%m%d")
    try:
        df = stock.get_market_trading_value_by_date(fromdate, todate, "000660")
    except Exception as e:
        print(f"warn: foreign flow fetch failed: {e}")
        return None, None
    if df is None or df.empty:
        return None, None
    col = next((c for c in ("외국인합계", "외국인") if c in df.columns), None)
    if col is None:
        return None, None
    vals = [float(x) for x in df[col].tolist() if x == x]
    sig = flow_signal(vals)
    if sig is None:
        return None, None
    last1 = vals[-1] / 1e8
    sum3 = sum(vals[-3:]) / 1e8
    note = f"전일 외국인 순매수 {last1:+,.0f}억 · 최근 3일 {sum3:+,.0f}억"
    return round(sig, 2), note


def fetch_rss(query, hl, gl, ceid, limit=30):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + f"&hl={hl}&gl={gl}&ceid={ceid}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"warn: rss '{query}' failed: {e}")
        return []
    out = []
    for t in re.findall(r"<title>(.*?)</title>", xml, re.S)[1:limit + 1]:
        t = html.unescape(re.sub(r"<.*?>", "", t)).strip()
        if t:
            out.append(t)
    return out


def fetch_news_titles():
    titles = []
    for q, hl, gl, ceid in NEWS_QUERIES:
        titles += fetch_rss(q, hl, gl, ceid)
    try:
        import yfinance as yf
        for sym in ("000660.KS", "MU"):
            for n in (yf.Ticker(sym).news or []):
                t = (n.get("title") or n.get("content", {}).get("title") or "")
                if t:
                    titles.append(t)
    except Exception as e:
        print(f"warn: yfinance news failed: {e}")
    seen, uniq = set(), []
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k); uniq.append(t)
    return uniq


def fetch_market():
    import yfinance as yf
    out, last_date, hynix_series = {}, None, []
    for key, sym in TICKERS.items():
        try:
            period = "120d" if key == "hynix" else "10d"
            h = yf.Ticker(sym).history(period=period, interval="1d")
            chg, last = pct_and_last(list(h["Close"].values))
            out[key + "_pct"] = round(chg, 2) if chg is not None else None
            out[key + "_last"] = last
            if key == "hynix":
                for idx, val in zip(h.index, h["Close"].values):
                    if val == val:
                        hynix_series.append({"date": idx.date().isoformat(), "close": float(val)})
                if len(h.index):
                    last_date = h.index[-1].date()
        except Exception as e:
            print(f"warn: {sym} fetch failed: {e}")
            out[key + "_pct"] = None
            out[key + "_last"] = None
    return out, last_date, hynix_series


def seed_history(series):
    hist = json.load(open(HISTORY)) if os.path.exists(HISTORY) else {"closes": [], "predictions": []}
    have = {c["date"] for c in hist.get("closes", [])}
    added = 0
    for s in series:
        if s["date"] not in have:
            hist.setdefault("closes", []).append(s); have.add(s["date"]); added += 1
    hist["closes"].sort(key=lambda c: c["date"])
    json.dump(hist, open(HISTORY, "w"), indent=2)
    print(f"seeded {added} new closes (total {len(hist['closes'])})")


def main():
    if "--selftest" in sys.argv:
        run_selftest(); return

    mkt, last_date, hynix_series = fetch_market()
    if last_date is None:
        print("ERROR: could not fetch Hynix data; aborting."); sys.exit(1)
    target = next_business_day(last_date)
    seed_history(hynix_series)

    ff_sig, ff_note = fetch_foreign_flow(last_date)
    print(f"foreign flow: {ff_sig}  {ff_note}")

    titles = fetch_news_titles()
    ns, bull, bear, n = keyword_sentiment(titles)
    news_note = (f"키워드 스캔: 호재 {bull}건 · 악재 {bear}건 (다매체 제목 {n}개)"
                 if ns is not None else "뉴스 데이터 없음")

    inp = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "target_date": target.isoformat(),
        "last_session_date": last_date.isoformat(),
        "hynix_prev_close": mkt.get("hynix_last"),
        "last_session_return_pct": mkt.get("hynix_pct"),
        "sox_pct": mkt.get("sox_pct"), "mu_pct": mkt.get("mu_pct"), "nvda_pct": None,
        "nasdaq_pct": mkt.get("nasdaq_pct"), "vix_pct": mkt.get("vix_pct"),
        "usdkrw": round(mkt["usdkrw_last"], 2) if mkt.get("usdkrw_last") else None,
        "usdkrw_pct": mkt.get("usdkrw_pct"),
        "news_sentiment": round(ns, 2) if ns is not None else None,
        "news_note": news_note,
        "foreign_flow_signal": ff_sig,
        "foreign_flow_note": ff_note,
        "data_sources": ["yfinance + Google News RSS (multi-outlet) + pykrx(외국인 수급)"],
    }
    ipath = os.path.join(DATA, f"inputs_{target.isoformat()}.json")
    json.dump(inp, open(ipath, "w"), indent=2, ensure_ascii=False)

    py = sys.executable
    subprocess.run([py, "predict.py", "--inputs", ipath], cwd=HERE, check=True)
    subprocess.run([py, "build_dashboard.py"], cwd=HERE, check=True)
    import shutil
    shutil.copy(os.path.join(HERE, "dashboard.html"), os.path.join(HERE, "index.html"))
    print("wrote index.html")


def run_selftest():
    from datetime import date
    assert next_business_day(date(2026, 6, 26)).isoformat() == "2026-06-29"
    assert abs(pct_and_last([100.0, 110.0])[0] - 10) < 1e-9
    s, b, r, n = keyword_sentiment(["SK Hynix HBM demand surges to record",
                                    "메모리 공급 과잉 우려에 SK하이닉스 급락",
                                    "Micron beat earnings, raises targets",
                                    "삼성·SK하이닉스 신고가 경신"])
    assert b == 3 and r == 1, (b, r, n)
    assert flow_signal([1e11, 2e11, 3e11]) > 0      # strong net buying -> positive
    assert flow_signal([-3e11, -2e11, -1e11]) < 0   # net selling -> negative
    assert flow_signal([]) is None
    print(f"SELFTEST OK (news bull={b} bear={r}; flow+={flow_signal([1e11,2e11,3e11]):.2f})")


if __name__ == "__main__":
    main()
