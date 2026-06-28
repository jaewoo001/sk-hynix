#!/usr/bin/env python3
"""Render data/latest.json into a self-contained Korean dashboard.html."""
import json, os, html, re
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(HERE, "data", "latest.json")
OUT = os.path.join(HERE, "dashboard.html")

FACTOR_LABELS = {
    "us_semis": "미국 반도체 (마이크론·SOX)",
    "us_broad": "미국 시장 전반 (나스닥·VIX)",
    "momentum": "하이닉스 모멘텀·평균회귀",
    "fx": "원/달러 환율",
    "technicals": "기술적 지표 (20일선·RSI)",
    "news": "뉴스·촉매",
    "foreign_flows": "외국인 순매수 (KRX)",
}
CALL_KR = {"UP": "상승", "DOWN": "하락", "NEUTRAL": "중립"}
CONV_KR = {"High": "높음", "Moderate": "보통", "Low": "낮음"}
ACT_KR = {"UP": "상승", "DOWN": "하락", "FLAT": "보합"}
CALL_COLORS = {"UP": "#0a7d3c", "DOWN": "#c02626", "NEUTRAL": "#9a6b00"}
CALL_BG = {"UP": "#e7f6ec", "DOWN": "#fcebeb", "NEUTRAL": "#fbf3e0"}


def esc(s):
    return html.escape(str(s))


def to_kst(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
        return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (iso[:16].replace("T", " ") if iso else "—")


def fmt(v, suffix="%"):
    return f"{v}{suffix}" if v is not None else "—"


def kr_note(factor, active, inp):
    if factor == "us_semis":
        return f"마이크론 {fmt(inp.get('mu_pct'))}, SOX {fmt(inp.get('sox_pct'))}"
    if factor == "us_broad":
        return f"나스닥 {fmt(inp.get('nasdaq_pct'))}, VIX {fmt(inp.get('vix_pct'))}"
    if factor == "momentum":
        return f"전일 등락 {fmt(inp.get('last_session_return_pct'))}"
    if factor == "fx":
        chg = inp.get("usdkrw_pct")
        if chg is None:
            return "환율 데이터 없음"
        d = "원화 약세→위험회피" if chg > 0 else ("원화 강세→위험선호" if chg < 0 else "보합")
        return f"원/달러 {chg:+.2f}% ({d})"
    if factor == "technicals":
        return "20일 이동평균·RSI 기준" if active else "워밍업 중 (약 15~20거래일 데이터 필요)"
    if factor == "news":
        return inp.get("news_note") or "—"
    if factor == "foreign_flows":
        return inp.get("foreign_flow_note") or "장 마감 후 공개 — 데이터 없음"
    return ""


def note_cell(factor, note):
    """The news note can be long, so render it as compact bullets so it stays
    narrow and doesn't force the other columns to wrap. Other notes stay plain."""
    if factor == "news" and note and note != "—":
        parts = [x.strip() for x in re.split(r',\s*|\.\s+|·\s*', note) if x.strip()]
        if len(parts) > 1:
            lis = "".join(f"<li>{esc(x)}</li>" for x in parts)
            return f'<td class="note"><ul class="cb">{lis}</ul></td>'
    return f'<td class="note">{esc(note)}</td>'


def bar(contribution, maxabs=42):
    pct = min(abs(contribution) / maxabs, 1.0) * 50.0
    color = "#0a7d3c" if contribution > 0 else ("#c02626" if contribution < 0 else "#999")
    if contribution >= 0:
        return (f'<div class="bar"><div class="bneg"></div><div class="bpos">'
                f'<div class="fill" style="width:{pct:.1f}%;background:{color}"></div></div></div>')
    return (f'<div class="bar"><div class="bneg" style="display:flex;justify-content:flex-end">'
            f'<div class="fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="bpos"></div></div>')


def main():
    with open(LATEST) as f:
        d = json.load(f)
    r = d["result"]
    call = r["call"]
    acc = d["accuracy"]
    inp = d["inputs"]
    gen = d.get("generated", "")
    gen_kst = to_kst(gen)

    rows = ""
    for b in r["breakdown"]:
        label = FACTOR_LABELS.get(b["factor"], b["factor"])
        note = kr_note(b["factor"], b["active"], inp)
        ncell = note_cell(b["factor"], note)
        if b["active"]:
            color = '#0a7d3c' if b['contribution'] > 0 else ('#c02626' if b['contribution'] < 0 else '#666')
            rows += ('<tr><td class="fname">' + esc(label) + '</td>'
                     f'<td class="num">{b["weight"]}</td>'
                     f'<td class="num">{b["sub"]:+.2f}</td>'
                     f'<td class="num strong" style="color:{color}">{b["contribution"]:+.1f}</td>'
                     f'<td>{bar(b["contribution"])}</td>' + ncell + '</tr>')
        else:
            rows += ('<tr class="inactive"><td class="fname">' + esc(label) + '</td>'
                     f'<td class="num">{b["weight"]}</td>'
                     f'<td class="num">—</td><td class="num">—</td>'
                     f'<td>{bar(0)}</td>' + ncell + '</tr>')

    recent = d.get("recent", [])[::-1]
    rrows = ""
    for p in recent:
        if p.get("correct") is True:
            outcome = '<span class="hit">✓ 적중</span>'
        elif p.get("correct") is False:
            outcome = '<span class="miss">✗ 빗나감</span>'
        elif p.get("actual") is None and p["target_date"] == d["target_date"]:
            outcome = '<span class="pend">대기중</span>'
        else:
            outcome = '<span class="na">— (중립)</span>'
        act = (f'{ACT_KR.get(p["actual"], p["actual"])} ({p["actual_return_pct"]:+.2f}%)'
               if p.get("actual") else "—")
        c = CALL_COLORS.get(p["call"], "#333")
        rrows += f'<tr><td>{esc(p["target_date"])}</td>' \
                 f'<td class="strong" style="color:{c}">{esc(CALL_KR.get(p["call"], p["call"]))}</td>' \
                 f'<td class="num">{p["score"]:+.0f}</td>' \
                 f'<td>{esc(act)}</td><td>{outcome}</td></tr>'
    if not rrows:
        rrows = '<tr><td colspan="5" class="note">아직 기록이 없습니다.</td></tr>'

    if acc.get("hit_rate") is not None:
        hit_line = (f'방향성 예측 {acc["hits"]} / {acc["graded"]} 적중 &nbsp;·&nbsp; '
                    f'적중률 <b>{acc["hit_rate"]}%</b>')
    else:
        hit_line = "아직 채점된 예측이 없습니다 — 첫 거래일 종료 후 적중률이 표시됩니다."

    prev_close = d.get("prev_close")
    prev_close_str = f"₩{prev_close:,.0f}" if prev_close else "—"
    ns = inp.get("news_sentiment")
    ns_str = f"{ns:+}" if ns is not None else "—"
    cbg = CALL_BG.get(call, "#eee")
    cc = CALL_COLORS.get(call, "#333")

    html_doc = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SK하이닉스 일일 주가 방향 예측</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:#f5f6f8; color:#1a1c1f;
  font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","Apple SD Gothic Neo","Segoe UI",Roboto,sans-serif; }}
.wrap {{ max-width:880px; margin:0 auto; padding:20px 16px 48px; }}
h1 {{ font-size:20px; margin:0 0 2px; }}
.sub {{ color:#666; font-size:13px; margin-bottom:18px; }}
.card {{ background:#fff; border:1px solid #e4e7eb; border-radius:14px; padding:20px;
  margin-bottom:16px; box-shadow:0 1px 2px rgba(0,0,0,.03); }}
.callbox {{ text-align:center; background:{cbg}; border:1px solid {cc}33; }}
.callword {{ font-size:54px; font-weight:800; letter-spacing:1px; line-height:1; color:{cc}; }}
.callmeta {{ margin-top:10px; font-size:14px; color:#444; }}
.pill {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px;
  font-weight:600; background:#fff; border:1px solid #ddd; margin:0 3px; }}
h2 {{ font-size:14px; letter-spacing:.3px; color:#555; margin:0 0 12px; font-weight:700; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; color:#888; font-weight:600; font-size:11px; padding:6px 8px; border-bottom:1px solid #eee; }}
td {{ padding:9px 8px; border-bottom:1px solid #f0f1f3; vertical-align:middle; }}
.fct {{ table-layout:fixed; }}
.fname {{ font-weight:600; word-break:keep-all; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.strong {{ font-weight:700; }}
.note {{ color:#777; font-size:11.5px; }}
.cb {{ margin:0; padding-left:15px; }}
.cb li {{ font-size:11px; color:#777; line-height:1.5; margin-bottom:2px; }}
tr.inactive {{ opacity:.5; }}
.bar {{ display:flex; width:120px; height:10px; }}
.bneg, .bpos {{ width:50%; height:100%; background:#f1f2f4; }}
.bneg {{ border-radius:5px 0 0 5px; }} .bpos {{ border-radius:0 5px 5px 0; }}
.fill {{ height:100%; border-radius:3px; }}
.hit {{ color:#0a7d3c; font-weight:600; }} .miss {{ color:#c02626; font-weight:600; }}
.pend {{ color:#9a6b00; }} .na {{ color:#999; }}
.disc {{ font-size:11.5px; color:#8a8d92; line-height:1.6; }}
.gloss td {{ vertical-align:top; font-size:12.5px; color:#444; line-height:1.6; }}
.gloss td:first-child {{ font-weight:700; color:#1a1c1f; white-space:nowrap; width:130px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
.kv {{ background:#fafbfc; border:1px solid #eef0f2; border-radius:10px; padding:10px 12px; }}
.kv .k {{ font-size:11px; color:#888; }}
.kv .v {{ font-size:16px; font-weight:700; margin-top:2px; }}
</style></head>
<body><div class="wrap">
  <h1>SK하이닉스 · KRX 000660 — 일일 주가 방향 예측</h1>
  <div class="sub">예측 대상 거래일 <b>{esc(d['target_date'])}</b> · 전일 종가 {prev_close_str} · 생성 {esc(gen_kst)} KST</div>

  <div class="card callbox">
    <div class="callword">{esc(CALL_KR.get(call, call))}</div>
    <div class="callmeta">
      <span class="pill">점수 {r['score']:+.0f}</span>
      <span class="pill">확신도 {esc(CONV_KR.get(r['conviction'], r['conviction']))} · 약 {r['confidence_pct']}%</span>
    </div>
  </div>

  <div class="card">
    <h2>근거 — 요인별 분석</h2>
    <table class="fct">
      <colgroup><col style="width:188px"><col style="width:56px"><col style="width:52px"><col style="width:60px"><col style="width:132px"><col></colgroup>
      <tr><th>요인</th><th class="num">가중치</th><th class="num">점수</th><th class="num">기여</th><th>−&nbsp;&nbsp;0&nbsp;&nbsp;+</th><th>세부 내용</th></tr>
      {rows}
    </table>
  </div>

  <div class="card">
    <h2>오늘의 시장 입력값</h2>
    <div class="grid">
      <div class="kv"><div class="k">하이닉스 전일 등락</div><div class="v">{fmt(inp.get('last_session_return_pct'))}</div></div>
      <div class="kv"><div class="k">마이크론 (야간)</div><div class="v">{fmt(inp.get('mu_pct'))}</div></div>
      <div class="kv"><div class="k">SOX (야간)</div><div class="v">{fmt(inp.get('sox_pct'))}</div></div>
      <div class="kv"><div class="k">나스닥</div><div class="v">{fmt(inp.get('nasdaq_pct'))}</div></div>
      <div class="kv"><div class="k">원/달러</div><div class="v">{fmt(inp.get('usdkrw'), '')}</div></div>
      <div class="kv"><div class="k">뉴스 심리</div><div class="v">{ns_str}</div></div>
    </div>
  </div>

  <div class="card">
    <h2>예측 적중 기록</h2>
    <p style="margin:0 0 14px;font-size:14px">{hit_line}</p>
    <table>
      <tr><th>거래일</th><th>예측</th><th class="num">점수</th><th>실제</th><th>결과</th></tr>
      {rrows}
    </table>
  </div>

  <div class="card">
    <h2>도움말 — 용어 설명</h2>
    <table class="gloss">
      <tr><th>용어</th><th>설명</th></tr>
      <tr><td>예측</td><td>다음 거래일에 SK하이닉스가 전일 종가 대비 오를지(<span style="color:#0a7d3c">상승</span>) · 내릴지(<span style="color:#c02626">하락</span>) · 뚜렷하지 않은지(중립)를 나타냅니다.</td></tr>
      <tr><td>점수</td><td>모든 요인을 합산한 종합 점수(−100 ~ +100). <b>+15 이상 상승</b>, <b>−15 이하 하락</b>, 그 사이는 중립. 0에서 멀수록 강한 신호입니다.</td></tr>
      <tr><td>확신도</td><td>점수의 크기를 신뢰 수준으로 환산한 값. 등급(높음/보통/낮음)과 백분율로 표시하며, 동전 던지기(50%)보다 얼마나 유리한지를 뜻하고 최대 72%로 제한됩니다. <b>확실성 보장이 아닙니다.</b></td></tr>
      <tr><td>가중치</td><td>각 요인의 중요도(합계 100). 클수록 종합 점수에 더 크게 반영됩니다. 데이터가 없는 요인은 비활성화되고 그 가중치는 나머지 요인에 재배분됩니다.</td></tr>
      <tr><td>점수(요인별)</td><td>각 요인의 방향·강도(−1 ~ +1).</td></tr>
      <tr><td>기여</td><td>그 요인이 종합 점수에 더한 실제 값(요인 점수 × 가중치). 막대는 음(−, 빨강)·양(+, 초록) 방향을 보여줍니다.</td></tr>
      <tr><td>예측 적중 기록</td><td>과거 예측을 실제 결과와 비교한 기록. 개별 예측이 아니라 <b>누적 적중률</b>로 평가하세요(중립 예측은 채점 제외).</td></tr>
    </table>
    <h2 style="margin-top:22px">요인 설명</h2>
    <table class="gloss">
      <tr><th>요인</th><th>의미</th></tr>
      <tr><td>미국 반도체</td><td>간밤 마이크론·필라델피아 반도체지수(SOX) 등락 — 가장 강력한 신호.</td></tr>
      <tr><td>미국 시장 전반</td><td>나스닥 등락과 VIX(공포지수).</td></tr>
      <tr><td>하이닉스 모멘텀</td><td>전일 등락폭(급락 시 반등 가능성도 반영).</td></tr>
      <tr><td>원/달러 환율</td><td>원화 약세는 위험회피 신호로 부정적.</td></tr>
      <tr><td>기술적 지표</td><td>20일 이동평균·RSI(거래일이 쌓이면 작동).</td></tr>
      <tr><td>뉴스·촉매</td><td>호재/악재 뉴스 심리.</td></tr>
      <tr><td>외국인 순매수</td><td>외국인 매매 동향(데이터가 있을 때만).</td></tr>
    </table>
  </div>

  <div class="card">
    <p class="disc"><b>투자 자문이 아닙니다.</b> 개별 종목의 일일 등락 예측은 동전 던지기에 가깝습니다. 이 도구는 우연보다 조금 더 자주 맞히는 것을 목표로 하는 투명한 휴리스틱이며, 판단 근거를 함께 보여줍니다. 확신도는 최대 72%로 제한됩니다. 개별 예측이 아니라 위의 적중 기록으로 판단하세요. 데이터 출처: 구글 파이낸스/yfinance(지연 시세).</p>
  </div>
</div></body></html>"""

    with open(OUT, "w") as f:
        f.write(html_doc)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
