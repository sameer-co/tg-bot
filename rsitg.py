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
    """Formats logs as single-line JSON for Railway's Dashboard."""
    def format(self, record):
        log_entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
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

IST = pytz.timezone('Asia/Kolkata')
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

# ==================== 3. INDICATOR ENGINE ====================

async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '15m', 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        
        if resp.status_code != 200:
            logger.error(f"BINANCE_API_ERROR: {resp.status_code}")
            return None, None, None, None

        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        
        curr_rsi, curr_ema = rsi.iloc[-1], rsi_ema.iloc[-1]
        prev_rsi, prev_ema = rsi.iloc[-2], rsi_ema.iloc[-2]
        
        logger.info(f"DATA_TICK: RSI={curr_rsi:.2f}, EMA={curr_ema:.2f}")
        return curr_rsi, curr_ema, prev_rsi, prev_ema
    except Exception as e:
        logger.error(f"CALCULATION_ERROR: {str(e)}")
        return None, None, None, None

# ==================== 4. TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist != 0 else 0
    
    # Logic 1: SL to 0.5R (Protection)
    if not active_trade['sl_at_recovery'] and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['sl_at_recovery'] = True
        logger.info(f"RISK_MGMT: Trailed SL to +0.5R at {active_trade['sl']:.2f}")

    # Logic 2: Partial Exit at 2.1R
    if not active_trade['partial_done'] and rr_ratio >= 2.1:
        profit_70 = (active_trade['risk_usd'] * 2.1) * 0.70
        stats['balance'] += profit_70
        active_trade['partial_done'] = True
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.5)
        active_trade['last_trail_price'] = price
        logger.info(f"PARTIAL_EXIT: Banked 70% profit (${profit_70:.2f})")

    # Final Exit Check
    if price <= active_trade['sl']:
        pnl_rem = ((price - active_trade['entry']) / risk_dist * active_trade['risk_usd']) * 0.30
        stats['balance'] += pnl_rem
        stats['total_trades'] += 1
        
        win = price > active_trade['entry']
        stats['wins' if win else 'losses'] += 1
        
        logger.info(f"TRADE_CLOSED: Result={'PROFIT' if win else 'LOSS'} | Final Balance: ${stats['balance']:.2f}")
        await bot.send_message(chat_id=CHAT_ID, text=f"🏁 *Trade Closed*\nResult: {'✅ WIN' if win else '🛑 STOP'}\nBalance: `${stats['balance']:.2f}`")
        active_trade = None

# ==================== 5. MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info(f"SYSTEM_START: Monitoring {SYMBOL} crossover strategy.")
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    
    last_heartbeat = datetime.now()

    while True: # Outer loop for reconnection
        try:
            async with websockets.connect(uri) as ws:
                logger.info("WEBSOCKET_CONNECTED: Stream active.")
                while True:
                    data = json.loads(await ws.recv())
                    if 'k' in data:
                        price = float(data['k']['c'])
                        if active_trade: await monitor_trade(price)
                        
                        # Log status every 10 mins
                        if (datetime.now() - last_heartbeat).seconds > 600:
                            logger.info(f"HEARTBEAT: Bot alive. Current Price: {price}")
                            last_heartbeat = datetime.now()

                        # Check signal on 1m candle close
                        if data['k']['x']:
                            rsi, rsi_ema, prsi, pema = await fetch_indicators()
                            if not active_trade and rsi and prsi:
                                if prsi <= pema and rsi > rsi_ema:
                                    logger.info(f"SIGNAL_DETECTED: Bullish Crossover at {price}")
                                    
                                    # SL calculation logic (using recent low)
                                    resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                    low = float(resp[0][3]) * 0.9995
                                    risk = stats['balance'] * 0.05
                                    
                                    active_trade = {
                                        'entry': price, 'initial_sl': low, 'sl': low,
                                        'tp': price + ((price - low) * 2.1), 'risk_usd': risk,
                                        'partial_done': False, 'sl_at_recovery': False,
                                        'msg_id': await update_telegram(f"🚀 *Long Entry:* `{price}`\nSL: `{low:.2f}`")
                                    }
                                    logger.info(f"TRADE_OPENED: Entry={price}, SL={low:.2f}")

        except Exception as e:
            logger.error(f"CONNECTION_LOST: Reconnecting in 10s... Error: {str(e)}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("SYSTEM_SHUTDOWN: User stopped the bot.")
