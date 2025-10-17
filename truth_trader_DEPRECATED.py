#!/usr/bin/env python3
"""
TruthTrader — Truth Social (Donald Trump) poller with selective web search, reasoning escalation,
and phone notifications (Pushover). Uses the truthbrush **Python API** (no CLI) so we can stop at 1 page.

Python 3.10+

SETUP (recap)
  python -m venv .venv && .venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install "git+https://github.com/stanfordio/truthbrush.git" openai httpx python-dotenv tenacity
  # add a .env next to this file (see template at bottom)
  python truth_trader.py
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

# ------------------------- Config -------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL", "gpt-4o-mini")
REASONING_MODEL = os.getenv("REASONING_MODEL", "gpt-4o")  # <-- real, available model id
REASONING_TRIGGER_CONF = float(os.getenv("REASONING_TRIGGER_CONF", "0.50"))

# truthbrush API is ALWAYS used in this script
HANDLE = os.getenv("TRUTH_HANDLE", "realDonaldTrump")
TB_USER = os.getenv("TRUTHSOCIAL_USERNAME")  # optional; truthbrush can read env itself
TB_PASS = os.getenv("TRUTHSOCIAL_PASSWORD")

PUSHOVER_USER = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_API_TOKEN")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "90"))
TICKER_WHITELIST = {t.strip().upper() for t in os.getenv("TICKER_WHITELIST", "").split(",") if t.strip()}

# web_search daily budget
MAX_SEARCH_CALLS_PER_DAY = int(os.getenv("MAX_SEARCH_CALLS_PER_DAY", "60"))

# Optional web_search options
SEARCH_FILTERS = [d.strip() for d in os.getenv("SEARCH_FILTERS", "").split(",") if d.strip()][:20]
LOC_COUNTRY = os.getenv("LOCATION_COUNTRY")
LOC_CITY = os.getenv("LOCATION_CITY")
LOC_REGION = os.getenv("LOCATION_REGION")
LOC_TZ = os.getenv("LOCATION_TZ")

STATE_FILE = os.getenv("STATE_FILE", ".truth_trader_state.json")

if not OPENAI_API_KEY:
    print("ERROR: OPENAI_API_KEY is required", file=sys.stderr)
    sys.exit(1)
if not (PUSHOVER_USER and PUSHOVER_TOKEN):
    print("ERROR: Pushover creds missing (PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN)", file=sys.stderr)
    sys.exit(1)

# ------------------------- OpenAI client -------------------------
from openai import OpenAI
from openai import BadRequestError  # for safe fallbacks

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------- truthbrush API (python) -------------------------
try:
    import truthbrush as tb  # exposes tb.Api
except Exception as e:
    print(f"ERROR: couldn't import truthbrush: {e}", file=sys.stderr)
    sys.exit(1)

# Construct the API client. Passing creds is optional if you already exported them.
TB = tb.Api()

# ------------------------- State -------------------------
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(data: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)

state = load_state()
last_seen_id: Optional[str] = state.get("last_seen_id")

def _roll_budget_if_new_day() -> None:
    st = state.get("search_budget") or {}
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if st.get("date") != today:
        st["date"] = today
        st["used"] = 0
        state["search_budget"] = st
        save_state(state)

def can_search() -> bool:
    _roll_budget_if_new_day()
    return (state.get("search_budget", {}).get("used", 0)) < MAX_SEARCH_CALLS_PER_DAY

def note_search_used() -> None:
    _roll_budget_if_new_day()
    st = state.get("search_budget") or {}
    st["used"] = int(st.get("used", 0)) + 1
    state["search_budget"] = st
    save_state(state)

# ------------------------- Notifications (Pushover) -------------------------
class RateLimit(Exception):
    pass

@retry(
    wait=wait_exponential_jitter(initial=1, exp_base=2, max=20),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type((httpx.HTTPError,))
)
def notify_pushover(title: str, message: str, priority: int = 0) -> None:
    with httpx.Client(timeout=15) as http:
        r = http.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title[:250],
                "message": message[:1024],
                "priority": int(priority),  # -2..2 per Pushover
            },
        )
        r.raise_for_status()


# ------------------------- Utilities -------------------------

def _strip_code_fences(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s

def _json_load_lenient(raw: str) -> dict:
    txt = _strip_code_fences(raw)
    try:
        return json.loads(txt)
    except Exception:
        # last resort: grab the first {...} block
        m = re.search(r"\{[\s\S]*\}", txt)
        if m:
            return json.loads(m.group(0))
        raise

# --- Debug logging ---
def log_post_preview(url: str, created_at: str, content: str) -> None:
    preview = (content or "").strip().replace("\n", " ")
    if len(preview) > 240:
        preview = preview[:240] + "…"
    print("[post]")
    print(f"  url: {url}")
    print(f"  created_at: {created_at}")
    print(f"  text: {preview or '(empty)'}")

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s) if ("<" in s and ">" in s) else s

def summarize_trade(decision: Dict[str, Any]) -> str:
    parts = [
        f"Sentiment: {decision.get('sentiment')} (conf {decision.get('confidence')})",
        decision.get("analysis", ""),
    ]
    tix = decision.get("tickers", []) or []
    if tix:
        parts.append("\nSignals:")
        for t in tix:
            parts.append(f"- {t.get('symbol','?')}: {t.get('action','HOLD')} — {t.get('rationale','')}")
    else:
        parts.append("\nNo trade suggested.")
    srcs = decision.get("sources", []) or []
    if srcs:
        parts.append("\nSources:")
        for s in srcs[:3]:
            parts.append(f"* {s.get('title','source')} — {s.get('url','')}")
    if decision.get("escalated"):
        parts.append("\n(Used reasoning model for final decision)")
    return "\n".join(p for p in parts if p)

# ------------------------- OpenAI analysis -------------------------
def _system_msg() -> str:
    return (
        "You are a cautious finance research assistant. Use the built-in web_search tool only "
        "when needed. Never guarantee profit; prefer large-cap liquid tickers. Provide concise, "
        "explainable rationales."
    )

def _web_search_tool_config() -> List[dict]:
    if not can_search():
        return []
    opts: Dict[str, Any] = {}
    if SEARCH_FILTERS:
        opts["filters"] = SEARCH_FILTERS
    loc = {}
    if LOC_COUNTRY: loc["country"] = LOC_COUNTRY
    if LOC_CITY:    loc["city"] = LOC_CITY
    if LOC_REGION:  loc["region"] = LOC_REGION
    if LOC_TZ:      loc["timezone"] = LOC_TZ
    if loc:
        opts["user_location"] = {"type": "approximate", "approximate": loc}
    return [{"type": "web_search", "web_search_options": opts}] if opts else [{"type": "web_search"}]

def _used_web_search_from_response(r) -> bool:
    try:
        anns = getattr(r.output[0], "message").annotations  # type: ignore
        if not anns:
            return False
        for a in anns:
            if getattr(a, "type", "") == "url_citation":
                return True
    except Exception:
        pass
    return False

def _responses_create_safe(model: str, **kwargs):
    """
    Call OpenAI Responses API. If the model doesn't exist, fall back in order.
    """
    try:
        return client.responses.create(model=model, **kwargs)
    except BadRequestError as e:
        if getattr(e, "status_code", None) == 400 and "does not exist" in str(e):
            fallbacks = [
                os.getenv("REASONING_FALLBACK_1", "gpt-4o"),
                os.getenv("REASONING_FALLBACK_2", "gpt-4.1-mini"),
                os.getenv("REASONING_FALLBACK_3", "gpt-4o-mini"),
            ]
            for fb in fallbacks:
                try:
                    print(f"[openai] fallback → model={fb}")
                    return client.responses.create(model=fb, **kwargs)
                except BadRequestError:
                    continue
        raise

def _shape_to_json(model: str, assistant_text: str, whitelist: Optional[List[str]]) -> Dict[str, Any]:
    note = f" Restrict to these tickers: {', '.join(whitelist)}" if whitelist else ""
    r = _responses_create_safe(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "Return ONLY valid JSON with keys: analysis, sentiment, confidence (0-1), "
                    "tickers (list of {symbol, action[BUY|SELL|HOLD], rationale}), needs_search (bool), "
                    "sources (list of {title,url}). If no trade, tickers=[]."
                ),
            },
            {"role": "assistant", "content": assistant_text + note},
        ],
    )
    raw = r.output_text
    try:
        data = _json_load_lenient(raw)
    except Exception:
        cleaned = _strip_code_fences(raw)
        data = {
            "analysis": cleaned[:500],  # keep it short if we must fall back
            "sentiment": "neutral",
            "confidence": 0.3,
            "tickers": [],
            "needs_search": False,
            "sources": [],
        }

    if whitelist:
        wl = set(t.upper() for t in whitelist)
        data["tickers"] = [t for t in data.get("tickers", []) if t.get("symbol", "").upper() in wl]
    return data

def analyze_post(content: str, url: str, created_at: str) -> Dict[str, Any]:
    # If the post has no textual content, avoid pointless analysis
    if not (content or "").strip():
        return {
            "analysis": "Media-only post (no text). No trade signal.",
            "sentiment": "neutral",
            "confidence": 0.4,
            "tickers": [],
            "needs_search": False,
            "sources": [],
        }

    tools = _web_search_tool_config()
    print(f"[openai] request 1 → model={MODEL} | tools={'web_search' if tools else 'none'} | url={url}")

    r1 = _responses_create_safe(
        model=MODEL,
        tools=tools,
        input=[
            {"role": "system", "content": _system_msg()},
            {
                "role": "user",
                "content": (
                    "Analyze this Truth Social post and decide if a trade is warranted.\n"
                    f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n\n"
                    "Return a short analysis. If you used web search, cite sources inline and list them."
                ),
            },
        ],
    )
    used1 = _used_web_search_from_response(r1)
    print(f"[openai] request 1 done | web_used={bool(used1)}")
    if used1:
        note_search_used()

    decision = _shape_to_json(MODEL, r1.output_text, sorted(TICKER_WHITELIST) if TICKER_WHITELIST else None)

    if decision.get("confidence", 0.0) < REASONING_TRIGGER_CONF:
        tools2 = _web_search_tool_config()
        print(f"[openai] request 2 (escalation) → model={REASONING_MODEL} | tools={'web_search' if tools2 else 'none'} | url={url}")

        r2 = _responses_create_safe(
            model=REASONING_MODEL,
            tools=tools2,
            input=[
                {"role": "system", "content": _system_msg()},
                {
                    "role": "user",
                    "content": (
                        "Re-analyze with deeper reasoning and refine the trade decision.\n"
                        f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n"
                        "Return a short analysis; cite sources if you browse."
                    ),
                },
            ],
        )
        used2 = _used_web_search_from_response(r2)
        print(f"[openai] request 2 done | web_used={bool(used2)}")
        if used2:
            note_search_used()

        decision2 = _shape_to_json(
            REASONING_MODEL, r2.output_text, sorted(TICKER_WHITELIST) if TICKER_WHITELIST else None
        )
        if decision2.get("confidence", 0.0) >= decision.get("confidence", 0.0):
            decision = decision2
            decision["escalated"] = True

    return decision

# ------------------------- Truthbrush fetchers -------------------------
def _is_cloudflare_html(payload: Any) -> bool:
    if isinstance(payload, str):
        return "<title>Access denied | truthsocial.com used Cloudflare" in payload or "Error 1015" in payload
    return False

@retry(
    wait=wait_exponential_jitter(initial=1, exp_base=2, max=30),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type((RateLimit, RuntimeError)),
)
def fetch_latest_statuses(since_id: Optional[str]) -> List[dict]:
    """
    Use the Python API and **only** walk the first page. Stop when we hit since_id.
    Returns newest->oldest for just-new posts.
    """
    new_posts: List[dict] = []
    try:
        page_iter = TB.pull_statuses(
            username=HANDLE,
            replies=False,
            verbose=False,
            created_after=None,
            since_id=since_id,   # truthbrush stops paginating when <= since_id on a page
            pinned=False,
        )
        # iterate the first page only; break after first non-new id
        for i, post in enumerate(page_iter):
            if _is_cloudflare_html(post):
                raise RateLimit("Cloudflare 1015 HTML encountered")

            pid = post.get("id")
            if pid is None:
                continue
            if since_id and pid <= since_id:
                break  # nothing newer on this page
            new_posts.append(post)

            if i > 25:
                break

        return new_posts
    except Exception as e:
        msg = str(e)
        if "429" in msg or "1015" in msg or "rate limited" in msg.lower():
            raise RateLimit(msg)
        if "Failed to decode JSON" in msg or "Access denied | truthsocial.com" in msg:
            raise RateLimit(msg)
        raise RuntimeError(msg)

# ------------------------- Analysis + Notify helpers -------------------------
def analyze_and_notify_single_post(st: dict) -> None:
    sid = st.get("id")
    content = strip_html(st.get("content") or st.get("text") or st.get("spoiler_text") or "").strip()
    url = st.get("url") or f"https://truthsocial.com/@{HANDLE}/{sid}"
    created_at = st.get("created_at") or st.get("createdAt") or ""

    log_post_preview(url, created_at, content)
    print("[openai] enqueue analysis → True")
    decision = analyze_post(content, url, created_at)
    print("[openai] analysis complete")

    msg = summarize_trade(decision)
    has_signal = bool(decision.get("tickers"))
    title = "TruthTrader — signal" if has_signal else "TruthTrader — no trade"
    try:
        notify_pushover(title, f"{url}\n\n{msg}", priority=1 if has_signal else 0)
        print(f"Notified for {url}")
    except Exception as e:
        print(f"[notify] failed: {e}", file=sys.stderr)


def poll_once(last_seen: Optional[str]) -> Tuple[Optional[str], List[Tuple[str, Dict[str, Any]]]]:
    decisions: List[Tuple[str, Dict[str, Any]]] = []
    statuses = fetch_latest_statuses(last_seen)

    newest_id = last_seen
    print(f"[fetch] got {len(statuses)} new post(s) since {last_seen or '(none)'}")

    # statuses are newest->oldest already; analyze in that order
    for st in statuses:
        sid = st.get("id")
        if not sid:
            continue
        if (last_seen is not None) and (sid <= last_seen):
            continue

        # One analysis per post
        analyze_and_notify_single_post(st)

        if (newest_id is None) or (sid > newest_id):
            newest_id = sid

    return newest_id, decisions  # decisions list is no longer used (notifications happen inline)

# ------------------------- Main -------------------------
def main():
    global last_seen_id
    print(
        f"Polling Truth Social (truthbrush API) for @{HANDLE} every {POLL_SECONDS}s | "
        f"model={MODEL} | reasoning={REASONING_MODEL} | whitelist={sorted(TICKER_WHITELIST) if TICKER_WHITELIST else 'ALL'}"
    )

    # Send a one-time startup notification
    try:
        notify_pushover(
            "TruthTrader — service started",
            (
                f"Watching @{HANDLE} every {POLL_SECONDS}s\n"
                f"Model: {MODEL}\nReasoning: {REASONING_MODEL}\n"
                f"Whitelist: {', '.join(sorted(TICKER_WHITELIST)) if TICKER_WHITELIST else 'ALL'}"
            )
        )
    except Exception as e:
        print(f"[startup] Pushover notify failed: {e}", file=sys.stderr)

    # Bootstrap: analyze the current newest post once, then set last_seen_id so subsequent cycles only handle new content.
    if not last_seen_id:
        try:
            first_page = fetch_latest_statuses(None)
            if first_page:
                latest = first_page[0]
                print(f"[bootstrap] analyzing newest {latest.get('id')}")
                analyze_and_notify_single_post(latest)
                last_seen_id = latest["id"]
                state["last_seen_id"] = last_seen_id
                save_state(state)
        except RateLimit:
            cooldown = 300
            print(f"[bootstrap] Rate limited (429/1015). Cooling down {cooldown}s...")
            time.sleep(cooldown)

    while True:
        try:
            new_last, _ = poll_once(last_seen_id)
            if new_last and new_last != last_seen_id:
                last_seen_id = new_last
                state["last_seen_id"] = last_seen_id
                save_state(state)
        except RateLimit as e:
            cooldown = random.randint(240, 480)  # 4–8 minutes
            print(f"[poll] Rate limited: {e}. Cooling down {cooldown}s...")
            time.sleep(cooldown)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)

        # jitter to avoid same-second polling patterns
        sleep_s = POLL_SECONDS + random.uniform(-5, 5)
        time.sleep(max(30, sleep_s))  # never less than 30s to be kind to the origin

if __name__ == "__main__":
    main()
