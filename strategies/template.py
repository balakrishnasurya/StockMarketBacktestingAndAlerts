"""
Strategy Template
=================
Copy this file to create a new strategy, e.g.:
    cp strategies/template.py strategies/my_strategy.py

Then register it in strategies/__init__.py with ONE line:
    "my_strategy": ("strategies.my_strategy", "My Strategy"),

Implement the signal() function below. It receives a DataFrame with columns:
    timestamp, open, high, low, close, volume
and must return 'BUY', 'SELL', or None.

IMPORTANT: signal() is called with data UP TO the current row only.
           Do NOT use future data (no look-ahead bias).
"""

# Optional constants (the engine reads these automatically)
STOP_LOSS_PCT = 0.03      # 3% stop-loss
TAKE_PROFIT_PCT = 0.08    # 8% take-profit
MIN_CANDLES = 50           # Minimum candles needed for indicators

def signal(df):
    """
    Evaluate the last row of df and return 'BUY', 'SELL', or None.

    Args:
        df: pandas DataFrame with columns [timestamp, open, high, low, close, volume]
            Contains only data up to and including the candle being evaluated.

    Returns:
        'BUY', 'SELL', or None
    """
    # Replace this with your logic
    return None
