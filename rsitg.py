import asyncio
import websockets
import json
import telegram
import requests
import numpy as np
from datetime import datetime
import pytz
import time
import nest_asyncio
import os

# ✅ FIXED: Enable nested event loops for Railway
nest_asyncio.apply()

# ==================== LIVE CONFIG ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = os.getenv('TG_TOKEN', '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs').replace(' ', '')  # ✅ Clean token
CHAT_ID = '1950462171'

all_closes_15m = []
rsi_history = []
prev_rsi = None
prev_rsi_ema = None
bot = None
last_15m_time = None


def wilders_rsi(closes):
    """LIVE 15m Wilder's RSI"""
    global rsi_history
    if len(closes) < RSI_PERIOD + 1:
        return 50.0
    
    rsi_history.clear()
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    
    avg_gain = np.mean(gains[:RSI_PERIOD])
    avg_loss = np.mean(losses[:RSI_PERIOD])
    
    for i in range(RSI_PERIOD, len(closes)):
        change = closes[i] - closes[i-1]
        gain = max(change, 0)
        loss = max(-change, 0)
        
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
        
        rs = avg_gain / avg_loss if avg_loss > 0 else 0
        rsi = 100 - (100 / (1 + rs))
        rsi_history.append(rsi)
    
    return rsi_history[-1]


def price_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    alpha = 2 / (period + 1)
    ema = np.mean(closes[:period])
    for i in range(period, len(closes)):
        ema = alpha * closes[i] + (1 - alpha) * ema
    return ema


def get_ist():
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S IST')


async def send_alert(msg):
    """✅ FIXED: Proper async Telegram sending"""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: bot.send_message(chat_id=CHAT_ID, text=msg))
        print(f"✅ ALERT SENT: {msg[:50]}...")
    except Exception as e:
        print(f"❌ TG FAIL: {str(e)[:80]}")


async def safe_fetch_klines(symbol, interval, limit=100):
    """✅ NEW: Bulletproof Binance data fetch"""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = requests.get('https://api.binance.com/api/v3/klines',
                              params={'symbol': symbol, 'interval': interval, 'limit': limit},
                              timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            closes = []
            for c in data:
                if isinstance(c, (list, tuple)) and len(c) >= 6:
                    try:
                        closes.append(float(c[4]))  # Close price index 4
                    except (ValueError, IndexError):
                        continue
            
            if len(closes) >= 50:
                return closes
            else:
                print(f"⚠️ Only {len(closes)} valid candles (need 50+), retry {attempt+1}/{max_retries}")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"❌ Fetch failed (attempt {attempt+1}): {e}")
            await asyncio.sleep(2)
    return []


async def main():
    global all_closes_15m, prev_rsi, prev_rsi_ema, bot, last_15m_time
    
    print("🚀 LIVE SOL 15M RSI BOT - PRODUCTION READY (Railway Fixed)")
    print("Time    | SOL    | VOL(K) | RSI  | RSIEMA | 9EMA   | 21EMA  | 50EMA  | Signal")
    print("-" * 85)
    
    # ✅ FIXED: Bot init FIRST, outside loops
    try:
        bot = telegram.Bot(token=TELEGRAM_TOKEN)
        await send_alert("🤖 SOL 15M RSI BOT STARTED - Railway Deployed!")
        print("✅ Telegram connected")
    except Exception as e:
        print(f"❌ Telegram init failed: {e}")
        return
    
    # ✅ FIXED: Safe initial data load
    all_closes_15m = await safe_fetch_klines(SYMBOL, '15m', 100)
    if not all_closes_15m:
        print("💥 FATAL: Cannot load initial data")
        return
    print(f"✅ LOADED {len(all_closes_15m)} candles | SOL: {all_closes_15m[-1]:.4f}")
    
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    reconnect_count = 0
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                reconnect_count = 0
                print("🔥 LIVE WEBSOCKET CONNECTED - 15m REPAINTING")
                
                last_health = time.time()
                
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    
                    if 'k' in msg and msg['k']['x']:  # 1m candle closed
                        price = float(msg['k']['c'])
                        volume = float(msg['k']['v'])
                        vol_thousands = volume / 1000
                        
                        # 🔄 NEW 15m CANDLE REFRESH (xx:00, xx:15, xx:30, xx:45)
                        current_15m_time = datetime.now().strftime('%M')
                        if last_15m_time != current_15m_time and int(current_15m_time) % 15 == 0:
                            new_data = await safe_fetch_klines(SYMBOL, '15m', 101)
                            if new_data:
                                all_closes_15m[:] = new_data[-100:]
                                last_15m_time = current_15m_time
                                print(f"🔄 NEW 15m CANDLE: {price:.4f}")
                        
                        # REPAINT last 15m candle with live price
                        if all_closes_15m:
                            all_closes_15m[-1] = price
                        
                        # LIVE calculations
                        rsi = wilders_rsi(all_closes_15m)
                        rsi_ema = price_ema(rsi_history, EMA_RSI_PERIOD) if len(rsi_history) >= EMA_RSI_PERIOD else rsi
                        ema9 = price_ema(all_closes_15m, 9)
                        ema21 = price_ema(all_closes_15m, 21)
                        ema50 = price_ema(all_closes_15m, 50)
                        
                        # RSI CROSSOVER detection
                        crossover = (prev_rsi is not None and prev_rsi_ema is not None and 
                                   prev_rsi <= prev_rsi_ema and rsi > rsi_ema)
                        
                        prev_rsi, prev_rsi_ema = rsi, rsi_ema
                        signal = "🟢 GREEN" if rsi > rsi_ema else "🔴 RED"
                        
                        # 🩺 HEALTH CHECK every 5 mins
                        if time.time() - last_health > 300:
                            try:
                                resp = requests.get(f'https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}', timeout=10)
                                health_price = float(resp.json()['price'])
                                print(f"🩺 HEALTH OK: SOL=${health_price:.4f}")
                                last_health = time.time()
                            except:
                                print("🩺 HEALTH FAIL")
                        
                        # 📊 TABLE OUTPUT
                        timestamp = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M')
                        print(f"[{timestamp}] {price:>7.4f} | {vol_thousands:>6.0f}K | "
                              f"{rsi:>4.1f} | {rsi_ema:>6.1f} | {ema9:>6.4f} | "
                              f"{ema21:>6.4f} | {ema50:>6.4f} | {signal}")
                        
                        # 🚀 ALERT ON CROSSOVER
                        if crossover:
                            alert_msg = (f"🚀 SOL 15M RSI CROSSOVER!\n"
                                      f"💰 SOL: ${price:.4f}\n"
                                      f"📊 VOL: {vol_thousands:,.0f}K\n"
                                      f"📈 RSI: {rsi:.1f} > RSIEMA: {rsi_ema:.1f}\n"
                                      f"📉 9EMA: ${ema9:.4f} | 21EMA: ${ema21:.4f}\n"
                                      f"🕐 {get_ist()}")
                            await send_alert(alert_msg)
                            print("🎯 CROSSOVER ALERT SENT!")
                    
                    await asyncio.sleep(0.1)
                    
        except Exception as e:
            reconnect_count += 1
            print(f"🔄 RECONNECT #{reconnect_count}: {str(e)[:80]}")
            await asyncio.sleep(min(5 * reconnect_count, 30))  # Progressive backoff


if __name__ == "__main__":
    try:
        print(f"🤖 Starting SOL RSI Bot | {get_ist()}")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 PRODUCTION BOT STOPPED")
    except Exception as e:
        print(f"💥 FATAL ERROR: {e}")
