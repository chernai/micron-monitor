import sys
import subprocess
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Streamlit Community Cloud's secrets manager populates st.secrets, not
# os.environ — bridge it here so db/init_db.py's plain os.environ.get(...)
# works the same way locally (.env), on Streamlit Cloud (st.secrets), and
# in GitHub Actions (a workflow env var) without three code paths.
import os
try:
    if "DATABASE_URL" in st.secrets:
        os.environ.setdefault("DATABASE_URL", st.secrets["DATABASE_URL"])
except Exception:
    pass  # no secrets.toml configured (e.g. plain local run using .env) — fine

import pandas as pd

from config.loader import load_config
from dashboard import data
from scoring import technicals
from scripts.background_scheduler import start_background_scheduler

start_background_scheduler()  # no-op unless MICRON_MONITOR_ENABLE_SCHEDULER=1 (set on hosted deploys)

st.set_page_config(page_title="Micron Monitor", layout="wide", page_icon="🧠")

# Status colors are reserved for signal state only — never reused as a
# generic series color elsewhere on the page.
SIGNAL_STYLE = {
    "BUYING_OPPORTUNITY": ("🟢", "BUYING OPPORTUNITY", "#16a34a"),
    "NEUTRAL_WAIT": ("🟡", "NEUTRAL / WAIT", "#ca8a04"),
    "RISK_REDUCE": ("🔴", "RISK / REDUCE", "#dc2626"),
}
COMPONENT_LABELS = {
    "hbm_demand": "HBM Demand",
    "dram_pricing": "DRAM Pricing",
    "gross_margins": "Gross Margins",
    "customer_capex": "Customer Capex",
    "valuation": "Valuation",
}
TREND_ARROW = {"up": "↑", "down": "↓", "stable": "→", None: "•"}
TIER_LABEL = {
    "FACT": "FACT", "MANAGEMENT_GUIDANCE": "GUIDANCE", "ANALYST_ESTIMATE": "ANALYST EST.",
    "INDUSTRY_ESTIMATE": "INDUSTRY EST.", "NEWS_REPORT": "NEWS", "INFERENCE": "INFERENCE",
}
LINE_BLUE = "#2563eb"
LINE_ORANGE = "#ea580c"


def badge(score):
    if score is None:
        return "⚪"
    if score >= 65:
        return "🟢"
    if score >= 45:
        return "🟡"
    return "🔴"


cfg = load_config()
conn = data.get_conn()
ticker = cfg["subject_ticker"]

st.title("🧠 Micron Monitor")
st.caption("Fundamentally-driven monitoring for MU — not a price predictor. "
           "Answers one question: given HBM demand, DRAM pricing, Micron margins, and customer AI capex, "
           "is today's price an attractive risk/reward entry?")

with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Run full refresh now", use_container_width=True):
        with st.spinner("Running collectors + scoring + alerts (this can take a minute)..."):
            result = subprocess.run(
                [sys.executable, "-m", "scripts.refresh"],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            )
        if result.returncode == 0:
            st.success("Refresh complete.")
        else:
            st.error("Refresh failed — see logs below.")
            st.code(result.stdout[-3000:] + "\n" + result.stderr[-3000:])
        st.rerun()

    st.divider()
    st.subheader("Component weights")
    for k, v in cfg["weights"].items():
        st.text(f"{COMPONENT_LABELS.get(k, k)}: {v}%")
    st.caption("Edit config/config.yaml to change weights or thresholds.")

overall = data.latest_overall_score(conn)
components = data.latest_component_scores(conn)

if not overall:
    st.warning("No scores computed yet. Run `python -m scripts.refresh` from the project root, "
               "or click 'Run full refresh now' in the sidebar.")
    st.stop()

# ---------- TOP BAR ----------
price = data.latest_metric(conn, "price_usd", ticker)
daily_chg = data.latest_metric(conn, "price_change_1d_pct", ticker)
dist_high = data.latest_metric(conn, "price_dist_from_52w_high_pct", ticker)
fwd_pe = data.latest_metric(conn, "forward_pe", ticker)

c1, c2, c3, c4 = st.columns(4)
c1.metric("MU Price", f"${price['value']:.2f}" if price else "—",
          f"{daily_chg['value']:+.2f}%" if daily_chg else None)
c2.metric("52-Week Position", f"{dist_high['value']:+.1f}% from high" if dist_high else "—")
c3.metric("Forward P/E", f"{fwd_pe['value']:.1f}x" if fwd_pe else "—")
c4.metric("As of", overall["as_of_date"])

st.divider()

# ---------- PRICE ACTION & FUNDAMENTALS (combined chart) ----------
st.subheader("📊 Price Action & Fundamentals")
st.caption("Fundamental score, price, moving averages, support/resistance, volume, and RSI — superimposed "
           "on one shared timeline since they all describe the same price series. The fundamental score "
           "and its dashed thresholds are the investment thesis; everything else here (MAs, S/R, volume, "
           "RSI) is short-term trading context that never feeds the score or the signal above.")

tech = technicals.compute_all(cfg, conn, ticker)
pv_series = technicals.get_price_volume_series(conn, ticker)

tcol1, tcol2 = st.columns(2)
vr = tech["volume_regime"]
with tcol1:
    if vr["insufficient_data"]:
        st.metric("Volume Regime", "Insufficient data")
    else:
        label = {"ABOVE_AVERAGE": "🟢 Above average", "BELOW_AVERAGE": "🔴 Below average",
                  "IN_LINE": "🟡 In line", "UNKNOWN": "—"}[vr["regime"]]
        st.metric(f"Volume Regime ({vr['short_days']}d vs {vr['long_days']}d avg)", label,
                  f"{vr['ratio'] * 100:.0f}% of longer average")
    if not vr["insufficient_data"]:
        st.caption(vr["rationale"])

vb = tech["volume_balance"]
with tcol2:
    if vb["insufficient_data"]:
        st.metric("Volume Balance", "Insufficient data")
    else:
        label = {"NET_DISTRIBUTION": "🔴 Bearish (net distribution)",
                  "NET_ACCUMULATION": "🟢 Bullish (net accumulation)",
                  "BALANCED": "🟡 Neutral (balanced)", "UNKNOWN": "—"}[vb["direction"]]
        st.metric(f"{vb['window_days']}-Day Volume Balance", label, f"{vb['balance_pct']:+.2f}%")
    if not vb["insufficient_data"]:
        st.caption(vb["rationale"])

sr = tech["support_resistance"]
if sr["insufficient_data"]:
    st.info(sr["rationale"])
else:
    scol1, scol2, scol3, scol4, scol5 = st.columns(5)
    for col, label, point in [
        (scol1, "Macro Floor", sr["macro_floor"]),
        (scol2, "Structural Support", sr["structural_support"]),
        (scol3, "Near-Term Support", sr["near_term_support"]),
        (scol4, "Near-Term Resistance", sr["near_term_resistance"]),
        (scol5, "Major Resistance", sr["major_resistance"]),
    ]:
        with col:
            if point:
                st.metric(label, f"${point['price']:.2f}", point["date"])
            else:
                st.metric(label, "—")
    st.caption(sr["rationale"])

fscore_hist = data.overall_score_history(conn)

if len(pv_series) < 2 or len(fscore_hist) < 2:
    st.info("Needs at least two days of history for price, volume, and fundamental score. Builds up as "
            "daily refreshes accumulate (or run a historical backfill — see scripts/backfill_history.py).")
else:
    dates = [s[0] for s in pv_series]
    closes = [s[1] for s in pv_series]
    volumes = [s[2] for s in pv_series]
    score_dates = [h["as_of_date"] for h in fscore_hist if h["fundamental_score"] is not None]
    score_values = [h["fundamental_score"] for h in fscore_hist if h["fundamental_score"] is not None]

    ma50 = technicals.moving_average_series(closes, 50)
    ma200 = technicals.moving_average_series(closes, 200)
    rsi = technicals.rsi_series(closes, 14)
    long_n = cfg["technicals"]["volume_avg_long_days"]
    vol_ma = technicals.moving_average_series(volumes, long_n)

    up_x, up_y, down_x, down_y, flat_x, flat_y = [], [], [], [], [], []
    for i, (d, close, vol) in enumerate(pv_series):
        if i == 0:
            flat_x.append(d); flat_y.append(vol)
            continue
        prev_close = pv_series[i - 1][1]
        if close > prev_close:
            up_x.append(d); up_y.append(vol)
        elif close < prev_close:
            down_x.append(d); down_y.append(vol)
        else:
            flat_x.append(d); flat_y.append(vol)

    # ONE chart, one plot area -- everything below is layered into it via
    # overlaying y-axes rather than separate subplot rows. Volume and RSI
    # get their own axes squeezed (via an inflated range) into thin bands
    # at the bottom of the SAME plot rather than full-height, so they read
    # as sub-indicator strips on one chart rather than boxed-off panels.
    vol_max = max(volumes) if volumes else 1
    volume_band, rsi_band = 0.15, (0.15, 0.35)  # fraction of plot height each occupies
    volume_axis_range = [0, vol_max / volume_band]
    # solve so RSI's real [0,100] maps to the [rsi_band[0], rsi_band[1]] fraction
    rsi_span = 100 / (rsi_band[1] - rsi_band[0])
    rsi_axis_min = -rsi_band[0] * rsi_span
    rsi_axis_range = [rsi_axis_min, rsi_axis_min + rsi_span]

    fig = go.Figure()
    fig.add_trace(go.Bar(x=score_dates, y=score_values, name="Fundamental Score",
                          marker_color=LINE_BLUE, opacity=0.35, yaxis="y"))
    fig.add_trace(go.Scatter(x=dates, y=closes, mode="lines", name="MU Price",
                              line=dict(color=LINE_ORANGE, width=2.2), yaxis="y2"))
    fig.add_trace(go.Scatter(x=dates, y=ma50, mode="lines", name="50-day MA",
                              line=dict(color="#7c3aed", width=1.2), yaxis="y2"))
    fig.add_trace(go.Scatter(x=dates, y=ma200, mode="lines", name="200-day MA",
                              line=dict(color="#0891b2", width=1.2), yaxis="y2"))
    fig.add_trace(go.Bar(x=up_x, y=up_y, name="Volume (up day)", marker_color="#16a34a",
                          opacity=0.5, yaxis="y3"))
    fig.add_trace(go.Bar(x=down_x, y=down_y, name="Volume (down day)", marker_color="#dc2626",
                          opacity=0.5, yaxis="y3"))
    if flat_x:
        fig.add_trace(go.Bar(x=flat_x, y=flat_y, name="Volume (flat)", marker_color="#9ca3af",
                              opacity=0.5, yaxis="y3"))
    fig.add_trace(go.Scatter(x=dates, y=vol_ma, mode="lines", name=f"{long_n}-day avg volume",
                              line=dict(color="#1f2937", width=1, dash="dot"), yaxis="y3"))
    fig.add_trace(go.Scatter(x=dates, y=rsi, mode="lines", name="RSI (14)",
                              line=dict(color="#b45309", width=1.3), yaxis="y4"))
    # The RSI axis has no visible tick labels (its range is artificially
    # stretched to squeeze the line into a bottom band, so raw ticks would
    # show meaningless numbers) -- tag the line's current value directly
    # instead, since that's the only way to actually read it off the chart.
    if rsi and rsi[-1] is not None:
        fig.add_annotation(
            x=dates[-1], y=rsi[-1], yref="y4", xref="x",
            text=f" RSI {rsi[-1]:.0f} ", showarrow=False, xanchor="left",
            font=dict(color="#b45309", size=12), bgcolor="rgba(255,255,255,0.85)",
        )

    buy_floor = cfg["signal_thresholds"]["buying_opportunity_min_fundamental"]
    risk_ceiling = cfg["signal_thresholds"]["risk_reduce_max_fundamental"]
    fig.add_hline(y=buy_floor, line_dash="dash", line_color="#16a34a",
                  annotation_text=f"Buying-opportunity floor ({buy_floor})", annotation_position="top left",
                  yref="y")
    fig.add_hline(y=risk_ceiling, line_dash="dash", line_color="#dc2626",
                  annotation_text=f"Risk/reduce ceiling ({risk_ceiling})", annotation_position="bottom left",
                  yref="y")
    if not sr["insufficient_data"]:
        level_style = [
            ("macro_floor", "Macro floor", "#6b7280"),
            ("structural_support", "Structural support", "#16a34a"),
            ("near_term_support", "Near-term support", "#86efac"),
            ("near_term_resistance", "Near-term resistance", "#fca5a5"),
            ("major_resistance", "Major resistance", "#dc2626"),
        ]
        for key, label, color in level_style:
            point = sr[key]
            if point:
                fig.add_hline(y=point["price"], line_dash="dot", line_color=color,
                              annotation_text=f"{label} (${point['price']:.0f})",
                              annotation_position="right", annotation_xanchor="left", yref="y2")
    rsi_70 = rsi_axis_min + 70 / 100 * rsi_span
    rsi_30 = rsi_axis_min + 30 / 100 * rsi_span
    fig.add_hline(y=rsi_70, line_dash="dot", line_color="#dc2626", line_width=1,
                  annotation_text="RSI overbought (70)", annotation_position="right", yref="y4")
    fig.add_hline(y=rsi_30, line_dash="dot", line_color="#16a34a", line_width=1,
                  annotation_text="RSI oversold (30)", annotation_position="right", yref="y4")

    fig.update_layout(
        height=750, margin=dict(t=20, b=10, l=10, r=190), barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=1.0),
        xaxis=dict(domain=[0, 1]),
        yaxis=dict(title="Fundamental Score (0-100)", range=[0, 100], side="left"),
        yaxis2=dict(title="Price (USD)", overlaying="y", side="right", anchor="x"),
        yaxis3=dict(overlaying="y", side="right", position=0.97, range=volume_axis_range,
                     showgrid=False, showticklabels=False, anchor="free"),
        yaxis4=dict(overlaying="y", side="left", position=0.03, range=rsi_axis_range,
                     showgrid=False, showticklabels=False, anchor="free"),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "One chart, everything layered on it: fundamental score (bars, left axis) vs MU price (line, "
        "right axis) with 50/200-day moving averages and support/resistance levels on the price scale, "
        "the same buying-opportunity/risk-reduce thresholds that drive the signal below, volume "
        "(colored by day direction, with its own average) squeezed into the bottom strip, and RSI(14) "
        "in the band just above it — above its dotted 'overbought' line or below 'oversold' is the "
        "conventional read. Moving averages, S/R, volume, and RSI are trading context only — none of "
        "them feed the fundamental score or the signal."
    )

st.divider()

# ---------- MAIN SIGNAL ----------
emoji, label, color = SIGNAL_STYLE.get(overall["signal"], ("⚪", "UNKNOWN", "#6b7280"))
st.markdown(
    f"""
    <div style="border:2px solid {color}; border-radius:12px; padding:20px 24px; margin-bottom:12px;">
        <div style="font-size:28px; font-weight:700; color:{color};">{emoji} {label}</div>
        <div style="margin-top:8px; font-size:15px; color: var(--text-color, #444);">
            Fundamental Score: <b>{overall['fundamental_score']:.0f}/100</b> &nbsp;|&nbsp;
            Valuation Score: <b>{overall['valuation_score']:.0f}/100</b> &nbsp;|&nbsp;
            Confidence: <b>{overall['confidence']}</b>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ---------- IS TODAY BETTER THAN N DAYS AGO? ----------
score_hist_for_deltas = data.overall_score_history(conn)
days_collected = len({h["as_of_date"] for h in score_hist_for_deltas})
st.caption(f"Fundamental score vs recent history ({days_collected} day(s) of history collected so far):")
delta_cols = st.columns(3)
for col, (label, n_days) in zip(delta_cols, [("1 day ago", 1), ("7 days ago", 7), ("30 days ago", 30)]):
    result = data.score_delta_vs(score_hist_for_deltas, n_days)
    with col:
        if result is None:
            st.metric(label, "Insufficient history")
        else:
            st.metric(
                label,
                f"{result['latest_score']:.0f}/100",
                f"{result['delta']:+.0f} vs {result['reference_score']:.0f} on {result['reference_date']}",
            )

st.divider()


# ---------- FOUR FUNDAMENTALS (+ valuation) ----------
st.subheader("Components")
cols = st.columns(5)
for i, comp_key in enumerate(["hbm_demand", "dram_pricing", "gross_margins", "customer_capex", "valuation"]):
    c = components.get(comp_key)
    with cols[i]:
        if not c or c["insufficient_data"] or c["score"] is None:
            st.metric(COMPONENT_LABELS[comp_key], "Insufficient data")
        else:
            st.metric(COMPONENT_LABELS[comp_key],
                      f"{badge(c['score'])} {c['score']:.0f}/100",
                      f"{TREND_ARROW.get(c['trend'], '•')} {c['confidence']}")

st.divider()

# ---------- PEER COMPARISON (context only, no signal) ----------
st.subheader("🌏 Peer Comparison")
st.caption("Context, not a signal — SK Hynix and Samsung have no usable SEC-filed financials (SK Hynix "
           "files bare 6-Ks as a foreign private issuer with no structured XBRL; Samsung isn't SEC-registered "
           "at all), so unlike MU these numbers come from Yahoo Finance's own fundamentals data rather than "
           "a citable filing — real, but lower-confidence and shallower history.")

peer_rows = []
for pticker, pname in [(ticker, "Micron (MU)")] + [(t, i["name"]) for t, i in cfg.get("peers", {}).items()]:
    gm_series = data.metric_series(conn, "gross_margin_pct", pticker, limit=2)
    gm = gm_series[-1] if gm_series else None
    gm_delta = (gm_series[-1]["value"] - gm_series[-2]["value"]) if len(gm_series) == 2 else None
    fpe = data.latest_metric(conn, "forward_pe", pticker)
    price_p = data.latest_metric(conn, "price_usd", pticker)
    peer_rows.append({
        "Company": pname,
        "Price": f"${price_p['value']:.2f}" if price_p else "—",
        "Gross Margin (latest qtr)": f"{gm['value']:.1f}%" if gm else "—",
        "Qtr-over-Qtr": f"{gm_delta:+.1f}pp" if gm_delta is not None else "—",
        "Forward P/E": f"{fpe['value']:.1f}x" if fpe else "—",
    })
st.dataframe(pd.DataFrame(peer_rows), use_container_width=True, hide_index=True)

st.divider()

# ---------- WHY THE SIGNAL ----------
st.subheader("Why this signal?")
if overall.get("explanation"):
    parts = overall["explanation"].split(" | ")
    st.markdown(f"**{parts[0]}**")
    for bullet in parts[1:]:
        st.markdown(f"- {bullet}")
missing = None
try:
    import json as _json
    missing = _json.loads(overall.get("missing_components") or "[]")
except Exception:
    pass
if missing:
    st.info(f"Excluded from fundamental score (insufficient data): {', '.join(COMPONENT_LABELS.get(m, m) for m in missing)}")

with st.expander("Component rationale (evidence-based, not a black box)"):
    for comp_key in ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex", "valuation"]:
        c = components.get(comp_key)
        if not c:
            continue
        st.markdown(f"**{COMPONENT_LABELS[comp_key]}**")
        st.write(c["rationale"])
        st.markdown("---")

st.divider()

# ---------- ALERTS ----------
st.subheader("Alerts")
alerts = data.recent_alerts(conn, limit=15)
if not alerts:
    st.caption("No alerts fired yet.")
else:
    for a in alerts:
        icon = "🟢" if a["severity"] == "GREEN" else "🔴"
        st.markdown(f"{icon} **{a['triggered_at'][:16].replace('T', ' ')}** — {a['message']}")

st.divider()

# ---------- NEWS / EVIDENCE FEED ----------
with st.expander("📰 News & evidence feed"):
    tabs = st.tabs([COMPONENT_LABELS[c] for c in ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex"]])
    for tab, comp_key in zip(tabs, ["hbm_demand", "dram_pricing", "gross_margins", "customer_capex"]):
        with tab:
            items = data.recent_observations(conn, comp_key, limit=12)
            if not items:
                st.caption("No items collected yet.")
            for it in items:
                tier = TIER_LABEL.get(it["source_type"], it["source_type"])
                conf = it["confidence"]
                title = it["text_excerpt"] or f"{it.get('metric_key','')}: {it.get('value','')}"
                link = it["source_url"]
                line = f"`{tier}` `{conf}` **{it['obs_date']}** — "
                if link:
                    line += f"[{title}]({link})"
                else:
                    line += title
                line += f"  \n<sub>{it['source_name']}</sub>"
                st.markdown(line, unsafe_allow_html=True)

conn.close()
