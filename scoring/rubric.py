"""Transparent, deterministic scoring rules. No black-box math: every score
is a sum of explainable point bands, and every band is listed here so the
rationale shown in the dashboard can be regenerated from this file alone.
"""

# --- Keyword rubric for HBM Demand / DRAM Pricing (the two categories with
# no free structured data feed). This is a plain keyword heuristic over
# news headlines, NOT NLP/ML classification — it is intentionally crude and
# fully auditable: every matched keyword and headline is kept as evidence.
STRONG_KEYWORDS = {
    "hbm_demand": [
        "sold out", "fully allocated", "allocated", "sole source", "capacity expansion",
        "expand capacity", "expanding capacity", "record revenue", "record demand",
        "raises guidance", "raised guidance", "increases forecast", "raises forecast",
        "share gain", "gaining share", "prepayment", "long-term agreement",
        "long-term supply agreement", "securing supply", "secures supply",
        "demand accelerat", "demand outpac", "tight supply", "supply shortage",
        "hbm4 adoption", "qualified", "qualification win", "capacity sold",
        "capex increase", "strong demand", "demand surge", "backs a", "buildout",
    ],
    "dram_pricing": [
        "price increase", "prices rise", "prices rose", "price hike", "raises prices",
        "contract price up", "contract prices rise", "tight supply", "shortage",
        "prices surge", "prices jump", "bullish", "price momentum", "prices climb",
        "pricing power", "prices soar",
    ],
}

WEAK_KEYWORDS = {
    "hbm_demand": [
        "inventory build", "inventory increase", "delay", "delaying orders",
        "push out", "pushed out", "pricing declin", "price decline", "price cut",
        "oversupply", "glut", "share loss", "losing share", "cuts forecast",
        "cut forecast", "reduces forecast", "reduce capex", "capex cut",
        "slowdown", "weak demand", "soft demand", "de-spec", "despec", "cancel",
    ],
    "dram_pricing": [
        "price decline", "prices fall", "prices fell", "price cut", "price drop",
        "oversupply", "glut", "bearish", "prices weaken", "soft pricing",
        "pricing pressure", "prices slide", "prices tumble",
    ],
}

CAPEX_GUIDANCE_STRONG = [
    "raises capex", "increases capex", "boosts spending", "raises capital expenditure",
    "increases capital expenditure forecast", "accelerat", "ramping up spending",
]
CAPEX_GUIDANCE_WEAK = [
    "cuts capex", "reduces capex", "pauses spending", "cuts capital expenditure",
    "slows spending", "pulls back on spending", "scales back",
]


def count_keyword_hits(text, keywords):
    text_l = text.lower()
    return sum(1 for kw in keywords if kw in text_l)


# --- Gross margin point bands ---
def gross_margin_level_score(margin_pct):
    if margin_pct is None:
        return None
    bands = [
        (60, 95), (50, 85), (45, 75), (40, 65), (35, 55), (30, 45), (25, 35),
    ]
    for threshold, score in bands:
        if margin_pct >= threshold:
            return score
    return 20


def gross_margin_change_adjustment(delta_pp, kind="sequential"):
    if delta_pp is None:
        return 0
    if kind == "sequential":
        bands = [(5, 20), (2, 10), (-2, 0), (-5, -10)]
        floor = -20
    else:  # yoy
        bands = [(15, 15), (5, 8), (-5, 0), (-15, -8)]
        floor = -15
    for threshold, adj in bands:
        if delta_pp >= threshold:
            return adj
    return floor


# --- Capex growth -> signal bands (-2..+2), averaged then scaled to 0-100 ---
def capex_growth_signal(yoy_growth_pct):
    if yoy_growth_pct is None:
        return None
    if yoy_growth_pct >= 40:
        return 2
    if yoy_growth_pct >= 15:
        return 1
    if yoy_growth_pct >= -5:
        return 0
    if yoy_growth_pct >= -25:
        return -1
    return -2


# --- Valuation bands ---
def forward_pe_band_score(forward_pe):
    if forward_pe is None or forward_pe <= 0:
        return None
    bands = [(8, 90), (12, 75), (16, 60), (22, 45), (30, 30)]
    for threshold, score in bands:
        if forward_pe < threshold:
            return score
    return 15


def distance_from_high_bonus(dist_from_high_pct):
    if dist_from_high_pct is None:
        return 0
    if dist_from_high_pct <= -30:
        return 15
    if dist_from_high_pct <= -20:
        return 10
    if dist_from_high_pct <= -10:
        return 5
    if dist_from_high_pct <= 0:
        return 0
    return -5


def clip(x, lo=0, hi=100):
    return max(lo, min(hi, x))
