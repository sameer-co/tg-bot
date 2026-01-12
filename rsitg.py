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
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5) # Move SL to 1.5R
        active_trade['highest_price'] = price
        active_trade['log'] += f"\n💰 *Partial Exit (70%):* +${profit_70:.2f} banked."
        status_updated = True

    # PHASE C: STEP TRAILING
    if active_trade['partial_done']:
        if price > active_trade['highest_price'] * 1.0020:
            active_trade['highest_price'] = price
            active_trade['sl'] = active_trade['sl'] * 1.0010 # Step SL up 0.1%

    if status_updated:
        current_status = f"📊 *Live Trade: {SYMBOL}*\nPrice: ${price:.2f}\nSL: ${active_trade['sl']:.2f}\n{active_trade['log']}"
        await update_telegram(current_status, active_trade['msg_id'])

    # PHASE D: FINAL EXIT
    if price <= active_trade['sl']:
        if active_trade['partial_done']:
            rem_profit = ((price - active_trade['entry']) / risk_dist) * (active_trade['risk_usd'] * 0.30)
            stats['balance'] += rem_profit
            stats['wins'] += 1
            total_usd = (active_trade['risk_usd'] * 2.1 * 0.70) + rem_profit
        else:
            loss_usd = ((active_trade['entry'] - price) / risk_dist) * active_trade['risk_usd']
            stats['balance'] -= loss_usd
            if loss_usd > 0: stats['losses'] += 1
            total_usd = -loss_usd
        
        final_r = total_usd / active_trade['risk_usd']
        res = "✅ WIN" if total_usd > 0 else "🛑 LOSS"
        final_log = (f"{res} *TRADE CLOSED*\n"
                     f"PnL: ${total_usd:.2f} ({final_r:.2f}R)\n"
                     f"New Bal: ${stats['balance']:.2f}\n"
                     f"Record: {stats['wins']}W - {stats['losses']}L")
        
        await bot.send_message(chat_id=CHAT_ID, text=final_log, parse_mode='Markdown')
        active_trade = None

# ==================== 4. MAIN EXECUTION LOOP ====================

async def main():
    global active_trade
    print(f"🚀 BOT STARTING | SYMBOL: {SYMBOL} | BAL: ${stats['balance']}")
    
    # 500-candle warm-up on startup
    await get_accurate_indicators()

    async with websockets.connect(f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m") as ws:
        while True:
            data = json.loads(await ws.recv())
            if 'k' in data:
                price = float(data['k']['c'])
                if active_trade: await monitor_trade(price)

                # Check Signal on 1m Candle Close
                if data['k']['x'] and not active_trade:
                    rsi, rsi_ema, prev_rsi, prev_rsi_ema = await get_accurate_indicators()
                    
                    if rsi and prev_rsi:
                        # CORE LOGIC: RSI Crossover EMA
                        if prev_rsi <= prev_rsi_ema and rsi > rsi_ema:
                            # SL calculation from 15m candle low
                            resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1")
                            low = float(resp.json()[0][3]) * 0.9995
                            
                            risk_usd = stats['balance'] * 0.05 # Risk 5%
                            active_trade = {
                                'entry': price, 'initial_sl': low, 'sl': low,
                                'tp': price + ((price - low) * 2.1), 'risk_usd': risk_usd,
                                'partial_done': False, 'sl_at_recovery': False, 'highest_price': price,
                                'log': f"🚀 *Entry:* ${price:.2f}\n*Initial SL:* ${low:.2f}"
                            }
                            active_trade['msg_id'] = await update_telegram(active_trade['log'])
                        
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] RSI: {rsi:.2f} | Signal: {rsi_ema:.2f} | Price: {price}")

if __name__ == "__main__":
    asyncio.run(main())
