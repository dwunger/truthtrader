import os

def get_config():
    return {
        # Models
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
        "MODEL": os.getenv("MODEL", "claude-sonnet-4-5-20250929"),
        "REASONING_MODEL": os.getenv("REASONING_MODEL", "claude-opus-4-20250514"),
        "REASONING_TRIGGER_CONF": float(os.getenv("REASONING_TRIGGER_CONF", "0.50")),
        "REASONING_FALLBACKS": [
            os.getenv("REASONING_FALLBACK_1", "claude-sonnet-4-20250514"),
            os.getenv("REASONING_FALLBACK_2", "claude-sonnet-3-7-20250219"),
            os.getenv("REASONING_FALLBACK_3", "claude-haiku-4-5-20251001"),
        ],

        # Truth Social
        "TRUTH_HANDLE": os.getenv("TRUTH_HANDLE", "realDonaldTrump"),

        # Poll cadence
        "POLL_SECONDS": int(os.getenv("POLL_SECONDS", "90")),
        
        # Rate limit handling: Add delay between posts to avoid bursts
        "POST_PROCESS_DELAY": float(os.getenv("POST_PROCESS_DELAY", "2.0")),  # 2 seconds between posts

        # Search budget
        "MAX_SEARCH_PER_DAY": int(os.getenv("MAX_SEARCH_CALLS_PER_DAY", "60")),

        # Location / filters (optional)
        "SEARCH_FILTERS": [d.strip() for d in os.getenv("SEARCH_FILTERS", "").split(",") if d.strip()][:20],
        "LOCATION": {
            "country": os.getenv("LOCATION_COUNTRY") or None,
            "city": os.getenv("LOCATION_CITY") or None,
            "region": os.getenv("LOCATION_REGION") or None,
            "timezone": os.getenv("LOCATION_TZ") or None,
        },

        # Pushover
        "PUSHOVER_USER": os.getenv("PUSHOVER_USER_KEY"),
        "PUSHOVER_TOKEN": os.getenv("PUSHOVER_API_TOKEN"),

        # Whitelist
        "TICKER_WHITELIST": {t.strip().upper() for t in os.getenv("TICKER_WHITELIST", "").split(",") if t.strip()},

        # State
        "STATE_FILE": os.getenv("STATE_FILE", ".truth_trader_state.json"),
    }
