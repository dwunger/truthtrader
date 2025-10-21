# core/bus.py
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any

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
    payload: Optional[Dict[str, Any]] = None  # e.g., {"text": "...", "analyze": True, "taco_mode": True, "pre_screened": True}

def make_publisher(cfg, state):
    analyzer = Analyzer(cfg, state.get("search_budget", default={}))

    def _send_async(title: str, message: str, priority: int) -> None:
        def _worker():
            try:
                # Priority 2 (emergency) for TACO trades: retry every 30s for 60 minutes
                # Note: Pushover minimum retry is 30s, so we use that instead of 10s
                kwargs = {
                    "title": title,
                    "message": message,
                    "priority": priority,
                    "token": cfg["PUSHOVER_TOKEN"],
                    "user": cfg["PUSHOVER_USER"],
                }
                
                if priority == 2:
                    kwargs["retry_interval"] = 30  # Pushover min is 30s (requested 10s but API requires 30s)
                    kwargs["expire"] = 3600  # 60 minutes
                
                notify_pushover(**kwargs)
            except Exception as e:
                print(f"[bus] notify error: {e}", flush=True)
        threading.Thread(target=_worker, name="pushover-send", daemon=True).start()

    def publish(evt: Event):
        print(
            f"[bus] → publish enter | src={evt.source} title={evt.title!r} prio={evt.priority} "
            f"analyze={evt.payload.get('analyze', True) if evt.payload else True}",
            flush=True,
        )

        payload = evt.payload or {}
        analyze_flag = payload.get("analyze", True)
        text = (payload.get("text") or "").strip()
        taco_mode = payload.get("taco_mode", False)
        pre_screened = payload.get("pre_screened", False)  # Already screened by Haiku
        decision = None

        try:
            # Optional analysis
            if analyze_flag and text:
                # Skip screening if already pre-screened by monitor
                if not pre_screened:
                    print(f"[bus] (pre_screened=False, analyzing without screening)", flush=True)
                
                decision = analyzer.analyze_post(
                    content=text,
                    url=evt.url or "",
                    created_at=evt.created_at or "",
                    taco_mode=taco_mode,
                    state=state if taco_mode else None  # Pass state for TACO context
                )
                
                # Priority handling
                if decision.get("priority") is not None:
                    evt.priority = max(evt.priority, decision["priority"])
                    print(f"[bus] Claude set priority={decision['priority']}", flush=True)
                elif decision.get("tickers"):
                    # Default: any ticker signal = priority 1
                    evt.priority = max(evt.priority, 1)
                    print(f"[bus] Tickers present, defaulting to priority=1", flush=True)
                
                # TACO mode validation: ensure actionable signals are emergency priority
                if taco_mode and decision.get("tickers"):
                    actions = [t.get("action", "") for t in decision.get("tickers", [])]
                    if any(action in ["BUY_PUTS", "BUY_CALLS"] for action in actions):
                        evt.priority = max(evt.priority, 2)  # Force emergency priority for options
                        print(f"[bus] TACO options signal detected, ensuring priority=2 (emergency)", flush=True)

            # Build final message
            final_message = evt.message
            if decision:
                summary = summarize_trade(decision)
                prefix = f"{evt.url}\n\n" if evt.url else ""
                final_message = prefix + summary

            # Async notify so we never block the monitor loop
            _send_async(evt.title, final_message, evt.priority)

            # Persist analyzer budget back to state **only if we analyzed**
            if analyze_flag and text:
                try:
                    state.set(analyzer.search_state, "search_budget")
                except Exception as se:
                    print(f"[bus] state.set error: {se}", flush=True)

        finally:
            print(f"[bus] ← publish exit  | src={evt.source} title={evt.title!r} prio={evt.priority}", flush=True)

    return publish