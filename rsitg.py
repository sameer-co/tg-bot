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

# YOUR CREDENTIALS INTEGRATED
TELEGRAM_TOKEN = '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE'
CHAT_ID = '1950462171'

stats = {
    "balance": 100, 
    "risk_percent": 0.02,
    "total_trades": 0,
    "wins_target": 0,   # Full 1.85R Wins
    "wins_trailed": 0,  # Partial 1.15R Wins
    "losses": 0        # -1.0R Losses
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

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0

    # 1. PARTIAL TP & TRAILING: Hit 1.5R -> Move SL to +0.8R & mark half closed
    if not active_trade['half_closed'] and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.8)
        active_trade['half_closed'] = True
        
        trail_msg = (
            f"⚡ *PARTIAL EXIT (50%) hit 1.5R*\n"
            f"🛡️ SL moved to +0.8R zone: `${active_trade['sl']:.2f}`\n"
            f"🔒 Trade is now secured."
        )
        await bot.send_message(chat_id=CHAT_ID, text=trail_msg, parse_mode='Markdown')
        logger.info(f"TRAIL: Half closed, SL to +0.8R at {active_trade['sl']:.2f}")

    # 2. EXIT LOGIC: Target (2.2R) or current SL hit
    if rr_ratio >= 2.2:
        await close_trade(price, "🎯 TARGET HIT (Full 1.85R)")
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILING WIN (Full 1.15R)" if active_trade['half_closed'] else "🛑 STOP LOSS"
        await close_trade(price, reason)

async def close_trade(exit_price, reason):
    global active_trade, stats
    
    # Calculate PnL based on the specific scenario
    risk_usd = active_trade['risk_usd']
    
    if "TARGET" in reason:
        pnl = risk_usd * 1.85
        stats['wins_target'] += 1
    elif "TRAILING" in reason:
        pnl = risk_usd * 1.15
        stats['wins_trailed'] += 1
    else:
        pnl = -risk_usd
        stats['losses'] += 1
    
    stats['balance'] += pnl
    stats['total_trades'] += 1
    win_rate = ((stats['wins_target'] + stats['wins_trailed']) / stats['total_trades']) * 100
    
    exit_msg = (
        f"🏁 *TRADE CLOSED: {reason}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *PnL:* `{pnl:+.2f} USDT`\n"
        f"🏦 *New Balance:* `${stats['balance']:.2f}`\n\n"
        f"📊 *Stats:*\n"
        f"🎯 Full Target: `{stats['wins_target']}`\n"
        f"🛡️ Trailed: `{stats['wins_trailed']}`\n"
        f"🛑 Losses: `{stats['losses']}`\n"
        f"📈 Win Rate: `{win_rate:.1f}%`"
    )
    await bot.send_message(chat_id=CHAT_ID, text=exit_msg, parse_mode='Markdown')
    logger.info(f"CLOSED: {reason} | PnL: {pnl:.2f}")
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info("SYSTEM_BOOT: Bot Online. Strategy: RSI EMA + Partial TP")
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
                                    resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=1").json()
                                    low_price = float(resp[0][3]) * 0.9995 
                                    risk_amount = stats['balance'] * stats['risk_percent']
                                    
                                    active_trade = {
                                        'entry': price, 'initial_sl': low_price, 'sl': low_price,
                                        'risk_usd': risk_amount, 'half_closed': False
                                    }
                                    
                                    entry_msg = (
                                        f"🚀 *LONG SIGNAL: {SYMBOL}*\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"💰 *Entry:* `${price:.2f}`\n"
                                        f"🛑 *Stop:* `${low_price:.2f}`\n"
                                        f"📏 *Risk:* `${risk_amount:.2f}`"
                                    )
                                    await bot.send_message(chat_id=CHAT_ID, text=entry_msg, parse_mode='Markdown')
                                    logger.info(f"SIGNAL_OPENED: Entry={price}")

        except Exception as e:
            logger.error(f"RECONNECTING: {str(e)}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
