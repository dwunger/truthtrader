import httpx
from tenacity import retry, wait_exponential_jitter, stop_after_attempt, retry_if_exception_type

# Small, focused wrapper
@retry(wait=wait_exponential_jitter(initial=1, exp_base=2, max=20),
       stop=stop_after_attempt(4),
       retry=retry_if_exception_type((httpx.HTTPError,)))
def notify_pushover(title: str, message: str, priority: int = 0, token: str = None, 
                    user: str = None, retry_interval: int = None, expire: int = None,
                    url: str = None, url_title: str = None):
    """
    Send a Pushover notification.
    
    Args:
        title: Notification title (max 250 chars)
        message: Notification body (max 1024 chars)
        priority: -2 to 2
            -2: No notification
            -1: Silent
             0: Normal (default)
             1: High (bypasses quiet hours)
             2: Emergency (requires retry_interval and expire)
        token: Pushover API token
        user: Pushover user key
        retry_interval: For priority 2, seconds between retries (min 30, default 30 for TACO)
        expire: For priority 2, seconds until giving up (max 10800, default 3600 for TACO)
        url: Supplementary URL to include with notification (opens when tapped)
        url_title: Text for the URL button (default: "View Full Post")
    """
    if not token or not user:
        # Allow caller to pre-validate; we just no-op to avoid crashing the pipeline
        return
    
    data = {
        "token": token,
        "user": user,
        "title": title[:250],
        "message": message[:1024],  # Hard limit from Pushover
        "priority": int(priority),
    }
    
    # Add URL if provided (opens when user taps notification)
    if url:
        data["url"] = url[:512]  # Pushover URL limit
        if url_title:
            data["url_title"] = url_title[:100]
    
    # Priority 2 (emergency) requires retry and expire parameters
    if priority == 2:
        # Use provided values or TACO defaults
        retry_interval = retry_interval or 30  # Default: 30 seconds (Pushover minimum)
        expire = expire or 3600  # Default: 60 minutes for TACO
        
        # Validate ranges (Pushover limits)
        retry_interval = max(30, min(retry_interval, 10800))  # Pushover min is 30s
        expire = max(30, min(expire, 10800))  # Max 3 hours
        
        data["retry"] = retry_interval
        data["expire"] = expire
        
        print(f"[notify] Emergency (priority 2): retry every {retry_interval}s, expire after {expire}s")
    
    # Add truncation indicator if message was cut off
    if len(message) > 1024:
        print(f"[notify] WARNING: Message truncated from {len(message)} to 1024 chars")
        if url:
            print(f"[notify] Full content available at URL: {url[:80]}...")
    
    with httpx.Client(timeout=15) as http:
        r = http.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
        )
        r.raise_for_status()
        
        # For priority 2, log the receipt token
        if priority == 2:
            try:
                response_data = r.json()
                receipt = response_data.get("receipt")
                if receipt:
                    print(f"[notify] Emergency notification sent. Receipt: {receipt}")
                    print(f"[notify] User must acknowledge within {expire}s or it will expire")
            except Exception:
                pass