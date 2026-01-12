import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime
import pytz

# ==================== 1. CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs'
CHAT_ID = '1950462171'

# Timezone & Stats
IST = pytz.timezone('Asia/Kolkata')
stats = {"balance": 1000.0, "wins": 0, "losses": 0, "total_trades": 0}

# Global State
active_trade = None  
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== 2. UTILS & TIME ====================

def get_ist_now():
    """Returns system time in IST."""
    return datetime.now(IST).strftime('%H:%M:%S')

def format_ist(ms):
    """Converts Binance Unix MS to IST string."""
    dt_utc = datetime.fromtimestamp(ms/1000.0, tz=pytz.UTC)
    return dt_utc.astimezone(IST).strftime('%H:%M:%S')

async def update_telegram(msg, msg_id=None):
    try:
        if msg_id:
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=msg_id, text=msg, parse_mode='Markdown')
            return msg_id
        else:
            sent = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            return sent.message_id
    except Exception as e:
        print(f"⚠️ TG Error: {e}")
        return msg_id

# ==================== 3. DATA ENGINE ====================

async def fetch_indicators():
    """Fetches 500 candles to ensure RSI matches TradingView exactly."""
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 500}
        resp = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        # Wilder's RSI calculation (TV Style)
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        # Signal Line (EMA of RSI)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        print(f"❌ API Error: {e}")
        return None, None, None, None

# ==================== 4. TRADE MONITORING ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    status_updated = False
    
    # 1. Fee Recovery (1.5R)
    if not active_trade['sl_at_recovery'] and price >= (active_trade['entry'] + (risk_dist * 1.5)):
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['sl_at_recovery'] = True
        active_trade['log'] += f"\n🛡️ *1.5x Reached:* SL moved to +0.5R"
        status_updated = True

    # 2. Partial Exit (2.1R)
    if not active_trade['partial_done'] and price >= active_trade['tp']:
        profit_70 = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += profit_70
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5)
        active_trade['log'] += f"\n💰 *Partial Exit:* Banked ${profit_70:.2f}"
        status_updated = True

    if status_updated:
        msg = f"📊 *Live {SYMBOL}*\nPrice: ${price:.2f}\nSL: ${active_trade['sl']:.2f}\n{active_trade['log']}"
        await update_telegram(msg, active_trade['msg_id'])

    # 3. Final Exit
    if price <= active_trade['sl']:
        pnl = (price - active_trade['entry']) / risk_dist * active_trade['risk_usd']
        stats['balance'] += pnl
        result = "✅ WIN" if pnl > 0 else "🛑 LOSS"
        await bot.send_message(chat_id=CHAT_ID, text=f"{result}\nPnL: ${pnl:.2f}\nBal: ${stats['balance']:.2f}")
        active_trade = None

# ==================== 5. MAIN LOOP ====================

async def main():
    global active_trade
    print(f"🚀 Bot Live Painting at {get_ist_now()} IST")
    
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    async with websockets.connect(uri) as ws:
        while True:
            raw_data = await ws.recv()
            data = json.loads(raw_data)
            
            if 'k' in data:
                price = float(data['k']['c'])
                candle_time = format_ist(data['k']['t'])
                
                if active_trade: await monitor_trade(price)

                # LIVE PAINTING: Refresh indicators every 1m or on 15m close
                if data['k']['x']: # This executes every time a 1m candle closes
                    rsi, rsi_ema, prsi, pema = await fetch_indicators()
                    
                    # LOGGING
                    print(f"[{candle_time}] Price: {price} | RSI: {rsi:.2f} | EMA: {rsi_ema:.2f}")
                    
                    # ENTRY LOGIC (Only if no active trade)
                    if not active_trade and rsi and prsi:
                        if prsi <= pema and rsi > rsi_ema: # CROSSOVER
                            # Get 15m Low for SL
                            resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1")
                            low = float(resp.json()[0][3]) * 0.9995
                            
                            risk = stats['balance'] * 0.05
                            active_trade = {
                                'entry': price, 'initial_sl': low, 'sl': low,
                                'tp': price + ((price - low) * 2.1), 'risk_usd': risk,
                                'partial_done': False, 'sl_at_recovery': False,
                                'log': f"🚀 *Entry:* ${price:.2f}"
                            }
                            active_trade['msg_id'] = await update_telegram(active_trade['log'])

if __name__ == "__main__":
    asyncio.run(main())
