import time
from typing import Optional
from core.bus import Event, publish

FRIENDLY_NAME = "Example monitor"

def run(poll_seconds: int = 300, **kwargs) -> None:
    # Announce start
    publish(Event(
        source="example",
        title="Example monitor started",
        url=None,
        created_at=None,
        text="The example monitor is running.",
        request_analysis=False,   # no OpenAI for this demo
        priority=0
    ))
    # Heartbeat loop
    while True:
        time.sleep(poll_seconds)
        publish(Event(
            source="example",
            title="Example heartbeat",
            url=None,
            created_at=None,
            text=f"Still alive (every {poll_seconds}s).",
            request_analysis=False,
            priority=0
        ))
