import os, re, json
from typing import Any, Dict, List, Optional
from anthropic import Anthropic, BadRequestError

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s) if s and "<" in s and ">" in s else (s or "")

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
        m = re.search(r"\{[\s\S]*\}", txt)
        if m:
            return json.loads(m.group(0))
        raise

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

def _system_msg():
    return ("You are a cautious finance research assistant. Use the built-in web_search tool only when needed. "
            "Never guarantee profit; prefer large-cap liquid tickers. Provide concise, explainable rationales.")

def _used_web_search_from_response(response) -> bool:
    """Check if web search was used by looking for server_tool_use in content blocks"""
    try:
        for block in response.content:
            if hasattr(block, 'type') and block.type == 'server_tool_use':
                if block.name == 'web_search':
                    return True
    except Exception:
        pass
    return False

def _extract_text_from_response(response) -> str:
    """Extract all text content from Claude's response"""
    text_parts = []
    for block in response.content:
        if hasattr(block, 'type') and block.type == 'text':
            text_parts.append(block.text)
    return "\n".join(text_parts)

class Analyzer:
    def __init__(self, cfg, search_budget_state: dict):
        self.cfg = cfg
        self.client = Anthropic(api_key=cfg["ANTHROPIC_API_KEY"])
        self.search_state = search_budget_state or {}

    def _can_search(self) -> bool:
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.search_state.get("date") != today:
            self.search_state["date"] = today
            self.search_state["used"] = 0
        return int(self.search_state.get("used", 0)) < int(self.cfg["MAX_SEARCH_PER_DAY"])

    def _note_search(self):
        self.search_state["used"] = int(self.search_state.get("used", 0)) + 1

    def _web_search_tool_config(self) -> List[dict]:
        if not self._can_search():
            return []
        
        tool_config: Dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5
        }
        
        # Add domain filters if configured
        filters = self.cfg.get("SEARCH_FILTERS") or []
        if filters:
            tool_config["allowed_domains"] = filters
        
        # Add location if configured
        loc = self.cfg.get("LOCATION") or {}
        loc = {k: v for k, v in loc.items() if v}
        if loc and all(k in loc for k in ["city", "region", "country", "timezone"]):
            tool_config["user_location"] = {
                "type": "approximate",
                "city": loc["city"],
                "region": loc["region"],
                "country": loc["country"],
                "timezone": loc["timezone"]
            }
        
        return [tool_config]

    def _messages_create_safe(self, model: str, **kwargs):
        try:
            return self.client.messages.create(model=model, **kwargs)
        except BadRequestError as e:
            if "does not exist" in str(e).lower() or "model" in str(e).lower():
                for fb in self.cfg["REASONING_FALLBACKS"]:
                    try:
                        print(f"[anthropic] fallback → model={fb}")
                        return self.client.messages.create(model=fb, **kwargs)
                    except BadRequestError:
                        continue
            raise

    def _shape_to_json(self, model: str, assistant_text: str, whitelist: Optional[List[str]]) -> Dict[str, Any]:
        note = f" Restrict to these tickers: {', '.join(whitelist)}" if whitelist else ""
        
        r = self._messages_create_safe(
            model=model,
            max_tokens=2000,
            system="Return ONLY valid JSON with keys: analysis, sentiment, confidence (0-1), "
                   "tickers (list of {symbol, action[BUY|SELL|HOLD], rationale}), needs_search (bool), "
                   "sources (list of {title,url}). If no trade, tickers=[].",
            messages=[
                {
                    "role": "user",
                    "content": f"Convert this analysis to the required JSON format:\n\n{assistant_text}{note}"
                }
            ],
        )
        
        raw = _extract_text_from_response(r)
        try:
            data = _json_load_lenient(raw)
        except Exception:
            cleaned = _strip_code_fences(raw)
            data = {"analysis": cleaned[:500], "sentiment": "neutral", "confidence": 0.3,
                    "tickers": [], "needs_search": False, "sources": []}
        
        if whitelist:
            wl = set(t.upper() for t in whitelist)
            data["tickers"] = [t for t in data.get("tickers", []) if t.get("symbol", "").upper() in wl]
        
        return data

    def analyze_post(self, content: str, url: str, created_at: str) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"analysis": "Media-only post (no text). No trade signal.",
                    "sentiment": "neutral", "confidence": 0.4,
                    "tickers": [], "needs_search": False, "sources": []}

        tools = self._web_search_tool_config()
        print(f"[anthropic] request 1 → model={self.cfg['MODEL']} | tools={'web_search' if tools else 'none'} | url={url}")
        
        r1 = self._messages_create_safe(
            model=self.cfg["MODEL"],
            max_tokens=16384,
            system=_system_msg(),
            tools=tools if tools else None,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyze this Truth Social post and decide if a trade is warranted.\n"
                               f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n\n"
                               "Return a short analysis. If you used web search, cite sources inline and list them."
                }
            ],
        )
        
        used1 = _used_web_search_from_response(r1)
        print(f"[anthropic] request 1 done | web_used={bool(used1)}")
        if used1:
            self._note_search()

        assistant_text = _extract_text_from_response(r1)
        decision = self._shape_to_json(
            self.cfg["MODEL"], 
            assistant_text,
            sorted(self.cfg["TICKER_WHITELIST"]) if self.cfg["TICKER_WHITELIST"] else None
        )

        # Escalate to reasoning model if confidence is low
        if decision.get("confidence", 0.0) < float(self.cfg["REASONING_TRIGGER_CONF"]):
            tools2 = self._web_search_tool_config()
            print(f"[anthropic] request 2 (escalation) → model={self.cfg['REASONING_MODEL']} | tools={'web_search' if tools2 else 'none'} | url={url}")
            
            r2 = self._messages_create_safe(
                model=self.cfg["REASONING_MODEL"],
                max_tokens=16384,
                system=_system_msg(),
                tools=tools2 if tools2 else None,
                messages=[
                    {
                        "role": "user",
                        "content": f"Re-analyze with deeper reasoning and refine the trade decision.\n"
                                   f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n"
                                   "Return a short analysis; cite sources if you browse."
                    }
                ],
            )
            
            used2 = _used_web_search_from_response(r2)
            print(f"[anthropic] request 2 done | web_used={bool(used2)}")
            if used2:
                self._note_search()

            assistant_text2 = _extract_text_from_response(r2)
            decision2 = self._shape_to_json(
                self.cfg["REASONING_MODEL"], 
                assistant_text2,
                sorted(self.cfg["TICKER_WHITELIST"]) if self.cfg["TICKER_WHITELIST"] else None
            )
            
            if decision2.get("confidence", 0.0) >= decision.get("confidence", 0.0):
                decision = decision2
                decision["escalated"] = True

        return decision
