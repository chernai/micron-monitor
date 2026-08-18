"""AI chat: lets the user ask free-form questions about the current
technical/fundamental picture. Unlike everything else in this app, this is
NOT deterministic -- it's the one place an LLM generates the actual words,
given a compact, factual summary of what's already been computed elsewhere
(scores, levels, the Technical Analysis narrative). It never writes back to
the database and never influences any score or signal.
"""
import os

import streamlit as st

try:
    import anthropic
except ImportError:
    anthropic = None

CHAT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 700
MAX_HISTORY_MESSAGES = 12  # keep request size/latency/cost bounded

SYSTEM_PROMPT_TEMPLATE = """You are the chat assistant embedded in "Micron Monitor," a dashboard for \
{ticker}. Answer using ONLY the data summary below -- don't invent numbers that aren't in it, and say so \
if something you'd need isn't there. This dashboard is fundamentals-first (HBM demand, DRAM pricing, \
gross margins, customer capex) with a rule-based Technical Timing Score and a rule-based chart narrative \
as secondary, entry-timing inputs -- keep that framing in mind (fundamentals decide the thesis, technicals \
decide the timing). You may state a view and reference generic options-strategy shapes (e.g. bear call \
spread, protective put, covered call, iron condor) to help express an opinion the user states, using the \
price levels given -- but always be clear this is educational, not personalized financial advice, and that \
you have no live options-chain data (no strikes, premiums, greeks, or expirations). Be concise and direct.

Current data summary:
{context}
"""


def _get_api_key():
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


def build_context_summary(ticker, overall, narrative):
    lines = [
        f"Ticker: {ticker}",
        f"As of: {overall.get('as_of_date')}",
        f"Signal: {overall.get('signal')} (confidence {overall.get('confidence')})",
        f"Fundamental Score: {overall.get('fundamental_score')}",
        f"Valuation Score: {overall.get('valuation_score')}",
        f"Technical Timing Score: {overall.get('technical_score')}",
    ]
    if narrative and narrative.get("available"):
        lines.append(f"Current price: ${narrative['current_price']:.2f}")
        lines.append(f"Technical Analysis outlook: {narrative['forecast']['outlook']} -- {narrative['forecast']['text']}")
        for heading, text in narrative["sections"]:
            if heading == "Outlook":
                continue
            lines.append(f"{heading}: {text}")
        if narrative.get("option_ideas"):
            lines.append("Rule-based options ideas already shown to the user: " + " ".join(narrative["option_ideas"]))
    else:
        lines.append("Technical Analysis hasn't been run yet this session (no chart narrative available).")
    return "\n".join(lines)


def ask(ticker, overall, narrative, conversation_history):
    if anthropic is None:
        return ("The `anthropic` package isn't installed in this environment -- add it to "
                "requirements.txt and reinstall.")
    api_key = _get_api_key()
    if not api_key:
        return ("No `ANTHROPIC_API_KEY` configured -- add one to `.streamlit/secrets.toml` (or this app's "
                "secrets on Streamlit Cloud) and to your local `.env` to enable the chat.")

    client = anthropic.Anthropic(api_key=api_key)
    system = SYSTEM_PROMPT_TEMPLATE.format(ticker=ticker, context=build_context_summary(ticker, overall, narrative))
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in conversation_history[-MAX_HISTORY_MESSAGES:]
    ]
    try:
        resp = client.messages.create(model=CHAT_MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages)
        return "".join(block.text for block in resp.content if block.type == "text") or "(empty response)"
    except Exception as e:
        return f"Chat request failed: {e}"
