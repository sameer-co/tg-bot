import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta
import logging
import sys

# ==================== LOGGING & CONFIG ====================
# (Logging setup remains the same as your original)
logger = logging.getLogger("BotEngine")
# ... [Keep your existing logging setup here] ...

SYMBOL = 'SOLUSDT'
TELEGRAM_TOKEN = 'YOUR_TOKEN'
CHAT_ID = 'YOUR_ID'

stats = {
    "balance": 1000,
    "risk_percent": 0.02,
    "total_trades": 0,
    "wins_target": 0,   # Hits 1.85R Total
    "wins_trailed": 0,  # Hits 1.15R Total
    "losses": 0         # Hits -1.0R Total
}

active_trade = None
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== DATA ENGINE ====================
async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        rsi = ta.rsi(df['close'], length=14)
        rsi_ema = ta.ema(rsi, length=9)
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        logger.error(f"FETCH_ERROR: {str(e)}")
        return None, None, None, None

# ==================== TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0

    # 1. PARTIAL TAKE PROFIT & TRAIL (Triggered at 1.5R)
    if not active_trade['half_closed'] and rr_ratio >= 1.5:
        active_trade['half_closed'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.8) # Move SL to +0.8R
        
        msg = f"⚡ *PARTIAL EXIT (50%)* at 1.5R\nMoving SL to +0.8R"
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
        logger.info("PARTIAL_EXIT: 50% closed at 1.5R, SL trailed to 0.8R")

    # 2. FINAL EXIT: Target (2.2R) or Trailing SL hit
    if rr_ratio >= 2.2:
        await close_trade(price, "🎯 TARGET HIT (Full Setup 1.85R)")
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILING WIN (Full Setup 1.15R)" if active_trade['half_closed'] else "🛑 STOP LOSS"
        await close_trade(price, reason)

async def close_trade(exit_price, reason):
    global active_trade, stats
    
    risk_usd = active_trade['risk_usd']
    
    # Calculate Total PnL based on your new math
    if "TARGET" in reason:
        # 50% at 1.5R + 50% at 2.2R = 1.85R total
        pnl = risk_usd * 1.85
        stats['wins_target'] += 1
    elif "TRAILING" in reason:
        # 50% at 1.5R + 50% at 0.8R = 1.15R total
        pnl = risk_usd * 1.15
        stats['wins_trailed'] += 1
    else:
        # Full loss before reaching 1.5R
        pnl = -risk_usd
        stats['losses'] += 1

    stats['balance'] += pnl
    stats['total_trades'] += 1
    win_rate = ((stats['wins_target'] + stats['wins_trailed']) / stats['total_trades']) * 100

    exit_msg = (
        f"🏁 *TRADE CLOSED*\n{reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *Total PnL:* `{pnl:+.2f} USDT`\n"
        f"🏦 *New Balance:* `${stats['balance']:.2f}`\n\n"
        f"📊 *Stats:* 🎯:`{stats['wins_target']}` | 🛡️:`{stats['wins_trailed']}` | 🛑:`{stats['losses']}`\n"
        f"📈 Win Rate: `{win_rate:.1f}%`"
    )
    await bot.send_message(chat_id=CHAT_ID, text=exit_msg, parse_mode='Markdown')
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info("SYSTEM_BOOT: Bot Online with Partial TP Logic.")
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                while True:
                    data = json.loads(await ws.recv())
                    if 'k' in data:
                        price = float(data['k']['c'])
                        if active_trade: await monitor_trade(price)
                        
                        if data['k']['x']: # Candle Close logic
                            rsi, rsi_ema, prsi, pema = await fetch_indicators()
                            if rsi and not active_trade:
                                if prsi <= pema and rsi > rsi_ema:
                                    # Fetch 15m candle low for SL
                                    resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                    low_price = float(resp[0][3]) * 0.9995
                                    
                                    active_trade = {
                                        'entry': price, 'sl': low_price, 'initial_sl': low_price,
                                        'risk_usd': stats['balance'] * stats['risk_percent'],
                                        'half_closed': False 
                                    }
                                    # [Telegram Entry Message code here...]
        except Exception as e:
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
