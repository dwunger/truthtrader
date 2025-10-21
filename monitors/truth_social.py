import os, re, time, random, traceback, inspect, threading, json
from typing import Any, List, Optional
from .base import Monitor
from core.bus import Event

try:
    import truthbrush as tb
except Exception as e:
    tb = None
    print(f"[truthSocial] ERROR importing truthbrush: {e}", flush=True)

VERSION = "truth_social/2.0.0-haiku"
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

        # Import Anthropic for Haiku screening
        try:
            from anthropic import Anthropic
            self.anthropic = Anthropic(api_key=config["ANTHROPIC_API_KEY"])
            self.enable_screening = os.getenv("TRUTH_SOCIAL_ENABLE_HAIKU_SCREENING", "1") != "0"
        except Exception as e:
            print(f"[truthSocial] WARNING: Anthropic not available, disabling screening: {e}", flush=True)
            self.anthropic = None
            self.enable_screening = False

        self.api = tb.Api()
        self.handle = config["TRUTH_HANDLE"]
        self.poll_seconds = max(30, int(os.getenv("TRUTH_SOCIAL_POLL_SECONDS", "90")))
        self.screening_model = os.getenv("TRUTH_SOCIAL_SCREENING_MODEL", "claude-haiku-4-5-20251001")
        self.state = ctx.get("state")
        self.state_key_last = "truth_social:last_seen_id"
        self.config = config  # Store for rate limit delay
        
        try:
            self.publish_timeout_sec = int(os.getenv("PUBLISH_TIMEOUT_SEC", "25"))
        except Exception:
            self.publish_timeout_sec = 25

        # Heartbeat controls
        try:
            self.heartbeat_sec = int(os.getenv("TRUTH_SOCIAL_HEARTBEAT_SEC", "0"))
        except Exception:
            self.heartbeat_sec = 0
        self.heartbeat_push = os.getenv("TRUTH_SOCIAL_HEARTBEAT_PUSH", "0").strip() not in ("0", "", "false", "False")
        
        print(f"[truthSocial] Haiku screening: {'ENABLED' if self.enable_screening else 'DISABLED'}", flush=True)

    def _screen_with_haiku(self, text: str) -> dict:
        """
        Use Haiku to screen if post is market-relevant.
        Cost: ~$0.0001 per post
        Returns: {"is_market_relevant": bool, "confidence": float, "reasoning": str}
        """
        if not self.enable_screening or not self.anthropic:
            # Fallback: analyze everything
            return {"is_market_relevant": True, "confidence": 1.0, "reasoning": "screening disabled"}
        
        try:
            response = self.anthropic.messages.create(
                model=self.screening_model,
                max_tokens=200,
                temperature=0,
                system="You are a trading assistant screening social media posts. Identify if a post could impact financial markets. Respond with JSON only.",
                messages=[{
                    "role": "user",
                    "content": f'''Is this post market-relevant for trading? Consider:
- Trade policy, tariffs, regulations
- Company mentions (Tesla, Apple, etc.)
- Economic policy, Fed, taxes
- Major political events affecting markets

Skip: judge nominations, routine endorsements, celebrations, statistics without market impact.

Respond JSON: {{"is_market_relevant": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}}

Post: {text[:500]}'''
                }]
            )
            
            # Extract text
            response_text = ""
            for block in response.content:
                if hasattr(block, 'type') and block.type == 'text':
                    response_text += block.text
            
            # Parse JSON
            response_text = response_text.strip()
            if response_text.startswith("```"):
                response_text = re.sub(r"^```[a-zA-Z]*\s*", "", response_text)
                response_text = re.sub(r"\s*```$", "", response_text)
            
            result = json.loads(response_text)
            return result
        except Exception as e:
            print(f"[truthSocial] Haiku screening error: {e}", flush=True)
            # Fallback: analyze everything (safe default)
            return {"is_market_relevant": True, "confidence": 0.5, "reasoning": "screening failed"}

    # ---- publish with timeout ----
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

        # Media-only posts: skip analysis
        if not text:
            print(f"[truthSocial] media-only post, skipping analysis", flush=True)
            evt = Event(
                source=self.name,
                title="TruthTrader — update",
                message="Media-only post (no text). No trade signal.",
                url=url,
                created_at=created_at,
                priority=0,
                payload={"analyze": False}
            )
            self._publish_with_timeout(evt)
            return

        # Screen with Haiku
        print(f"[truthSocial] screening with {self.screening_model}...", flush=True)
        screen_result = self._screen_with_haiku(text)
        
        if screen_result["is_market_relevant"] and screen_result["confidence"] > 0.6:
            print(f"[truthSocial] ✓ MARKET-RELEVANT (conf={screen_result['confidence']:.2f}) → analyzing with Sonnet", flush=True)
            print(f"[truthSocial]   reasoning: {screen_result['reasoning']}", flush=True)
            
            evt = Event(
                source=self.name,
                title="TruthTrader — update",
                message="Analyzing market-relevant post...",
                url=url,
                created_at=created_at,
                priority=0,
                payload={
                    "analyze": True,
                    "text": text,
                    "taco_mode": False,
                    "pre_screened": True,  # Already screened by Haiku
                    "screen_confidence": screen_result["confidence"],
                }
            )
        else:
            print(f"[truthSocial] ✗ not market-relevant (conf={screen_result['confidence']:.2f}), skipping analysis", flush=True)
            print(f"[truthSocial]   reasoning: {screen_result['reasoning']}", flush=True)
            
            # Still send notification, but skip expensive Sonnet analysis
            evt = Event(
                source=self.name,
                title="TruthTrader — update (not analyzed)",
                message=f"Post not market-relevant (Haiku conf={screen_result['confidence']:.2f}):\n{text[:200]}...\n\nSkipped analysis to save tokens.",
                url=url,
                created_at=created_at,
                priority=0,
                payload={"analyze": False}
            )
        
        self._publish_with_timeout(evt)

    def run(self) -> None:
        print(f"[truthSocial] RUN START — {VERSION}", flush=True)
        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        print(f"[truthSocial] start; handle=@{self.handle} poll={self.poll_seconds}s last_seen={last_seen}", flush=True)

        next_heartbeat_ts = time.time() + max(0, self.heartbeat_sec) if self.heartbeat_sec > 0 else float("inf")

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
                    
                    # Get rate limit delay from config
                    post_delay = self.config.get("POST_PROCESS_DELAY", 2.0)
                    
                    for post in reversed(new_posts):
                        try:
                            self._publish_post(post)
                            
                            # Add delay between posts
                            if post_delay > 0:
                                print(f"[truthSocial] rate limit protection: waiting {post_delay}s", flush=True)
                                time.sleep(post_delay)
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

                # Heartbeat
                now = time.time()
                if now >= next_heartbeat_ts:
                    if self.heartbeat_push:
                        print("[truthSocial] heartbeat publish → begin", flush=True)
                        self._publish_with_timeout(Event(
                            source=self.name,
                            title="Truth Social heartbeat",
                            message=f"Alive. last_seen={last_seen or '(none)'}",
                            priority=0,
                            payload={"analyze": False},
                        ))
                        print("[truthSocial] heartbeat publish → done", flush=True)
                    else:
                        print(f"[truthSocial] heartbeat (console-only): alive; last_seen={last_seen}", flush=True)
                    next_heartbeat_ts = now + max(5, self.heartbeat_sec) if self.heartbeat_sec > 0 else float("inf")

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