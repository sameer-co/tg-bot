import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime
import pytz
import logging
import sys

# ==================== 0. RAILWAY LOGGING SETUP ====================
class RailwayJSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        return json.dumps(log_entry)

logger = logging.getLogger("BotEngine")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(RailwayJSONFormatter())
logger.addHandler(console_handler)

# ==================== 1. CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs'
CHAT_ID = '1950462171'

stats = {"balance": 1000.0, "wins": 0, "losses": 0, "total_trades": 0}
active_trade = None  
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== 2. UTILS ====================

async def update_telegram(msg, msg_id=None):
    try:
        if msg_id:
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=msg_id, text=msg, parse_mode='Markdown')
            return msg_id
        else:
            sent = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            return sent.message_id
    except Exception as e:
        logger.error(f"TELEGRAM_ERROR: {str(e)}")
        return msg_id

# ==================== 3. DATA ENGINE ====================

async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        logger.error(f"FETCH_ERROR: {str(e)}")
        return None, None, None, None

# ==================== 4. TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist != 0 else 0
    
    # Logic: Move SL to protection and Partial Exit
    if not active_trade['sl_at_recovery'] and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['sl_at_recovery'] = True
        logger.info("SL moved to +0.5R")

    if not active_trade['partial_done'] and rr_ratio >= 2.1:
        profit_70 = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += profit_70
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5)
        logger.info("Partial exit 70% completed.")

    # EXIT TRIGGER
    if price <= active_trade['sl']:
        # Final PnL Calculation
        pnl_rem = ((price - active_trade['entry']) / risk_dist * active_trade['risk_usd']) * 0.30
        stats['balance'] += pnl_rem
        stats['total_trades'] += 1
        
        is_win = price > active_trade['entry']
        if is_win: stats['wins'] += 1
        else: stats['losses'] += 1
        
        win_rate = (stats['wins'] / stats['total_trades']) * 100
        result_icon = "✅ PROFIT" if is_win else "🛑 LOSS/STOP"

        # --- TELEGRAM RESULT MESSAGE ---
        result_msg = (
            f"{result_icon} *Trade Result Summary*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Entry:* `${active_trade['entry']:.2f}`\n"
            f"🏁 *Exit:* `${price:.2f}`\n"
            f"🎯 *Initial Target:* `${active_trade['tp']:.2f}`\n"
            f"💵 *Final Balance:* `${stats['balance']:.2f}`\n\n"
            f"📊 *Bot Statistics:*\n"
            f"Wins: `{stats['wins']}` | Losses: `{stats['losses']}`\n"
            f"Win Rate: `{win_rate:.1f}%`"
        )
        
        await bot.send_message(chat_id=CHAT_ID, text=result_msg, parse_mode='Markdown')
        logger.info(f"TRADE_CLOSED: Result={result_icon}")
        active_trade = None

# ==================== 5. MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info("SYSTEM_BOOT: Bot Online.")
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                while True:
                    data = json.loads(await ws.recv())
                    if 'k' in data:
                        price = float(data['k']['c'])
                        if active_trade: await monitor_trade(price)
                        
                        if data['k']['x']: # Candle Close
                            rsi, rsi_ema, prsi, pema = await fetch_indicators()
                            if rsi and not active_trade:
                                if prsi <= pema and rsi > rsi_ema:
                                    # Setup SL and TP
                                    resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                    low = float(resp[0][3]) * 0.9995
                                    tp = price + ((price - low) * 2.1)
                                    risk = stats['balance'] * 0.05
                                    
                                    active_trade = {
                                        'entry': price, 'initial_sl': low, 'sl': low,
                                        'tp': tp, 'risk_usd': risk,
                                        'partial_done': False, 'sl_at_recovery': False
                                    }
                                    
                                    entry_msg = (
                                        f"🚀 *LONG SIGNAL DETECTED*\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"💰 *Entry:* `${price:.2f}`\n"
                                        f"🎯 *Target (TP):* `${tp:.2f}`\n"
                                        f"🛑 *Stop Loss (SL):* `${low:.2f}`"
                                    )
                                    await update_telegram(entry_msg)
                                    logger.info(f"SIGNAL_OPENED: TP={tp:.2f}")

        except Exception as e:
            logger.error(f"RECONNECTING: {str(e)}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
