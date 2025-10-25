# core/bus.py
import threading
import time
from queue import Queue, Full
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
    payload: Optional[Dict[str, Any]] = None


def make_publisher(cfg, state):
    analyzer = Analyzer(cfg, state.get("search_budget", default={}))
    analysis_q: Queue[Event] = Queue(maxsize=100)

    # -------------------------
    # notification helper
    # -------------------------
    def _send_async(title: str, message: str, priority: int, url: str = None) -> None:
        def _worker():
            try:
                kwargs = {
                    "title": title,
                    "message": message,
                    "priority": priority,
                    "token": cfg["PUSHOVER_TOKEN"],
                    "user": cfg["PUSHOVER_USER"],
                }
                if url:
                    kwargs["url"] = url
                    kwargs["url_title"] = "View Full Post"
                if priority == 2:
                    kwargs["retry_interval"] = 30
                    kwargs["expire"] = 3600
                notify_pushover(**kwargs)
            except Exception as e:
                print(f"[bus] notify error: {e}", flush=True)

        threading.Thread(target=_worker, name="pushover-send", daemon=True).start()

    # -------------------------
    # quick-signal builder
    # -------------------------
    def _build_quick_signal(decision: Dict[str, Any]) -> str:
        parts = []
        for t in decision.get("tickers", []):
            sym, act = t.get("symbol", "?"), t.get("action", "HOLD")
            line = f"{sym}: {act}"
            if t.get("strike"): line += f" @ ${t['strike']}"
            if t.get("expiration"): line += f" ({t['expiration']})"
            parts.append(line)
        return "\n".join(parts) or "Signal generated — check details notification"

    # -------------------------
    # worker: does heavy lifting
    # -------------------------
    def _process(evt: Event):
        payload = evt.payload or {}
        analyze_flag = payload.get("analyze", True)
        text = (payload.get("text") or "").strip()
        taco_mode = payload.get("taco_mode", False)
        pre_screened = payload.get("pre_screened", False)
        decision = None

        try:
            if analyze_flag and text:
                if not pre_screened:
                    print(f"[bus] analyzing unscreened post...", flush=True)
                t0 = time.time()
                decision = analyzer.analyze_post(
                    content=text,
                    url=evt.url or "",
                    created_at=evt.created_at or "",
                    taco_mode=taco_mode,
                    state=state if taco_mode else None,
                )
                dt = time.time() - t0
                print(f"[bus] analysis done in {dt:.1f}s", flush=True)

                # Priority adjustment
                if decision.get("priority") is not None:
                    evt.priority = max(evt.priority, decision["priority"])
                elif decision.get("tickers"):
                    evt.priority = max(evt.priority, 1)

                # TACO options emergency
                if taco_mode and any(
                    t.get("action") in ["BUY_PUTS", "BUY_CALLS"]
                    for t in decision.get("tickers", [])
                ):
                    evt.priority = max(evt.priority, 2)

            # Build message
            final_message = evt.message
            if decision:
                summary = summarize_trade(decision)
                prefix = f"{evt.url}\n\n" if evt.url else ""
                final_message = prefix + summary

            # Dispatch notifications
            if taco_mode and evt.priority >= 2 and decision and decision.get("tickers"):
                quick_signal = _build_quick_signal(decision)
                _send_async("🚨 TACO EMERGENCY", quick_signal, 2, evt.url)
                time.sleep(2)
                _send_async("TACO Analysis — Details", final_message, 0, evt.url)
            else:
                _send_async(evt.title, final_message, evt.priority, evt.url)

            # Persist analyzer state
            if analyze_flag and text:
                state.set(analyzer.search_state, "search_budget")

        except Exception as e:
            print(f"[bus] error processing event {evt.source}: {e}", flush=True)

        finally:
            print(f"[bus] ← finished | src={evt.source} prio={evt.priority}", flush=True)

    # -------------------------
    # worker thread (background)
    # -------------------------
    def _worker_loop():
        while True:
            evt = analysis_q.get()
            _process(evt)
            analysis_q.task_done()

    threading.Thread(target=_worker_loop, daemon=True, name="bus-analyzer").start()

    # -------------------------
    # public entry point
    # -------------------------
    def publish(evt: Event):
        try:
            analysis_q.put_nowait(evt)
            print(f"[bus] queued | src={evt.source} title={evt.title!r} prio={evt.priority}", flush=True)
        except Full:
            print("[bus] WARNING: analysis queue full — dropping event!", flush=True)

    return publish
