import random, time
from datetime import datetime
from typing import Optional, List, Dict, Any

import truthbrush as tb
from tenacity import retry, wait_exponential_jitter, stop_after_attempt, retry_if_exception_type

from .base import Monitor
from core.bus import Event
from core.analysis import strip_html

class RateLimit(Exception): ...
TB = tb.Api()

def _is_cloudflare_html(payload: Any) -> bool:
    return isinstance(payload, str) and (
        "<title>Access denied | truthsocial.com used Cloudflare" in payload or "Error 1015" in payload
    )

@retry(wait=wait_exponential_jitter(initial=1, exp_base=2, max=30),
       stop=stop_after_attempt(4),
       retry=retry_if_exception_type((RateLimit, RuntimeError)))
def _fetch_latest_page(handle: str, since_id: Optional[str]) -> List[dict]:
    new_posts: List[dict] = []
    try:
        page_iter = TB.pull_statuses(
            username=handle,
            replies=False,
            verbose=False,
            created_after=None,
            since_id=since_id,
            pinned=False,
        )
        for i, post in enumerate(page_iter):
            if _is_cloudflare_html(post):
                raise RateLimit("Cloudflare 1015 HTML")
            pid = post.get("id")
            if not pid: continue
            if since_id and pid <= since_id:
                break
            new_posts.append(post)
            if i > 25: break
        return new_posts
    except Exception as e:
        msg = str(e)
        if "429" in msg or "1015" in msg or "rate limited" in msg.lower(): raise RateLimit(msg)
        if "Failed to decode JSON" in msg or "Access denied | truthsocial.com" in msg: raise RateLimit(msg)
        raise RuntimeError(msg)

class Monitor(Monitor):  # type: ignore[misc]
    name = "truthSocial"

    def run(self) -> None:
        state = self.ctx["state"]
        handle = self.config["TRUTH_HANDLE"]
        poll_seconds = int(self.config["POLL_SECONDS"])

        # Namespaced last_seen
        last_seen = state.get(self.name, "last_seen_id", default=None)

        # Bootstrap: analyze newest once
        if not last_seen:
            first_page = _fetch_latest_page(handle, None)
            if first_page:
                latest = first_page[0]
                self._emit(latest)  # publish with analysis
                last_seen = latest["id"]
                state.set(last_seen, self.name, "last_seen_id")

        # Poll loop
        while True:
            try:
                posts = _fetch_latest_page(handle, last_seen)
                print(f"[fetch:{self.name}] got {len(posts)} new post(s) since {last_seen or '(none)'}")
                for st in posts:
                    sid = st.get("id")
                    if not sid: continue
                    if last_seen and sid <= last_seen: continue
                    self._emit(st)
                    if (not last_seen) or (sid > last_seen):
                        last_seen = sid
                        state.set(last_seen, self.name, "last_seen_id")
            except RateLimit as e:
                cooldown = random.randint(240, 480)
                print(f"[poll:{self.name}] rate limited: {e}. Cooling {cooldown}s")
                time.sleep(cooldown)
            except Exception as e:
                print(f"[poll:{self.name}] ERROR: {e}")

            jitter = random.uniform(-5, 5)
            time.sleep(max(30, poll_seconds + jitter))

    def _emit(self, st: dict):
        sid = st.get("id")
        url = st.get("url") or f"https://truthsocial.com/@{self.config['TRUTH_HANDLE']}/{sid}"
        created_at = st.get("created_at") or st.get("createdAt") or ""
        text = strip_html(st.get("content") or st.get("text") or st.get("spoiler_text") or "").strip()

        # Let the central publisher handle OpenAI and Pushover formatting.
        self.publish(Event(
            source=self.name,
            title="TruthTrader — update",
            message=f"{url}\n\n{text[:180] + ('…' if len(text) > 180 else '')}" if text else url,
            url=url,
            priority=0,
            created_at=created_at,
            payload={"text": text, "analyze": True, "raw": st}
        ))
