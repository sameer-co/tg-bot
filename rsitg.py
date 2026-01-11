import asyncio
import websockets
import json
import telegram
import requests
import numpy as np
from datetime import datetime
import pytz
import time
import os

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = os.getenv('TG_TOKEN', '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs').replace(' ', '')
CHAT_ID = '1950462171'

# Paper Trading Stats
INITIAL_BALANCE = 1000.0
stats = {"balance": INITIAL_BALANCE, "wins": 0, "losses": 0, "total_trades": 0}
active_trade = None  # Stores: {'entry': 0, 'sl': 0, 'tp': 0, 'risk_usd': 0}

all_closes_15m = []
rsi_history = []
prev_rsi = None
prev_rsi_ema = None
bot = None
last_15m_time = None

# ==================== INDICATORS & UTILS ====================

def wilders_rsi(closes):
    global rsi_history
    if len(closes) < RSI_PERIOD + 1: return 50.0
    rsi_history.clear()
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    avg_gain = np.mean(gains[:RSI_PERIOD])
    avg_loss = np.mean(losses[:RSI_PERIOD])
    for i in range(RSI_PERIOD, len(closes)):
        change = closes[i] - closes[i-1]
        gain, loss = max(change, 0), max(-change, 0)
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
        rs = avg_gain / avg_loss if avg_loss > 0 else 0
        rsi_history.append(100 - (100 / (1 + rs)))
    return rsi_history[-1]

def price_ema(data, period):
    if len(data) < period: return data[-1]
    alpha = 2 / (period + 1)
    ema = np.mean(data[:period])
    for i in range(period, len(data)):
        ema = alpha * data[i] + (1 - alpha) * ema
    return ema

def get_ist():
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S IST')

async def send_alert(msg):
    global bot
    if not bot: return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg)
    except Exception as e:
        print(f"❌ TG ERROR: {e}")

async def fetch_klines(symbol, interval, limit=100):
    try:
        resp = requests.get('https://api.binance.com/api/v3/klines',
                          params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
        return resp.json()
    except:
        return []

# ==================== TRADING ENGINE ====================

async def check_trade_exits(current_price):
    global active_trade, stats
    if not active_trade: return

    # WIN: Take Profit Hit
    if current_price >= active_trade['tp']:
        profit = active_trade['risk_usd'] * 2.1
        stats['balance'] += profit
        stats['wins'] += 1
        stats['total_trades'] += 1
        msg = (f"🎯 TARGET HIT! +${profit:.2f}\n"
               f"💰 Balance: ${stats['balance']:.2f}\n"
               f"📊 Record: {stats['wins']}W - {stats['losses']}L")
        await send_alert(msg)
        active_trade = None

    # LOSS: Stop Loss Hit
    elif current_price <= active_trade['sl']:
        loss = active_trade['risk_usd']
        stats['balance'] -= loss
        stats['losses'] += 1
        stats['total_trades'] += 1
        msg = (f"🛑 STOP LOSS HIT! -${loss:.2f}\n"
               f"💰 Balance: ${stats['balance']:.2f}\n"
               f"📊 Record: {stats['wins']}W - {stats['losses']}L")
        await send_alert(msg)
        active_trade = None

# ==================== MAIN LOOP ====================

async def main():
    global all_closes_15m, prev_rsi, prev_rsi_ema, bot, last_15m_time, active_trade
    
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    print(f"🚀 SOL 15M BOT STARTING | BAL: ${stats['balance']}")
    
    # Initial Data Load
    raw_data = await fetch_klines(SYMBOL, '15m', 100)
    all_closes_15m = [float(c[4]) for c in raw_data]
    
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    async with websockets.connect(uri) as ws:
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            
            # Update price on every tick, but check logic on candle close
            if 'k' in data:
                price = float(data['k']['c'])
                
                # 1. Check exit conditions for active trade on every tick
                if active_trade:
                    await check_trade_exits(price)

                # 2. On 1m candle close, check for new entry signals
                if data['k']['x']:
                    # Periodical refresh of 15m data
                    current_min = datetime.now().strftime('%M')
                    if last_15m_time != current_min and int(current_min) % 15 == 0:
                        raw_15m = await fetch_klines(SYMBOL, '15m', 100)
                        all_closes_15m = [float(c[4]) for c in raw_15m]
                        last_15m_time = current_min
                    
                    all_closes_15m[-1] = price
                    rsi = wilders_rsi(all_closes_15m)
                    rsi_ema_val = price_ema(rsi_history, EMA_RSI_PERIOD)

                    # SIGNAL LOGIC
                    crossover = (prev_rsi is not None and prev_rsi <= prev_rsi_ema and rsi > rsi_ema_val)
                    
                    if crossover and active_trade is None:
                        # Fetch the low of the 15m candle for SL
                        latest_candle = await fetch_klines(SYMBOL, '15m', 1)
                        candle_low = float(latest_candle[0][3])
                        
                        risk_per_coin = price - candle_low
                        if risk_per_coin > 0:
                            tp_price = price + (risk_per_coin * 2.1)
                            risk_usd = stats['balance'] * 0.05 # Risking 5% of balance
                            
                            active_trade = {
                                'entry': price,
                                'sl': candle_low,
                                'tp': tp_price,
                                'risk_usd': risk_usd
                            }
                            
                            alert_msg = (f"🚀 POSITION OPENED\n"
                                         f"Entry: ${price:.2f}\n"
                                         f"SL: ${candle_low:.2f}\n"
                                         f"TP (2.1x): ${tp_price:.2f}")
                            await send_alert(alert_msg)

                    prev_rsi, prev_rsi_ema = rsi, rsi_ema_val
                    
                    # Log status to console
                    status = f"TRADE ACTIVE (SL: {active_trade['sl']})" if active_trade else "SCANNING..."
                    print(f"[{get_ist()}] ${price:<7} | RSI: {rsi:.1f} | {status} | Bal: ${stats['balance']:.2f}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped")
