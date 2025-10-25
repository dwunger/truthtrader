"""
TACO Monitor: Trump Always Chickens Out
Uses Haiku for cheap, intelligent screening of tariff-related posts.
Focuses on PUT options for the drop (real money) and CALLS for the rebound.
"""
import os, re, time, inspect
from typing import Optional
from .base import Monitor
from core.bus import Event

VERSION = "taco/2.1.0"
print(f"[taco] module file → {inspect.getfile(inspect.currentframe())}", flush=True)

class Monitor(Monitor):
    name = "taco"
    
    def __init__(self, publish, config, ctx):
        super().__init__(publish, config, ctx)
        
        try:
            import truthbrush as tb
            self.api = tb.Api()
        except Exception as e:
            raise RuntimeError(f"truthbrush module not available: {e}")
        
        # Import Anthropic for Haiku screening
        try:
            from anthropic import Anthropic
            self.anthropic = Anthropic(api_key=config["ANTHROPIC_API_KEY"])
        except Exception as e:
            raise RuntimeError(f"Anthropic SDK not available: {e}")
        
        self.handle = config.get("TACO_HANDLE") or config["TRUTH_HANDLE"]
        self.poll_seconds = int(os.getenv("TACO_POLL_SECONDS", "90"))
        self.screening_model = os.getenv("TACO_SCREENING_MODEL", "claude-haiku-4-5-20251001")
        self.state = ctx.get("state")
        self.state_key_last = "taco:last_seen_id"
        self.config = config  # Store config for rate limit delay
        
        print(f"[taco] initialized for @{self.handle} | poll={self.poll_seconds}s | screening={self.screening_model}", flush=True)
    
    def _strip_html(self, s: str) -> str:
        if not s:
            return ""
        if "<" in s and ">" in s:
            s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    
    def _screen_with_haiku(self, text: str) -> dict:
        """
        Use Haiku to intelligently screen if post is tariff-related.
        Cost: ~$0.0001 per post (cheap!)
        Returns: {"is_tariff_related": bool, "confidence": float, "reasoning": str}
        """
        try:
            response = self.anthropic.messages.create(
                model=self.screening_model,
                max_tokens=200,
                temperature=0,
                system="You are a trading assistant screening social media posts. Your ONLY job is to identify if a post is about tariffs, trade policy, or trade negotiations. Respond with JSON only.",
                messages=[{
                    "role": "user",
                    "content": f'Is this post about tariffs/trade policy? Respond with JSON: {{"is_tariff_related": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}}\n\nPost: {text[:500]}'
                }]
            )
            
            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, 'type') and block.type == 'text':
                    response_text += block.text
            
            # Parse JSON
            import json
            # Strip markdown if present
            response_text = response_text.strip()
            if response_text.startswith("```"):
                response_text = re.sub(r"^```[a-zA-Z]*\s*", "", response_text)
                response_text = re.sub(r"\s*```$", "", response_text)
            
            result = json.loads(response_text)
            return result
        except Exception as e:
            print(f"[taco] Haiku screening error: {e}", flush=True)
            # Fallback to simple keyword check
            text_lower = text.lower()
            is_related = any(kw in text_lower for kw in ["tariff", "trade war", "trade deal"])
            return {"is_tariff_related": is_related, "confidence": 0.5, "reasoning": "fallback"}
    
    def run(self) -> None:
        print(f"[taco] RUN START — {VERSION}", flush=True)
        last_seen: Optional[str] = self.state.get(self.state_key_last, default=None)
        print(f"[taco] monitoring @{self.handle} for tariff posts | last_seen={last_seen}", flush=True)
        
        # Bootstrap
        if not last_seen:
            print("[taco] bootstrap → fetching recent posts", flush=True)
            try:
                page_iter = self.api.pull_statuses(
                    username=self.handle, replies=False, verbose=False,
                    created_after=None, since_id=None, pinned=False,
                )
                first_page = []
                for i, post in enumerate(page_iter):
                    first_page.append(post)
                    if i >= 5:
                        break
                
                if first_page:
                    latest = first_page[0]
                    
                    # Screen the latest post for tariffs
                    raw = latest.get("content") or latest.get("text") or ""
                    text = self._strip_html(raw)
                    
                    if text:
                        print("[taco] bootstrap → screening latest post with Haiku", flush=True)
                        screen_result = self._screen_with_haiku(text)
                        
                        if screen_result["is_tariff_related"] and screen_result["confidence"] > 0.6:
                            url = latest.get("url") or f"https://truthsocial.com/@{self.handle}/{latest.get('id')}"
                            created_at = latest.get("created_at") or ""
                            
                            print(f"[taco] bootstrap → ✓ TARIFF POST (conf={screen_result['confidence']:.2f})", flush=True)
                            
                            evt = Event(
                                source=self.name,
                                title="TACO Analysis",
                                message="Analyzing tariff-related post with TACO pattern awareness...",
                                url=url,
                                created_at=created_at,
                                priority=0,
                                payload={
                                    "analyze": True,
                                    "text": text,
                                    "taco_mode": True,
                                    "screen_confidence": screen_result["confidence"],
                                }
                            )
                            self.publish(evt)
                        else:
                            print(f"[taco] bootstrap → ✗ not tariff-related (conf={screen_result['confidence']:.2f})", flush=True)
                    
                    last_seen = latest["id"]
                    self.state.set(last_seen, self.state_key_last)
                    print(f"[taco] bootstrap complete | set last_seen={last_seen}", flush=True)
            except Exception as e:
                print(f"[taco] bootstrap error: {e}", flush=True)
        
        # Main loop
        while True:
            try:
                print("[taco] poll tick", flush=True)

                try:
                    page_iter = self.api.pull_statuses(
                        username=self.handle, replies=False, verbose=False,
                        created_after=None, since_id=last_seen, pinned=False,
                    )
                except Exception as e:
                    print(f"[taco] truthbrush fetch failed: {e}", flush=True)
                    time.sleep(60)
                    continue

                
                new_posts = []
                for i, post in enumerate(page_iter):
                    pid = post.get("id")
                    if not pid:
                        continue
                    if last_seen and pid <= last_seen:
                        break
                    new_posts.append(post)
                    if i > 25:
                        break
                
                if new_posts:
                    print(f"[taco] found {len(new_posts)} new posts", flush=True)
                    
                    # Get rate limit delay from config
                    post_delay = self.config.get("POST_PROCESS_DELAY", 2.0)
                    
                    for post in reversed(new_posts):
                        raw = post.get("content") or post.get("text") or ""
                        text = self._strip_html(raw)
                        
                        if not text:
                            continue
                        
                        # Use Haiku to screen (cheap, intelligent)
                        print(f"[taco] screening post with {self.screening_model}...", flush=True)
                        screen_result = self._screen_with_haiku(text)
                        
                        if screen_result["is_tariff_related"] and screen_result["confidence"] > 0.6:
                            url = post.get("url") or f"https://truthsocial.com/@{self.handle}/{post.get('id')}"
                            created_at = post.get("created_at") or ""
                            
                            print(f"[taco] ✓ TARIFF POST DETECTED (conf={screen_result['confidence']:.2f}) → sending to TACO-aware Claude", flush=True)
                            print(f"[taco]   reasoning: {screen_result['reasoning']}", flush=True)
                            
                            evt = Event(
                                source=self.name,
                                title="TACO Analysis",
                                message="Analyzing tariff-related post with TACO pattern awareness...",
                                url=url,
                                created_at=created_at,
                                priority=0,  # Claude will escalate if needed
                                payload={
                                    "analyze": True,
                                    "text": text,
                                    "taco_mode": True,
                                    "screen_confidence": screen_result["confidence"],
                                }
                            )
                            self.publish(evt)
                            
                            # Add delay after publishing to avoid rate limits
                            if post_delay > 0:
                                print(f"[taco] rate limit protection: waiting {post_delay}s", flush=True)
                                time.sleep(post_delay)
                        else:
                            print(f"[taco] ✗ not tariff-related (conf={screen_result['confidence']:.2f})", flush=True)
                        
                        # Update last seen
                        pid = post.get("id")
                        if pid and (not last_seen or pid > last_seen):
                            last_seen = pid
                            self.state.set(last_seen, self.state_key_last)
                
                time.sleep(self.poll_seconds)
                
            except Exception as e:
                print(f"[taco] error: {e}", flush=True)
                import traceback
                traceback.print_exc()
                time.sleep(30)
