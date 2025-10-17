from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable

from core.notify import notify_pushover
from core.analysis import Analyzer, summarize_trade

@dataclass
class Event:
    source: str
    title: str
    message: str
    url: Optional[str] = None
    priority: int = 0
    created_at: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None  # e.g., {"text": "...", "analyze": True}

def make_publisher(cfg, state):
    # Analyzer keeps its own tiny search budget (namespaced in state)
    analyzer = Analyzer(cfg, state.get("search_budget", default={}))
    def publish(evt: Event):
        # Optional cross-cutting enrichment
        payload = evt.payload or {}
        analyze_flag = payload.get("analyze", True)
        text = (payload.get("text") or "").strip()
        decision = None

        if analyze_flag and text:
            decision = analyzer.analyze_post(
                content=text,
                url=evt.url or "",
                created_at=evt.created_at or ""
            )
            # Priority bump if any tickers were suggested
            if decision.get("tickers"):
                evt.priority = max(evt.priority, 1)

        # Build final message
        final_message = evt.message
        if decision:
            summary = summarize_trade(decision)
            prefix = f"{evt.url}\n\n" if evt.url else ""
            final_message = prefix + summary

        # Push
        notify_pushover(
            title=evt.title,
            message=final_message,
            priority=evt.priority,
            token=cfg["PUSHOVER_TOKEN"],
            user=cfg["PUSHOVER_USER"],
        )
        # Persist analyzer budget back to state
        state.set(analyzer.search_state, "search_budget")
    return publish
