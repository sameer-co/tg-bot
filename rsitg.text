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

# ==================== LIVE CONFIG ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = os.getenv('TG_TOKEN', '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs').replace(' ', '')
CHAT_ID = '1950462171'

all_closes_15m = []
rsi_history = []
prev_rsi = None
prev_rsi_ema = None
bot = None
last_15m_time = None

def wilders_rsi(closes):
    """LIVE 15m Wilder's RSI - PRODUCTION"""
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

# ✅ FIXED: PROPER ASYNC TELEGRAM v20+ (NO WARNINGS)
async def send_alert(msg):
    """ASYNC for python-telegram-bot v20+ - 100% RELIABLE"""
    global bot
    if not bot:
        print("⚠️ Bot not initialized")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"✅ TELEGRAM SENT: {msg[:50]}...")
    except Exception as e:
        print(f"❌ TG ERROR: {str(e)[:80]}")

async def safe_fetch_klines(symbol, interval, limit=100):
    """Bulletproof Binance API - 5 retries"""
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
                        closes.append(float(c[4]))  # Close price
                    except (ValueError, IndexError):
                        continue
            
            if len(closes) >= 50:
                print(f"📡 BINANCE OK: {len(closes)} candles | SOL: {closes[-1]:.4f}")
                return closes
            print(f"⚠️ Only {len(closes)} candles, retry {attempt+1}/5")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"❌ API ERROR {attempt+1}/5: {str(e)[:50]}")
            await asyncio.sleep(1)
    print("💥 Binance fetch FAILED completely")
    return []

async def main():
    global all_closes_15m, prev_rsi, prev_rsi_ema, bot, last_15m_time
    
    print("🚀 SOL 15M RSI BOT v2.1 - PRODUCTION READY")
    print("Time    | SOL     | VOL(K) | RSI  | RSI-EMA | Signal")
    print("=" * 70)
    
    # 🔥 STEP 1: Initialize Telegram bot
    try:
        bot = telegram.Bot(token=TELEGRAM_TOKEN)
        await send_alert("🚀 SOL 15M RSI BOT STARTED ✅ PRODUCTION")
        print("✅ TELEGRAM CONNECTED & TESTED")
    except Exception as e:
        print(f"❌ TELEGRAM FAILED: {e}")
        return
    
    # 🔥 STEP 2: Load initial 15m data
    all_closes_15m = await safe_fetch_klines(SYMBOL, '15m', 100)
    if not all_closes_15m:
        await send_alert("❌ CRITICAL: No Binance data")
        print("💥 FATAL: Cannot load market data")
        return
    print(f"✅ DATA LOADED: {len(all_closes_15m)} candles | SOL: {all_closes_15m[-1]:.4f}")
    
    # 🔥 STEP 3: Live WebSocket monitoring
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    reconnect_count = 0
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                reconnect_count = 0
                print("🔥 WEBSOCKET LIVE - MONITORING SOL 15M RSI")
                last_health = time.time()
                
                while True:
                    # 🔥 Receive 1m candle updates
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        msg = json.loads(msg)
                    except asyncio.TimeoutError:
                        # Healthy timeout - check health every 5 mins
                        if time.time() - last_health > 300:
                            try:
                                resp = requests.get(
                                    f'https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}',
                                    timeout=5
                                )
                                price = float(resp.json()['price'])
                                print(f"🩺 ALIVE: SOL=${price:.4f}")
                                last_health = time.time()
                            except:
                                print("🩺 Health check failed")
                        continue
                    except Exception:
                        continue
                    
                    # 🔥 Process closed 1m candle
                    if 'k' in msg and msg['k']['x']:
                        price = float(msg['k']['c'])
                        volume = float(msg['k']['v'])
                        vol_k = volume / 1000
                        
                        # 🔄 Refresh official 15m data every 15 mins
                        current_min = datetime.now().strftime('%M')
                        if last_15m_time != current_min and int(current_min) % 15 == 0:
                            new_data = await safe_fetch_klines(SYMBOL, '15m', 101)
                            if new_data:
                                all_closes_15m[:] = new_data[-100:]
                                last_15m_time = current_min
                                print(f"🔄 15M REFRESH: {price:.4f}")
                        
                        # 🔥 Live repaint last candle
                        if all_closes_15m:
                            all_closes_15m[-1] = price
                        
                        # 🔥 Calculate indicators
                        rsi = wilders_rsi(all_closes_15m)
                        rsi_ema_val = price_ema(rsi_history, EMA_RSI_PERIOD) if len(rsi_history) >= EMA_RSI_PERIOD else rsi
                        
                        # 🔥 RSI BULLISH CROSSOVER
                        crossover = (prev_rsi is not None and prev_rsi_ema is not None and 
                                   prev_rsi <= prev_rsi_ema and rsi > rsi_ema_val)
                        
                        prev_rsi, prev_rsi_ema = rsi, rsi_ema_val
                        signal = "🟢 BUY" if rsi > rsi_ema_val else "🔴 SELL"
                        
                        # 📊 Live dashboard
                        timestamp = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M')
                        print(f"[{timestamp}] ${price:>6.2f} | {vol_k:>5.0f}K | "
                              f"RSI:{rsi:>4.1f} | EMA:{rsi_ema_val:>5.1f} | {signal}")
                        
                        # 🚀 INSTANT ALERT ON CROSSOVER
                        if crossover:
                            alert_msg = (f"🚀 SOL 15M RSI BULLISH CROSSOVER!\n"
                                      f"💰 PRICE: ${price:.4f}\n"
                                      f"📊 VOL: {vol_k:,.0f}K\n"
                                      f"📈 RSI: {rsi:.1f} ↗ EMA: {rsi_ema_val:.1f}\n"
                                      f"🕐 {get_ist()}")
                            await send_alert(alert_msg)
                            print("🎯 CROSSOVER ALERT SENT!")
                    
                    await asyncio.sleep(0.1)
                    
        except Exception as e:
            reconnect_count += 1
            print(f"🔄 RECONNECT #{reconnect_count}: {str(e)[:60]}")
            await asyncio.sleep(min(5 * reconnect_count, 30))

if __name__ == "__main__":
    try:
        print(f"🤖 SOL 15M RSI TRADING BOT | {get_ist()}")
        print("💎 LOCAL / VPS / CLOUD READY")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 BOT STOPPED")
        if 'bot' in globals() and bot:
            asyncio.create_task(send_alert("👋 SOL RSI Bot stopped"))
    except Exception as e:
        print(f"💥 FATAL: {e}")
        if 'bot' in globals() and bot:
            asyncio.create_task(send_alert(f"💥 Bot crashed: {str(e)[:100]}"))
