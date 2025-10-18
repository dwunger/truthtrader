#!/usr/bin/env python3
import os, sys, time, threading, importlib, traceback
from typing import Dict, Any, List
from dotenv import load_dotenv

from core.config import get_config
from core.state import State
from core.bus import make_publisher
from core.notify import notify_pushover

load_dotenv()

def _thread_excepthook(args):
    print(f"[thread:{getattr(args, 'thread', None)}] unhandled exception: {args.exc_type.__name__}: {args.exc_value}", flush=True)
    traceback.print_tb(args.exc_traceback)

threading.excepthook = _thread_excepthook

def load_monitor(name: str):
    mod = importlib.import_module(f"monitors.{name}")
    # Each module must expose `Monitor` class
    return mod.Monitor

def run_monitor_loop(MonitorCls, publish, cfg: Dict[str, Any], state: State, name: str):
    """
    Keeps a monitor alive: if it raises, log and restart after a short backoff.
    """
    backoff = 5
    while True:
        try:
            mon = MonitorCls(publish=publish, config=cfg, ctx={"state": state})
            mon.run()  # blocking loop
        except Exception as e:
            print(f"[runner:{name}] crashed: {e}", flush=True)
            traceback.print_exc()
            backoff = min(120, backoff * 2)
            for i in range(backoff, 0, -5):
                print(f"[runner:{name}] restart in {i}s …", flush=True)
                time.sleep(min(5, i))
            continue

def main():
    cfg = get_config()
    state = State(path=cfg["STATE_FILE"])
    publish = make_publisher(cfg=cfg, state=state)

    enabled = [s.strip() for s in os.getenv("ENABLED_MONITORS", "truth_social,example").split(",") if s.strip()]
    # Friendly names for the startup notification
    pretty = {
        "truth_social": "Truth Social (@{})".format(cfg["TRUTH_HANDLE"]),
        "example": "Example Monitor",
    }
    names_list = ", ".join(pretty.get(n, n) for n in enabled)

    # One-time startup ping
    try:
        notify_pushover(
            title="TruthTrader — service started",
            message=f"Monitors: {names_list}\nModel: {cfg['MODEL']}\nReasoning: {cfg['REASONING_MODEL']}",
            priority=0
        )
    except Exception as e:
        print(f"[startup] Pushover notify failed: {e}", file=sys.stderr)

    threads: List[threading.Thread] = []
    for name in enabled:
        try:
            MonitorCls = load_monitor(name)
        except Exception as e:
            print(f"[main] Failed to load monitor '{name}': {e}", file=sys.stderr)
            continue
        t = threading.Thread(
            target=run_monitor_loop,
            args=(MonitorCls, publish, cfg, state, name),
            daemon=True,
            name=f"monitor:{name}"
        )
        t.start()
        threads.append(t)
        print(f"[main] started monitor: {name}", flush=True)

    # Watchdog prints liveness
    def _watchdog():
        while True:
            for t in threads:
                print(f"[watchdog] {t.name} alive={t.is_alive()}", flush=True)
            time.sleep(15)

    wd = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
    wd.start()

    # Keep the main process alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[main] shutdown requested", flush=True)

if __name__ == "__main__":
    main()
