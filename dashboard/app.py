import sys
import subprocess
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import plotly.graph_objects as go

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

from config.loader import load_config
from dashboard import data
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

# ---------- CHARTS ----------
chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.subheader("Fundamental score over time")
    hist = data.overall_score_history(conn)
    if len(hist) < 2:
        st.info("Only one reading so far. This chart builds up as daily refreshes accumulate history.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=[h["as_of_date"] for h in hist], y=[h["fundamental_score"] for h in hist],
            mode="lines", name="Fundamental Score", line=dict(color=LINE_BLUE, width=2),
        ))
        fig.add_hline(y=cfg["signal_thresholds"]["buying_opportunity_min_fundamental"],
                       line_dash="dash", line_color="#9ca3af", annotation_text="Buying-opportunity floor")
        fig.add_hline(y=cfg["signal_thresholds"]["risk_reduce_max_fundamental"],
                       line_dash="dash", line_color="#9ca3af", annotation_text="Risk/reduce ceiling")
        fig.update_yaxes(range=[0, 100], title="Score")
        fig.update_layout(height=350, margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.subheader("Price vs. fundamentals (indexed to 100)")
    price_hist = data.price_history(conn, ticker)
    fscore_hist = data.overall_score_history(conn)
    if len(price_hist) < 2 or len(fscore_hist) < 2:
        st.info("Needs at least two days of history for both price and fundamental score. "
                "Builds up as daily refreshes accumulate.")
    else:
        p0 = price_hist[0]["value"]
        f0 = fscore_hist[0]["fundamental_score"] or 1
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=[p["obs_date"] for p in price_hist], y=[p["value"] / p0 * 100 for p in price_hist],
            mode="lines", name="MU Price (indexed)", line=dict(color=LINE_ORANGE, width=2),
        ))
        fig.add_trace(go.Scatter(
            x=[h["as_of_date"] for h in fscore_hist],
            y=[(h["fundamental_score"] / f0 * 100) if h["fundamental_score"] else None for h in fscore_hist],
            mode="lines", name="Fundamental Score (indexed)", line=dict(color=LINE_BLUE, width=2),
        ))
        fig.update_layout(height=350, margin=dict(t=10, b=10, l=10, r=10),
                           legend=dict(orientation="h", yanchor="bottom", y=1.02))
        fig.update_yaxes(title="Indexed to 100 at start of window")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Both series rebased to 100 at the first available date — this is a single shared axis, "
                   "not a dual-axis chart, so divergence between price and fundamentals is directly readable.")

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
st.subheader("News & evidence feed")
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
