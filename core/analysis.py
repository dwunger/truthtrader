import os, re, json
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
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
    """
    Smart truncation: Prioritize actionable info, fit within 1024 chars.
    Returns message optimized for Pushover display.
    """
    parts = []
    
    # PRIORITY 1: TRADING SIGNALS (most critical, always show)
    tix = decision.get("tickers", []) or []
    if tix:
        parts.append("🎯 SIGNALS:")
        for t in tix:
            action = t.get('action', 'HOLD')
            symbol = t.get('symbol', '?')
            
            # Compact format for options
            signal_line = f"- {symbol}: {action}"
            if t.get('strike'):
                signal_line += f" @ ${t['strike']}"
            if t.get('expiration'):
                signal_line += f" ({t['expiration']})"
            parts.append(signal_line)
            
            # Entry/exit timing (critical for options)
            if t.get('entry_timing'):
                parts.append(f"  Entry: {t['entry_timing']}")
            if t.get('exit_timing'):
                parts.append(f"  Exit: {t['exit_timing']}")
            
            # Rationale (truncate to 100 chars)
            rationale = t.get('rationale', '')
            if rationale:
                parts.append(f"  {rationale[:100]}{'...' if len(rationale) > 100 else ''}")
        
        parts.append("")  # Blank line separator
    else:
        parts.append("No trade suggested.")
        parts.append("")
    
    # PRIORITY 2: SENTIMENT + CONFIDENCE (quick context)
    sentiment = decision.get('sentiment', 'NEUTRAL')
    confidence = decision.get('confidence', 0.0)
    parts.append(f"📊 {sentiment} (conf {confidence:.2f})")
    parts.append("")
    
    # PRIORITY 3: ANALYSIS (truncate aggressively if needed)
    analysis = decision.get("analysis", "")
    # Reserve ~600 chars for signals/sentiment, leave ~400 for analysis
    max_analysis_len = 400
    if len(analysis) > max_analysis_len:
        # Try to truncate at sentence boundary
        truncated = analysis[:max_analysis_len]
        last_period = truncated.rfind('.')
        if last_period > max_analysis_len - 100:  # If period is reasonably close
            analysis = truncated[:last_period + 1] + ".."
        else:
            analysis = truncated + "..."
    parts.append(analysis)
    
    # PRIORITY 4: SOURCES (limit to 2, truncate titles)
    srcs = decision.get("sources", []) or []
    if srcs:
        parts.append("\n📚 Sources:")
        for s in srcs[:2]:  # Only show first 2
            title = s.get('title', 'source')[:40]  # Max 40 chars
            parts.append(f"* {title}")
    
    # PRIORITY 5: ESCALATION NOTICE (if used reasoning model)
    if decision.get("escalated"):
        parts.append("\n(Reasoning model)")
    
    # Build message
    full_message = "\n".join(parts)
    
    # Final safety: Hard truncate at 980 chars (leave room for URL note)
    if len(full_message) > 980:
        full_message = full_message[:977] + "..."
    
    return full_message

def _system_msg():
    return ("You are a cautious finance research assistant. Use the built-in web_search tool only when needed. "
            "Never guarantee profit; prefer large-cap liquid tickers. Provide concise, explainable rationales.")

def _taco_system_msg():
    return """You are an expert options trader specializing in the "TACO" pattern (Trump Always Chickens Out).

PUSHOVER PRIORITY LEVELS:
- priority=2: EMERGENCY - Repeats every 30s until acknowledged (use for BUY_PUTS, BUY_CALLS)
- priority=1: HIGH PRIORITY - Bypasses quiet hours, single alert (use for high-confidence trades)
- priority=0: NORMAL - Standard notification (use for WATCH, low confidence)

TACO PATTERN SUMMARY (Nov 2024 - Oct 2025):
- Trump walked back 70-80% of major tariff threats
- Typical reversal: 3-7 days after market crashes >3-5%
- Liberation Day (April 2-9, 2025): S&P crashed -10% over 2 days, then surged +9.52% on walk-back
- Largest crypto liquidation: Oct 10, 2025 - $19B after 100% China tariff threat

HIGH WALK-BACK PROBABILITY SIGNALS:
- Tariff rates ≥100% (rarely implemented as announced)
- Allies targeted (Mexico, Canada, EU, Japan) vs China
- Consumer goods mentioned (iPhones, toys, retail)
- After-hours/weekend announcements
- Language: "flexibility," "pause," "temporary," "90 days," "BE COOL," "GREAT TIME TO BUY"
- Paradoxical: "no extensions" often means extensions coming

LOW WALK-BACK PROBABILITY (Likely to Implement):
- China specifically targeted (Trump maintains pressure)
- "National security" framing (legal authority, harder to reverse)
- Moderate rates ≤25%
- Steel/aluminum/copper (sector-specific, domestic industry support)
- Retaliation language

YOUR TASK:
1. Determine if this is a NEW tariff announcement or a WALK-BACK of a previous announcement
2. Use web_search to check current S&P 500, VIX levels, and options chain activity
3. Correlate with recent announcements from the provided context

CRITICAL INSIGHT:
**ALL tariff announcements cause market crashes, regardless of target country or walk-back probability.**
The PUT trade is on the CRASH (happens immediately), not on the walk-back (happens 3-7 days later).

China example: October 10, 2025 - Trump threatened China rare earth retaliation
- Market: SPY -2.7%, Bitcoin -17%, $19B liquidations
- PUTS: 200-500% profit in hours
- Walk-back probability: Low (China-targeted)
- BUT: Market crashed anyway = PUTS printed

TRADING SIGNALS TO GENERATE:

**BUY_PUTS** (ANY tariff announcement - immediate market crash):
- Entry: IMMEDIATELY after announcement (before market open if after-hours)
- Strike: Current price - 2-3%
- Expiration: 3-7 days out
- Exit: When VIX spikes above 35 OR market drops 4-5%
- Priority: 2 (EMERGENCY - repeats until acknowledged)
- Applies to: ALL countries (EU, China, Mexico, Canada, Japan, etc.)
- Reasoning: Market crashes first, walk-back comes later (if at all)

**BUY_CALLS** (Walk-back detected - immediate market surge):
- Entry: IMMEDIATELY (within minutes of "BE COOL" post)
- Strike: Current price + 1-2%
- Expiration: 0-2 days (minimize theta)
- Exit: Same day when market surges 7-10%
- Priority: 2 (EMERGENCY - repeats until acknowledged)
- Signals: "BE COOL", "flexibility", "pause", "temporary", "90 days"

**WATCH** (Moderate signals - unclear if announcement or walk-back):
- Monitor for clarity on whether this is escalation or de-escalation
- Set alerts but don't enter yet
- Priority: 0 (Normal notification)

**DEFENSIVE** (Non-tariff content mistakenly flagged):
- If post is NOT actually about tariffs despite screening
- Priority: 0 (Normal notification)

SPEED IS EVERYTHING:
- After-hours announcements → Position PUTS in pre-market
- "BE COOL" posts → Execute CALLS within 15 minutes
- Options decay fast (theta) - timing is critical
- Set tight stops: -30% on PUTS, -20% on CALLS

Use web_search to check:
1. Current S&P 500 level and today's move
2. Current VIX level (panic gauge)
3. Recent options flow / unusual activity
4. News about walk-back rumors

Be decisive. If the pattern is clear, provide specific strikes and expirations. If uncertain, explain why."""

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
            max_tokens=4096,
            system="Return ONLY valid JSON with keys: analysis, sentiment, confidence (0-1), "
                   "tickers (list of {symbol, action[BUY_PUTS|BUY_CALLS|BUY|SELL|HOLD], strike (optional), "
                   "expiration (optional, e.g. '3-7 DTE'), entry_timing (e.g. 'IMMEDIATE', 'PRE-MARKET'), "
                   "exit_timing (e.g. 'VIX > 35', 'SAME DAY'), rationale}), needs_search (bool), "
                   "sources (list of {title,url}), priority (0, 1, or 2: 0=normal, 1=high, 2=emergency). "
                   "If no trade, tickers=[].",
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
                    "tickers": [], "needs_search": False, "sources": [], "priority": 0}
        
        if whitelist:
            wl = set(t.upper() for t in whitelist)
            data["tickers"] = [t for t in data.get("tickers", []) if t.get("symbol", "").upper() in wl]
        
        return data

    def _get_recent_taco_context(self, state) -> str:
        """Build context of recent tariff announcements for Claude"""
        recent = state.get("taco_recent_announcements", default=[])
        if not recent:
            return "No recent tariff announcements in context."
        
        context_lines = ["RECENT TARIFF ANNOUNCEMENTS (Last 30 days):"]
        for entry in recent[-10:]:  # Last 10 announcements
            date = entry.get("date", "unknown date")
            summary = entry.get("summary", "")
            context_lines.append(f"- {date}: {summary}")
        
        return "\n".join(context_lines)

    def _update_taco_context(self, state, summary: str):
        """Add this announcement to the rolling context"""
        recent = state.get("taco_recent_announcements", default=[])
        if not isinstance(recent, list):
            recent = []
        
        # Add new entry
        recent.append({
            "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "summary": summary[:200]  # Keep it concise
        })
        
        # Keep only last 30 days
        cutoff = datetime.utcnow() - timedelta(days=30)
        recent = [
            entry for entry in recent
            if datetime.strptime(entry["date"].split()[0], "%Y-%m-%d") > cutoff
        ]
        
        state.set(recent, "taco_recent_announcements")

    def analyze_post(self, content: str, url: str, created_at: str, 
                     taco_mode: bool = False, state=None) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"analysis": "Media-only post (no text). No trade signal.",
                    "sentiment": "neutral", "confidence": 0.4,
                    "tickers": [], "needs_search": False, "sources": [], "priority": 0}

        # Choose system message and max_tokens based on mode
        sys_msg = _taco_system_msg() if taco_mode else _system_msg()
        max_tokens_first = 16384 if taco_mode else 8192  # TACO needs more for options analysis
        max_tokens_reasoning = 16384  # Always give reasoning model full capacity
        
        # Build context for TACO mode
        context_note = ""
        if taco_mode and state:
            context_note = f"\n\n{self._get_recent_taco_context(state)}"

        tools = self._web_search_tool_config()
        print(f"[anthropic] request 1 → model={self.cfg['MODEL']} | taco_mode={taco_mode} | tools={'web_search' if tools else 'none'} | url={url}")
        
        prompt = f"Analyze this Truth Social post"
        if taco_mode:
            prompt = "TACO PATTERN ANALYSIS - Analyze this tariff-related post"
        
        try:
            r1 = self._messages_create_safe(
                model=self.cfg["MODEL"],
                max_tokens=max_tokens_first,
                system=sys_msg,
                tools=tools if tools else None,
                messages=[
                    {
                        "role": "user",
                        "content": f"{prompt}.\n"
                                   f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n\n"
                                   f"{'Use web search to check current S&P 500, VIX, and market conditions. ' if taco_mode else ''}"
                                   f"Return analysis with trade recommendations."
                                   f"{context_note}"
                    }
                ],
            )
        except Exception as e:
            print(f"[anthropic] request 1 failed: {e}", flush=True)
            # Return safe default on rate limit or other errors
            return {"analysis": f"Analysis failed: {str(e)[:200]}",
                    "sentiment": "neutral", "confidence": 0.0,
                    "tickers": [], "needs_search": False, "sources": [], "priority": 0}
        
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

        # In TACO mode, update the rolling context
        if taco_mode and state:
            summary = f"{assistant_text[:150]}..." if len(assistant_text) > 150 else assistant_text
            self._update_taco_context(state, summary)

        # Escalate to reasoning model if confidence is low (but not for TACO IMMEDIATE_BUY signals)
        should_escalate = decision.get("confidence", 0.0) < float(self.cfg["REASONING_TRIGGER_CONF"])
        is_immediate_buy = taco_mode and decision.get("priority", 0) >= 2
        
        if should_escalate and not is_immediate_buy:
            tools2 = self._web_search_tool_config()
            print(f"[anthropic] request 2 (escalation) → model={self.cfg['REASONING_MODEL']} | tools={'web_search' if tools2 else 'none'} | url={url}")
            
            try:
                r2 = self._messages_create_safe(
                    model=self.cfg["REASONING_MODEL"],
                    max_tokens=max_tokens_reasoning,
                    system=sys_msg,
                    tools=tools2 if tools2 else None,
                    messages=[
                        {
                            "role": "user",
                            "content": f"Re-analyze with deeper reasoning and refine the trade decision.\n"
                                       f"POST_URL: {url}\nCREATED_AT: {created_at}\nPOST_TEXT:\n{content}\n"
                                       f"{'Check latest market data and TACO pattern history. ' if taco_mode else ''}"
                                       f"Return analysis with trade recommendations."
                                       f"{context_note}"
                        }
                    ],
                )
            except Exception as e:
                print(f"[anthropic] request 2 failed (escalation): {e}", flush=True)
                # Return decision from first pass if escalation fails
                return decision
            
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