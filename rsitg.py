import asyncio
import websockets
import json
import telegram
import requests
import numpy as np
from datetime import datetime
import pytz
import os

# ==================== 1. CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
# Replace with your actual token or set as environment variable
TELEGRAM_TOKEN = os.getenv('TG_TOKEN', '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs').replace(' ', '')
CHAT_ID = '1950462171'

# Paper Trading Stats
INITIAL_BALANCE = 1000.0
stats = {"balance": INITIAL_BALANCE, "wins": 0, "losses": 0, "total_trades": 0}

# active_trade state management
active_trade = None  

all_closes_15m = []
rsi_history = []
prev_rsi = None
prev_rsi_ema = None
bot = None
last_15m_time = None

# ==================== 2. INDICATORS & UTILS ====================

def wilders_rsi(closes):
    """Calculates Wilder's RSI using the standard smoothing method."""
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
        rsi_val = 100 - (100 / (1 + rs))
        rsi_history.append(rsi_val)
    return rsi_history[-1]

def price_ema(data, period):
    """Calculates Exponential Moving Average for the RSI Signal line."""
    if len(data) < period: return data[-1]
    alpha = 2 / (period + 1)
    ema = np.mean(data[:period])
    for i in range(period, len(data)):
        ema = alpha * data[i] + (1 - alpha) * ema
    return ema

def get_ist():
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S IST')

async def update_telegram_log(msg, msg_id=None):
    """Edits an existing message if msg_id is provided, otherwise sends a new one."""
    global bot
    try:
        if msg_id:
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=msg_id, text=msg, parse_mode='Markdown')
            return msg_id
        else:
            sent = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            return sent.message_id
    except Exception as e:
        print(f"❌ Telegram Error: {e}")
        return msg_id

async def fetch_klines(symbol, interval, limit=100):
    """Fetches historical candle data from Binance REST API."""
    try:
        resp = requests.get('https://api.binance.com/api/v3/klines', 
                          params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"❌ API Error: {e}")
        return []

# ==================== 3. THE TRADING ENGINE ====================

async def monitor_active_trade(price):
    global active_trade, stats
    if not active_trade: return

    # Core math variables
    initial_risk_val = active_trade['entry'] - active_trade['initial_sl']
    status_updated = False
    
    # --- PHASE 1: FEE RECOVERY (Triggered at 1.5x Risk) ---
    if not active_trade['partial_done'] and not active_trade['sl_at_recovery']:
        if price >= (active_trade['entry'] + (initial_risk_val * 1.5)):
            active_trade['sl'] = active_trade['entry'] + (initial_risk_val * 0.5)
            active_trade['sl_at_recovery'] = True
            active_trade['log'] += f"\n🛡️ *1.5x Reached:* SL moved to +0.5R (Fee Recovery)"
            status_updated = True

    # --- PHASE 2: PARTIAL EXIT (Triggered at 2.1x Risk) ---
    if not active_trade['partial_done'] and price >= active_trade['tp']:
        # Realize 70% of the target profit
        realized_profit_usd = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += realized_profit_usd
        
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade['entry'] + (initial_risk_val * 1.5) # Move SL to 1.5R
        active_trade['highest_price'] = price
        active_trade['log'] += f"\n💰 *Partial Exit (70%):* +${realized_profit_usd:.2f} banked."
        status_updated = True

    # --- PHASE 3: ACTIVE TRAILING (Steps of 0.10% every 0.20% move) ---
    if active_trade['partial_done']:
        if price > active_trade['highest_price'] * 1.0020:
            active_trade['highest_price'] = price
            active_trade['sl'] = active_trade['sl'] * 1.0010
            # We don't edit the TG message for every tiny trail to avoid rate limits

    # Refresh the TG Log if a milestone was hit
    if status_updated:
        current_status = f"📊 *Live Trade Update: {SYMBOL}*\nPrice: ${price:.2f}\nCurrent SL: ${active_trade['sl']:.2f}\n{active_trade['log']}"
        await update_telegram_log(current_status, active_trade['msg_id'])

    # --- PHASE 4: FINAL EXIT CHECK ---
    if price <= active_trade['sl']:
        if active_trade['partial_done']:
            # Calculate PnL for the remaining 30%
            rem_profit_usd = ((price - active_trade['entry']) / initial_risk_val) * (active_trade['risk_usd'] * 0.30)
            stats['balance'] += rem_profit_usd
            stats['wins'] += 1
            total_win_usd = (active_trade['risk_usd'] * 2.1 * 0.70) + rem_profit_usd
            actual_r = total_win_usd / active_trade['risk_usd']
            
            result_msg = (f"✅ *TRADE COMPLETED: {SYMBOL}*\n"
                          f"Outcome: Success (Partial + Trail)\n"
                          f"Total Profit: +${total_win_usd:.2f}\n"
                          f"Performance: *{actual_r:.2f}R*\n"
                          f"Final Balance: ${stats['balance']:.2f}\n"
                          f"Record: {stats['wins']}W - {stats['losses']}L")
        else:
            # Full SL or Fee Recovery hit
            final_pnl_usd = ((price - active_trade['entry']) / initial_risk_val) * active_trade['risk_usd']
            stats['balance'] += final_pnl_usd
            actual_r = final_pnl_usd / active_trade['risk_usd']
            
            if final_pnl_usd < 0: stats['losses'] += 1
            else: stats['wins'] += 1 # Fee recovery is technically a win
            
            outcome = "Fee Recovery Hit" if final_pnl_usd > 0 else "Stop Loss Hit"
            result_msg = (f"🛑 *TRADE CLOSED: {SYMBOL}*\n"
                          f"Outcome: {outcome}\n"
                          f"Net PnL: ${final_pnl_usd:.2f}\n"
                          f"Performance: *{actual_r:.2f}R*\n"
                          f"Final Balance: ${stats['balance']:.2f}")

        await bot.send_message(chat_id=CHAT_ID, text=result_msg, parse_mode='Markdown')
        stats['total_trades'] += 1
        active_trade = None

# ==================== 4. MAIN LOOP ====================

async def main():
    global all_closes_15m, prev_rsi, prev_rsi_ema, bot, active_trade, last_15m_time
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    print(f"🚀 {SYMBOL} BOT LIVE | IST: {get_ist()}")

    # Bootstrapping Data
    raw_data = await fetch_klines(SYMBOL, '15m', 100)
    all_closes_15m = [float(c[4]) for c in raw_data]
    
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    async with websockets.connect(uri) as ws:
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            
            if 'k' in data:
                price = float(data['k']['c'])
                
                # Monitor current trade on every tick
                if active_trade:
                    await monitor_active_trade(price)

                # Process new signals on candle close
                if data['k']['x'] and not active_trade:
                    # Sync 15m data
                    current_min = datetime.now().strftime('%M')
                    if last_15m_time != current_min and int(current_min) % 15 == 0:
                        raw_15m = await fetch_klines(SYMBOL, '15m', 100)
                        all_closes_15m = [float(c[4]) for c in raw_15m]
                        last_15m_time = current_min
                    
                    all_closes_15m[-1] = price
                    rsi = wilders_rsi(all_closes_15m)
                    rsi_ema_val = price_ema(rsi_history, EMA_RSI_PERIOD)

                    # SIGNAL: RSI Bullish Crossover
                    if prev_rsi is not None and prev_rsi <= prev_rsi_ema and rsi > rsi_ema_val:
                        # Entry Logic
                        candle_data = await fetch_klines(SYMBOL, '15m', 1)
                        low = float(candle_data[0][3]) * 0.9995 # 0.05% Buffer
                        risk_usd = stats['balance'] * 0.05 # Risking 5% of wallet
                        
                        active_trade = {
                            'entry': price, 'initial_sl': low, 'sl': low,
                            'tp': price + ((price - low) * 2.1),
                            'risk_usd': risk_usd, 'partial_done': False, 
                            'sl_at_recovery': False, 'highest_price': price,
                            'log': f"🚀 *New Trade Opened*\nEntry: ${price:.2f}\nInitial SL: ${low:.2f}"
                        }
                        active_trade['msg_id'] = await update_telegram_log(active_trade['log'])

                    prev_rsi, prev_rsi_ema = rsi, rsi_ema_val
                    print(f"[{get_ist()}] ${price:<7.2f} | RSI: {rsi:.1f} | Bal: ${stats['balance']:.2f}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Stopped")
