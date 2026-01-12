import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta
import numpy as np
from datetime import datetime
import pytz
import os

# ==================== 1. CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
# Enter your Telegram details here
TELEGRAM_TOKEN = '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs'
CHAT_ID = '1950462171'

# Paper Trading Stats
INITIAL_BALANCE = 1000.0
stats = {"balance": INITIAL_BALANCE, "wins": 0, "losses": 0, "total_trades": 0}

# Global State
active_trade = None  
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== 2. DATA & RSI CALCULATOR ====================

async def get_accurate_indicators():
    """Fetches 500 candles to ensure RSI convergence matches Binance exactly."""
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 500}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        # Calculate RSI (Wilder's is the default in pandas_ta RSI)
        rsi_series = ta.rsi(df['close'], length=RSI_PERIOD)
        # Calculate EMA of RSI (Signal Line)
        rsi_ema_series = ta.ema(rsi_series, length=EMA_RSI_PERIOD)
        
        return rsi_series.iloc[-1], rsi_ema_series.iloc[-1], rsi_series.iloc[-2], rsi_ema_series.iloc[-2]
    except Exception as e:
        print(f"❌ Indicator Error: {e}")
        return None, None, None, None

async def update_telegram(msg, msg_id=None):
    try:
        if msg_id:
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=msg_id, text=msg, parse_mode='Markdown')
            return msg_id
        else:
            sent = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            return sent.message_id
    except Exception as e:
        print(f"❌ TG Error: {e}")
        return msg_id

# ==================== 3. TRADE MONITORING ENGINE ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    status_updated = False
    
    # PHASE A: FEE RECOVERY (1.5x Risk)
    if not active_trade['partial_done'] and not active_trade['sl_at_recovery']:
        if price >= (active_trade['entry'] + (risk_dist * 1.5)):
            active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
            active_trade['sl_at_recovery'] = True
            active_trade['log'] += f"\n🛡️ *1.5x Reached:* SL moved to +0.5R (Recovery)"
            status_updated = True

    # PHASE B: PARTIAL EXIT (2.1x Risk)
    if not active_trade['partial_done'] and price >= active_trade['tp']:
        profit_70 = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += profit_70
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade
