from typing import Callable, Dict, Any
from core.bus import Event

Publisher = Callable[[Event], None]

class Monitor:
    name = "base"
    def __init__(self, publish: Publisher, config: Dict[str, Any], ctx: Dict[str, Any]):
        self.publish = publish
        self.config = config
        self.ctx = ctx  # ctx["state"] is a State object

    def run(self) -> None:
        raise NotImplementedError
