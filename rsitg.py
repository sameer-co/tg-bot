import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta
import logging
import sys

# ==================== LOGGING SETUP ====================
class RailwayJSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        })

logger = logging.getLogger("BotEngine")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(RailwayJSONFormatter())
logger.addHandler(console_handler)

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
TELEGRAM_TOKEN = '7669372307:AAGyLdhMomWfKEoYSDVqvYs2FLn1mCIFhHs'
CHAT_ID = '1950462171'

# Stats and Capital Tracking
stats = {
    "balance": 1000.0, 
    "risk_percent": 0.02, # 2% risk
    "wins": 0, 
    "losses": 0, 
    "total_trades": 0
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
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        logger.error(f"FETCH_ERROR: {str(e)}")
        return None, None, None, None

# ==================== TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade
    if not active_trade: return

    # Calculate Current Risk/Reward (RR) Ratio
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0

    # 1. TRAILING LOGIC: Price hit 1.5R -> Move SL to +0.5R
    if not active_trade['sl_trailed'] and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['sl_trailed'] = True
        logger.info(f"TRAIL: SL moved to +0.5R at {active_trade['sl']:.2f}")

    # 2. EXIT LOGIC: Target (2.2R) or current SL hit
    if rr_ratio >= 2.2:
        await close_trade(price, "🎯 TARGET HIT (2.2R)")
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILING STOP (+0.5R)" if active_trade['sl_trailed'] else "🛑 STOP LOSS"
        await close_trade(price, reason)

async def close_trade(exit_price, reason):
    global active_trade, stats
    
    # Calculate PnL based on the original dollar risk
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    achieved_rr = (exit_price - active_trade['entry']) / risk_dist
    pnl = achieved_rr * active_trade['risk_usd']
    
    stats['balance'] += pnl
    stats['total_trades'] += 1
    if pnl > 0: stats['wins'] += 1
    else: stats['losses'] += 1
    
    win_rate = (stats['wins'] / stats['total_trades']) * 100
    
    exit_msg = (
        f"🏁 *TRADE CLOSED: {reason}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Entry:* `${active_trade['entry']:.2f}`\n"
        f"🏁 *Exit:* `${exit_price:.2f}`\n"
        f"💵 *PnL:* `{pnl:+.2f} USDT`\n"
        f"🏦 *New Balance:* `${stats['balance']:.2f}`\n\n"
        f"📊 *Lifetime Stats:*\n"
        f"✅ Wins: `{stats['wins']}` | 🛑 Losses: `{stats['losses']}`\n"
        f"📈 Win Rate: `{win_rate:.1f}%`"
    )
    await bot.send_message(chat_id=CHAT_ID, text=exit_msg, parse_mode='Markdown')
    logger.info(f"CLOSED: {reason} | PnL: {pnl:.2f}")
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info("SYSTEM_BOOT: Bot Online. Capital: $1000, Risk: 2% ($20)")
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
                                    # Setup Parameters
                                    resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                    low_price = float(resp[0][3]) * 0.9995 
                                    tp_price = price + ((price - low_price) * 2.2)
                                    risk_amount = stats['balance'] * stats['risk_percent'] # $20
                                    
                                    active_trade = {
                                        'entry': price, 'initial_sl': low_price, 'sl': low_price,
                                        'tp': tp_price, 'risk_usd': risk_amount, 'sl_trailed': False
                                    }
                                    
                                    entry_msg = (
                                        f"🚀 *LONG SIGNAL: {SYMBOL}*\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"💰 *Entry:* `${price:.2f}`\n"
                                        f"🎯 *Target (2.2R):* `${tp_price:.2f}`\n"
                                        f"🛑 *Stop Loss:* `${low_price:.2f}`\n"
                                        f"📏 *Risk per Trade:* `${risk_amount:.2f}`\n\n"
                                        f"📊 *Current Stats:*\n"
                                        f"✅ Wins: `{stats['wins']}` | 🛑 Losses: `{stats['losses']}`"
                                    )
                                    await bot.send_message(chat_id=CHAT_ID, text=entry_msg, parse_mode='Markdown')
                                    logger.info(f"SIGNAL_OPENED: TP={tp_price:.2f}")

        except Exception as e:
            logger.error(f"RECONNECTING: {str(e)}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
