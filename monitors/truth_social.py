import os, re, time, random, traceback, inspect, threading
from typing import Any, List, Optional
from .base import Monitor
from core.bus import Event

try:
    import truthbrush as tb
except Exception as e:
    tb = None
    print(f"[truthSocial] ERROR importing truthbrush: {e}", flush=True)

VERSION = "truth_social/1.1.3"
print(f"[truthSocial] module file → {inspect.getfile(inspect.currentframe())}", flush=True)

_CLOUDFLARE_STRINGS = ("Access denied | truthsocial.com used Cloudflare", "Error 1015")
_HTML_TAGS = re.compile(r"<[^>]+>")

def _sleep_with_logs(total_s: int, label: str = "sleep"):
    remaining = max(1, int(total_s))
    step = 5
    while remaining > 0:
        chunk = min(step, remaining)
        print(f"[truthSocial] {label} … {remaining}s left", flush=True)
        time.sleep(chunk)
        remaining -= chunk

def _safe_print_exc(prefix: str, e: Exception):
    try:
        print(f"[truthSocial] {prefix}: {e}", flush=True)
        traceback.print_exc()
    except Exception:
        pass

def _is_cloudflare_html(payload: Any) -> bool:
    if isinstance(payload, str):
        return any(s in payload for s in _CLOUDFLARE_STRINGS)
    return False

def _strip_html(s: str) -> str:
    if not s:
        return ""
    if "<" in s and ">" in s:
        s = _HTML_TAGS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

class Monitor(Monitor):  # type: ignore[misc]
    name = "truthSocial"

    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        if tb is None:
            raise RuntimeError("truthbrush module not available")

        self.api = tb.Api()
        self.handle = config["TRUTH_HANDLE"]
        self.poll_seconds = max(30, int(os.getenv("TRUTH_SOCIAL_POLL_SECONDS", "90")))
        self.state = ctx.get("state")
        self.state_key_last = "truth_social:last_seen_id"
        try:
            self.publish_timeout_sec = int(os.getenv("PUBLISH_TIMEOUT_SEC", "25"))
        except Exception:
            self.publish_timeout_sec = 25

    # ---- publish with timeout so we don't stall forever ----
    def _publish_with_timeout(self, evt: Event) -> bool:
        done = threading.Event()
        err: list[BaseException] = []

        def _run():
            try:
                self.publish(evt)
            except BaseException as e:
                err.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True, name="truthSocial:publish")
        t.start()
        finished = done.wait(timeout=max(1, self.publish_timeout_sec))
        if not finished:
            print(f"[truthSocial] WARNING: publish timed out after {self.publish_timeout_sec}s — continuing monitor loop", flush=True)
            return False
        if err:
            _safe_print_exc("publish exception", err[0])
        return True

    def _fetch_new(self, since_id: Optional[str]) -> List[dict]:
        new_posts: List[dict] = []
        try:
            page_iter = self.api.pull_statuses(
                username=self.handle, replies=False, verbose=False,
                created_after=None, since_id=since_id, pinned=False,
            )
            for i, post in enumerate(page_iter):
                if _is_cloudflare_html(post):
                    raise RuntimeError("Cloudflare 1015 HTML encountered")
                pid = post.get("id")
                if not pid:
                    continue
                if since_id and pid <= since_id:
                    break
                new_posts.append(post)
                if i > 25:
                    break
            return new_posts
        except Exception as e:
            msg = str(e)
            if "429" in msg or "1015" in msg or "rate limit" in msg.lower() or "Access denied" in msg:
                raise RuntimeError(f"rate-limit: {msg}")
            raise

    def _publish_post(self, post: dict) -> None:
        sid = post.get("id")
        url = post.get("url") or f"https://truthsocial.com/@{self.handle}/{sid}"
        created_at = post.get("created_at") or post.get("createdAt") or ""
        raw = post.get("content") or post.get("text") or post.get("spoiler_text") or ""
        text = _strip_html(raw)

        preview = (text or "").replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "…"
        print(f"[post:truthSocial] {sid} | {preview or '(media-only)'}", flush=True)

        analyze_flag = bool(text)
        payload = {"analyze": analyze_flag}
        if analyze_flag:
            payload["text"] = text

        evt = Event(
            source=self.name,
            title="TruthTrader — update",
            message="Media-only post (no text). No trade signal." if not analyze_flag else "Analyzing post…",
            url=url,
            created_at=created_at,
            priority=0,
            payload=payload,
        )
        print("[truthSocial] publish → begin", flush=True)
        ok = self._publish_with_timeout(evt)
        print(f"[truthSocial] publish → {'done' if ok else 'timed out'}", flush=True)

    def run(self) -> None:
        print(f"[truthSocial] RUN START — {VERSION}", flush=True)
        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        print(f"[truthSocial] start; handle=@{self.handle} poll={self.poll_seconds}s last_seen={last_seen}", flush=True)

        try:
            heartbeat_sec = int(os.getenv("TRUTH_SOCIAL_HEARTBEAT_SEC", "600"))
        except Exception:
            heartbeat_sec = 600
        next_heartbeat_ts = time.time() + max(0, heartbeat_sec)

        # Bootstrap
        if not last_seen:
            print("[truthSocial] bootstrap → fetching newest page", flush=True)
            try:
                first_page = self._fetch_new(None)
                print(f"[truthSocial] bootstrap → got {len(first_page) if first_page else 0} post(s)", flush=True)
                if first_page:
                    latest = first_page[0]
                    self._publish_post(latest)
                    print("[truthSocial] bootstrap → publish returned; updating state", flush=True)
                    last_seen = latest["id"]
                    try:
                        self.state.set(last_seen, self.state_key_last)
                        print(f"[truthSocial] bootstrap → state.set OK last_seen={last_seen}", flush=True)
                    except Exception as se:
                        _safe_print_exc("state.set error (bootstrap)", se)
            except Exception as e:
                _safe_print_exc("bootstrap fetch error", e)

        print("[truthSocial] entering main poll loop", flush=True)

        while True:
            try:
                print("[truthSocial] POLL BEGIN", flush=True)
                new_posts = self._fetch_new(last_seen)

                if new_posts:
                    print(f"[truthSocial] poll tick — new={len(new_posts)} (publishing oldest→newest)", flush=True)
                    for post in reversed(new_posts):
                        try:
                            self._publish_post(post)
                        except Exception as pe:
                            _safe_print_exc("publish_post error", pe)
                        pid = post.get("id")
                        if pid and (not last_seen or pid > last_seen):
                            last_seen = pid
                            try:
                                self.state.set(last_seen, self.state_key_last)
                                print(f"[truthSocial] state.set OK last_seen={last_seen}", flush=True)
                            except Exception as se:
                                _safe_print_exc("state.set error", se)
                else:
                    print(f"[truthSocial] poll tick — new=0 last_seen={last_seen}", flush=True)

                now = time.time()
                if heartbeat_sec > 0 and now >= next_heartbeat_ts:
                    try:
                        print("[truthSocial] heartbeat publish → begin", flush=True)
                        self._publish_with_timeout(Event(
                            source=self.name,
                            title="Truth Social heartbeat",
                            message=f"Alive. last_seen={last_seen or '(none)'}",
                            priority=0,
                            payload={"analyze": False},
                        ))
                        print("[truthSocial] heartbeat publish → done", flush=True)
                    except Exception as he:
                        _safe_print_exc("heartbeat push error", he)
                    next_heartbeat_ts = now + heartbeat_sec

                jitter = int(random.uniform(-max(5, self.poll_seconds // 8),
                                            max(5, self.poll_seconds // 8)))
                sleep_s = max(30, self.poll_seconds + jitter)
                _sleep_with_logs(sleep_s, label="idle")

            except RuntimeError as rte:
                msg = str(rte)
                if "rate-limit" in msg.lower() or "429" in msg:
                    cool = random.randint(240, 480)
                    print(f"[truthSocial] rate limited; cooling {cool}s… ({msg})", flush=True)
                    _sleep_with_logs(cool, label="cooldown")
                    continue
                _safe_print_exc("runtime error", rte)
                _sleep_with_logs(10, label="recover")
            except Exception as e:
                _safe_print_exc("ERROR loop", e)
                _sleep_with_logs(10, label="recover")
