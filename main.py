#!/usr/bin/env python3
import os, sys, time, threading, importlib
from typing import Dict, Any, List
from dotenv import load_dotenv

from core.config import get_config
from core.state import State
from core.bus import make_publisher
from core.notify import notify_pushover

load_dotenv()

def load_monitor(name: str):
    mod = importlib.import_module(f"monitors.{name}")
    # Each module must expose `Monitor` class
    return mod.Monitor

def run_monitor(MonitorCls, publish, cfg: Dict[str, Any], state: State):
    mon = MonitorCls(publish=publish, config=cfg, ctx={"state": state})
    mon.run()  # blocking loop

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
        t = threading.Thread(target=run_monitor, args=(MonitorCls, publish, cfg, state), daemon=True)
        t.start()
        threads.append(t)
        print(f"[main] started monitor: {name}")

    # Keep the main process alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[main] shutdown requested")

if __name__ == "__main__":
    main()
