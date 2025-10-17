import httpx
from tenacity import retry, wait_exponential_jitter, stop_after_attempt, retry_if_exception_type

# Small, focused wrapper
@retry(wait=wait_exponential_jitter(initial=1, exp_base=2, max=20),
       stop=stop_after_attempt(4),
       retry=retry_if_exception_type((httpx.HTTPError,)))
def notify_pushover(title: str, message: str, priority: int = 0, token: str = None, user: str = None):
    if not token or not user:
        # Allow caller to pre-validate; we just no-op to avoid crashing the pipeline
        return
    with httpx.Client(timeout=15) as http:
        r = http.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": token,
                "user": user,
                "title": title[:250],
                "message": message[:1024],
                "priority": int(priority),
            },
        )
        r.raise_for_status()
