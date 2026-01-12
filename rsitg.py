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

# ==================== 0. LOGGING CONFIGURATION ====================
# This sets up the console output format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==================== 1. CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs'
CHAT_ID = '1950462171'

IST = pytz.timezone('Asia/Kolkata')
stats = {"balance": 1000.0, "wins": 0, "losses": 0, "total_trades": 0}

active_trade = None  
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== 2. UTILS ====================

def get_ist_now():
    return datetime.now(IST).strftime('%H:%M:%S')

async def update_telegram(msg, msg_id=None):
    try:
        if msg_id:
            await bot.edit_message_text(chat_id=CHAT_ID, message_id=msg_id, text=msg, parse_mode='Markdown')
            return msg_id
        else:
            sent = await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')
            return sent.message_id
    except Exception as e:
        logger.error(f"❌ Telegram Error: {e}")
        return msg_id

# ==================== 3. DATA ENGINE ====================

async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        
        if resp.status_code != 200:
            logger.error(f"Binance API returned status {resp.status_code}")
            return None, None, None, None

        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        
        curr_rsi, curr_ema = rsi.iloc[-1], rsi_ema.iloc[-1]
        prev_rsi, prev_ema = rsi.iloc[-2], rsi_ema.iloc[-2]
        
        logger.info(f"🔍 RSI: {curr_rsi:.2f} | EMA: {curr_ema:.2f}")
        return curr_rsi, curr_ema, prev_rsi, prev_ema
    except Exception as e:
        logger.error(f"Error fetching indicators: {e}")
        return None, None, None, None

# ==================== 4. TRADE MONITORING ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist != 0 else 0
    pct_change = (reward_dist / active_trade['entry']) * 100
    
    status_updated = False
    
    # 1. Fee Recovery (1.5R)
    if not active_trade['sl_at_recovery'] and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['sl_at_recovery'] = True
        active_trade['log'] += f"\n🛡️ *SL moved to +0.5R*"
        logger.info(f"🛡️ Risk managed: SL moved to {active_trade['sl']:.2f}")
        status_updated = True

    # 2. Partial Exit (2.1R)
    if not active_trade['partial_done'] and rr_ratio >= 2.1:
        profit_70 = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += profit_70
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5)
        active_trade['last_trail_price'] = price 
        active_trade['log'] += f"\n💰 *Partial Exit:* Banked 70% (${profit_70:.2f})"
        logger.info(f"💰 Partial Profit Taken: Banked ${profit_70:.2f}")
        status_updated = True

    # 3. Dynamic Trailing
    if active_trade.get('partial_done'):
        if price >= (active_trade['last_trail_price'] * 1.0040):
            active_trade['sl'] *= 1.0020
            active_trade['last_trail_price'] = price
            active_trade['log'] += f"\n📈 *Trail:* SL Up 0.20%"
            logger.info(f"📈 Trailing SL up to: {active_trade['sl']:.2f}")
            status_updated = True

    # Dashboard Update Logic
    if status_updated or abs(price - active_trade.get('last_msg_price', 0)) > (price * 0.002):
        active_trade['last_msg_price'] = price
        win_rate = (stats['wins'] / stats['total_trades'] * 100) if stats['total_trades'] > 0 else 0
        
        msg = (f"📊 *Live {SYMBOL} Dashboard*\n"
               f"━━━━━━━━━━━━━━\n"
               f"💵 *Price:* `${price:.2f}`\n"
               f"🛑 *SL:* `${active_trade['sl']:.2f}` | 🎯 *TP:* `${active_trade['tp']:.2f}`\n"
               f"⚖️ *RR:* `{rr_ratio:.2f}R` | 📈 *P/L:* `{pct_change:+.2f}%` \n"
               f"━━━━━━━━━━━━━━\n"
               f"🏆 *Wins:* `{stats['wins']}` | ❌ *Losses:* `{stats['losses']}`\n"
               f"📊 *Win Rate:* `{win_rate:.1f}%` | 💰 *Bal:* `${stats['balance']:.2f}`\n"
               f"━━━━━━━━━━━━━━\n"
               f"{active_trade['log']}")
        await update_telegram(msg, active_trade['msg_id'])

    # 4. Final Exit
    if price <= active_trade['sl']:
        pnl_rem = ((price - active_trade['entry']) / risk_dist * active_trade['risk_usd']) * 0.30
        stats['balance'] += pnl_rem
        stats['total_trades'] += 1
        
        if price > active_trade['entry']:
            stats['wins'] += 1
            result_tag = "✅ PROFIT"
        else:
            stats['losses'] += 1
            result_tag = "🛑 STOPPED"

        logger.info(f"🏁 Trade Closed: {result_tag} | Final Balance: ${stats['balance']:.2f}")

        exit_msg = (f"🏁 *{result_tag}*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"⚖️ *Final RR:* `{rr_ratio:.2f}R`\n"
                    f"💵 *Rem. PnL:* `${pnl_rem:+.2f}`\n"
                    f"🏆 *Total Wins:* `{stats['wins']}` | ❌ *Losses:* `{stats['losses']}`\n"
                    f"💰 *Final Wallet:* `${stats['balance']:.2f}`")
        
        await bot.send_message(chat_id=CHAT_ID, text=exit_msg, parse_mode='Markdown')
        active_trade = None

# ==================== 5. MAIN LOOP ====================

async def main():
    global active_trade
    logger.info(f"🚀 Bot Starting... Monitoring {SYMBOL} on 15m RSI/EMA crossover.")
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    async with websockets.connect(uri) as ws:
        while True:
            try:
                data = json.loads(await ws.recv())
                if 'k' in data:
                    price = float(data['k']['c'])
                    
                    if active_trade:
                        await monitor_trade(price)
                    
                    if data['k']['x']: # New 1m candle closed
                        logger.info(f"⏰ 1m Candle Closed. Price: {price}. Checking Signal...")
                        rsi, rsi_ema, prsi, pema = await fetch_indicators()
                        
                        if not active_trade and rsi and prsi:
                            # Check for Bullish Crossover
                            if prsi <= pema and rsi > rsi_ema:
                                logger.info("⚡ BULLISH CROSSOVER DETECTED! Opening Position...")
                                
                                # Fetch 15m low for SL
                                resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                low = float(resp[0][3]) * 0.9995
                                risk = stats['balance'] * 0.05
                                
                                active_trade = {
                                    'entry': price, 'initial_sl': low, 'sl': low,
                                    'tp': price + ((price - low) * 2.1), 'risk_usd': risk,
                                    'partial_done': False, 'sl_at_recovery': False,
                                    'log': f"🚀 *Entry:* `${price:.2f}`", 'last_msg_price': price
                                }
                                active_trade['msg_id'] = await update_telegram("⏳ Opening Trade...")
                                logger.info(f"✅ Trade Opened at {price} | SL: {low:.2f} | Risk: ${risk:.2f}")
            except Exception as e:
                logger.error(f"Main Loop Error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
