# Trade Monitor

Plugin-based monitor service.

## Run
1) Create and activate a Python 3.11 venv
2) pip install -r requirements.txt (or install the same packages you already used)
3) Copy .env.example to .env and fill keys
4) python main.py

## Configure
- ENABLED_MONITORS=truth_social,example
- Each monitor in monitors/ implements un(**kwargs) and sets FRIENDLY_NAME.

## Notes
- Pushover priority auto-boosts to 1 when a signal includes tickers.
- State is stored in .truth_trader_state.json.
