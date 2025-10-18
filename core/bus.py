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
    payload: Optional[Dict[str, Any]] = None  # e.g., {"text": "...", "analyze": True}

def make_publisher(cfg, state):
    analyzer = Analyzer(cfg, state.get("search_budget", default={}))

    def _send_async(title: str, message: str, priority: int) -> None:
        def _worker():
            try:
                notify_pushover(
                    title=title,
                    message=message,
                    priority=priority,
                    token=cfg["PUSHOVER_TOKEN"],
                    user=cfg["PUSHOVER_USER"],
                )
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
        decision = None

        try:
            # Optional analysis
            if analyze_flag and text:
                decision = analyzer.analyze_post(
                    content=text,
                    url=evt.url or "",
                    created_at=evt.created_at or ""
                )
                if decision.get("tickers"):
                    evt.priority = max(evt.priority, 1)

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
