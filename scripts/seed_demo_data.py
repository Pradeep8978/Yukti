import asyncio
import random
from datetime import datetime, timedelta
from yukti.data.database import get_db, create_all_tables
from yukti.data.models import Trade, JournalEntry, Position, DailyPerformance
from yukti.execution.order_intent import OrderIntent
from sqlalchemy import delete

async def seed_realistic_data():
    print("🧹 Cleaning old data...")
    async with get_db() as db:
        async with db.begin(): # Single transaction
            await db.execute(delete(DailyPerformance))
            await db.execute(delete(JournalEntry))
            await db.execute(delete(Position))
            await db.execute(delete(Trade))
            await db.execute(delete(OrderIntent))

            print("🚀 Seeding realistic trading history...")
            # 1. Create a series of realistic trades for the last 7 days
            symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "BHARTIARTL"]
            setups = ["EMA_BREAKOUT", "VBPV_REVERSAL", "MACD_CROSSOVER", "SUPPORT_BOUNCE"]
            
            all_trades = []
            for i in range(15): # 15 historical trades
                # Ensure at least one trade is for today (i=0)
                days_ago = 0 if i == 0 else random.randint(1, 10)
                opened_at = datetime.now() - timedelta(days=days_ago, hours=random.randint(9, 15), minutes=random.randint(0, 59))
                closed_at = opened_at + timedelta(hours=random.randint(1, 4))
                
                entry = random.uniform(1000, 4000)
                # 60% win rate
                is_win = random.random() < 0.6
                if is_win:
                    exit_p = entry * (1 + random.uniform(0.01, 0.03))
                else:
                    exit_p = entry * (1 - random.uniform(0.005, 0.015))
                
                pnl_pct = (exit_p - entry) / entry * 100

                t = Trade(
                    symbol=random.choice(symbols),
                    security_id=str(random.randint(1000, 20000)),
                    direction=random.choice(["LONG", "SHORT"]),
                    quantity=random.randint(5, 50),
                    entry_price=entry,
                    exit_price=exit_p,
                    stop_loss=entry * 0.99,
                    target_1=entry * 1.03,
                    risk_reward=3.0,
                    max_loss=500.0,
                    reasoning="Automated setup based on trend confluence and volume profile.",
                    pnl_pct=pnl_pct,
                    status="CLOSED",
                    opened_at=opened_at,
                    closed_at=closed_at,
                    conviction=random.randint(6, 9),
                    setup_type=random.choice(setups),
                    holding_period="intraday",
                    market_bias="BULLISH" if random.random() > 0.4 else "BEARISH"
                )
                db.add(t)
                all_trades.append(t)
            
            await db.flush() # Generate IDs

            # 2. Add Journal Entries for the biggest wins/losses
            for t in sorted(all_trades, key=lambda x: abs(x.pnl_pct or 0), reverse=True)[:5]:
                db.add(JournalEntry(
                    trade_id=t.id,
                    symbol=t.symbol,
                    setup_type=t.setup_type,
                    direction=t.direction,
                    pnl_pct=t.pnl_pct,
                    entry_text=f"The {t.symbol} {t.setup_type} trade was executed with {t.conviction}/10 conviction. "
                               f"Market was showing strong {'bullish' if t.direction=='LONG' else 'bearish'} momentum. "
                               f"Exit was triggered at target levels. Risk management was tight.",
                    created_at=t.closed_at + timedelta(minutes=10)
                ))

            # 3. Aggregate Daily Performance (matching the seeded trades)
            daily_stats = {}
            for t in all_trades:
                d = t.opened_at.date()
                if d not in daily_stats:
                    daily_stats[d] = {"pnl": 0.0, "won": 0, "total": 0}
                daily_stats[d]["pnl"] += (t.pnl_pct or 0) * 100 # Rough rupee pnl for demo
                daily_stats[d]["total"] += 1
                if (t.pnl_pct or 0) > 0:
                    daily_stats[d]["won"] += 1
            
            for d, stats in daily_stats.items():
                db.add(DailyPerformance(
                    date=d,
                    trades_taken=stats["total"],
                    trades_won=stats["won"],
                    trades_lost=stats["total"] - stats["won"],
                    win_rate=stats["won"] / stats["total"],
                    gross_pnl=stats["pnl"],
                    profit_factor=2.0 if stats["won"] > 0 else 0.5
                ))

            # 4. Add 2 ACTIVE positions
            for sym, sec in [("RELIANCE", "2885"), ("TCS", "11536")]:
                intent = OrderIntent(
                    symbol=sym,
                    security_id=sec,
                    direction="LONG",
                    holding_period="intraday",
                    quantity=10,
                    entry_price=2500.0,
                    stop_loss=2475.0,
                    target_1=2560.0,
                    conviction=8,
                    setup_type="EMA_BREAKOUT",
                    reasoning="Consolidation breakout on high volume.",
                    state="ARMED"
                )
                db.add(intent)
                await db.flush()
                
                db.add(Position(
                    symbol=sym,
                    security_id=sec,
                    intent_id=intent.id,
                    direction="LONG",
                    setup_type="EMA_BREAKOUT",
                    holding_period="intraday",
                    entry_price=2500.0,
                    stop_loss=2475.0,
                    target_1=2560.0,
                    quantity=10,
                    risk_reward=2.5,
                    conviction=8,
                    status="OPEN",
                    reasoning="Bullish engulfing candle on 15m timeframe confirmed by RSI cross.",
                    opened_at=datetime.now() - timedelta(minutes=20)
                ))

    print("✅ Realistic demo data seeded!")

if __name__ == "__main__":
    asyncio.run(seed_realistic_data())
