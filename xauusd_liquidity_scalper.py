"""
╔══════════════════════════════════════════════════════════╗
║   XAUUSD Liquidity Scalper — 1 Second Timeframe          ║
║   Strategy : Liquidity Sweep + Fade                      ║
║   Exit     : Signal-based (fast, seconds)                ║
║   Risk     : Fixed lot size, no SL/TP                    ║
╚══════════════════════════════════════════════════════════╝

HOW IT WORKS:
─────────────
Liquidity scalping targets "liquidity pools" — price levels
where many stop-losses cluster (just above recent swing highs
or just below recent swing lows).

Smart money sweeps these levels to grab liquidity, then
reverses. This bot detects those sweeps and fades them:

  • Price spikes ABOVE recent high  →  short (SELL) the sweep
  • Price spikes BELOW recent low   →  long  (BUY)  the sweep

Exit: When price returns to a mid-zone (EMA) or an
      opposite sweep forms.

SETUP:
──────
  1. pip install MetaTrader5 pandas numpy
  2. Open MetaTrader 5 (Windows only), log in to broker
  3. Edit the SETTINGS section below
  4. Run: python xauusd_liquidity_scalper.py

⚠️  WARNING: Scalping with no SL/TP is HIGH RISK.
    Use a demo account first and understand the strategy
    before trading live money.
"""

import time
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime

# ══════════════════════════════════════════════
#  ⚙️  SETTINGS
# ══════════════════════════════════════════════
SYMBOL          = "XAUUSD"
TIMEFRAME       = mt5.TIMEFRAME_M1     # MT5 has no native 1s TF; we use tick data for 1s logic
LOT_SIZE        = 0.01                 # Micro lot — start small!
MAGIC           = 999001               # Bot ID (don't change mid-run)

LOOKBACK        = 20                   # How many 1s bars to look for swing high/low
SWEEP_BUFFER    = 0.30                 # Points above/below swing to confirm sweep (Gold = 0.30)
EMA_PERIOD      = 10                   # EMA used as exit/mid target
MAX_HOLD_SEC    = 30                   # Force-exit trade after this many seconds
TICK_SLEEP      = 0.5                  # How fast the bot polls ticks (seconds)
# ══════════════════════════════════════════════


class CandleBuilder:
    """Builds synthetic 1-second OHLC bars from ticks."""
    def __init__(self):
        self.bars = []
        self._current = None
        self._bar_start = None

    def update(self, tick):
        price = (tick.bid + tick.ask) / 2
        ts = int(tick.time)

        if self._bar_start is None or ts > self._bar_start:
            if self._current:
                self.bars.append(self._current)
                if len(self.bars) > 200:      # keep last 200 bars
                    self.bars = self.bars[-200:]
            self._current = {"time": ts, "open": price, "high": price, "low": price, "close": price}
            self._bar_start = ts
        else:
            self._current["high"]  = max(self._current["high"],  price)
            self._current["low"]   = min(self._current["low"],   price)
            self._current["close"] = price

    def get_df(self):
        bars = self.bars[:]
        if self._current:
            bars = bars + [self._current]
        if len(bars) < 5:
            return None
        df = pd.DataFrame(bars)
        df["ema"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
        return df


class LiquidityScalper:
    def __init__(self):
        self.builder       = CandleBuilder()
        self.position      = None   # dict with entry info
        self.trade_open_ts = None

    # ── MT5 helpers ──────────────────────────────────────

    def connect(self):
        if not mt5.initialize():
            print(f"[ERROR] MT5 init failed: {mt5.last_error()}")
            return False
        mt5.symbol_select(SYMBOL, True)
        info = mt5.terminal_info()
        print(f"[OK] Connected — {info.company}")
        acc = mt5.account_info()
        print(f"[OK] Account #{acc.login} | Balance: {acc.balance:.2f} {acc.currency}")
        return True

    def get_tick(self):
        return mt5.symbol_info_tick(SYMBOL)

    def send_order(self, direction):
        tick = self.get_tick()
        if not tick:
            return None
        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price      = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price      = tick.bid

        req = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : SYMBOL,
            "volume"      : LOT_SIZE,
            "type"        : order_type,
            "price"       : price,
            "deviation"   : 30,
            "magic"       : MAGIC,
            "comment"     : f"liq_{direction.lower()}",
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  ✅ {direction} opened @ {price:.2f} | ticket #{result.order}")
            return {"ticket": result.order, "direction": direction, "entry": price}
        else:
            print(f"  ❌ Order failed: {result.retcode} — {result.comment}")
            return None

    def close_trade(self, reason="signal"):
        if not self.position:
            return
        positions = mt5.positions_get(symbol=SYMBOL)
        if not positions:
            self.position = None
            return
        for pos in positions:
            if pos.magic == MAGIC:
                tick = self.get_tick()
                if pos.type == 0:   # BUY → close with SELL
                    close_type = mt5.ORDER_TYPE_SELL
                    price      = tick.bid
                else:               # SELL → close with BUY
                    close_type = mt5.ORDER_TYPE_BUY
                    price      = tick.ask

                req = {
                    "action"      : mt5.TRADE_ACTION_DEAL,
                    "symbol"      : SYMBOL,
                    "volume"      : pos.volume,
                    "type"        : close_type,
                    "position"    : pos.ticket,
                    "price"       : price,
                    "deviation"   : 30,
                    "magic"       : MAGIC,
                    "comment"     : f"close_{reason}",
                    "type_time"   : mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(req)
                pnl = pos.profit
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"  🔒 Closed #{pos.ticket} [{reason}] @ {price:.2f} | PnL: {pnl:+.2f}")
                else:
                    print(f"  ❌ Close failed: {result.retcode}")
        self.position      = None
        self.trade_open_ts = None

    # ── Strategy logic ───────────────────────────────────

    def detect_sweep(self, df):
        """
        Detect liquidity sweep on the latest bar.

        A SELL sweep: latest HIGH exceeds the swing high of the
                      previous LOOKBACK bars by SWEEP_BUFFER,
                      then closes BELOW the swing high (fade).

        A BUY  sweep: latest LOW  goes below the swing low  of the
                      previous LOOKBACK bars by SWEEP_BUFFER,
                      then closes ABOVE the swing low (fade).
        """
        if len(df) < LOOKBACK + 2:
            return None

        history = df.iloc[-(LOOKBACK + 1):-1]   # last N closed bars (exclude current)
        current = df.iloc[-1]                    # current forming bar

        swing_high = history["high"].max()
        swing_low  = history["low"].min()

        # SELL signal: price swept above the high then pulled back
        if (current["high"] >= swing_high + SWEEP_BUFFER and
                current["close"] < swing_high):
            return "SELL"

        # BUY signal: price swept below the low then recovered
        if (current["low"] <= swing_low - SWEEP_BUFFER and
                current["close"] > swing_low):
            return "BUY"

        return None

    def should_exit(self, df, direction):
        """
        Exit rules:
          1. Price reverts to EMA (profit target)
          2. Opposite sweep detected
          3. Max hold time exceeded
        """
        now = time.time()

        # Rule 3: time-based exit
        if self.trade_open_ts and (now - self.trade_open_ts) >= MAX_HOLD_SEC:
            return "timeout"

        if df is None or len(df) < 2:
            return None

        current = df.iloc[-1]
        ema     = current["ema"]
        price   = current["close"]

        # Rule 1: price reached EMA
        if direction == "SELL" and price <= ema:
            return "ema_target"
        if direction == "BUY"  and price >= ema:
            return "ema_target"

        # Rule 2: opposite sweep (reversal failed)
        opposite_sweep = self.detect_sweep(df)
        if opposite_sweep and opposite_sweep != direction:
            return "opposite_sweep"

        return None

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        print("═" * 56)
        print("  XAUUSD Liquidity Scalper  |  1-Second Bars")
        print(f"  Lookback: {LOOKBACK}s | Sweep buffer: {SWEEP_BUFFER} pts")
        print(f"  Max hold: {MAX_HOLD_SEC}s | Lot: {LOT_SIZE}")
        print("  Press Ctrl+C to stop.")
        print("═" * 56 + "\n")

        if not self.connect():
            return

        bar_count = 0
        last_bar_time = None

        while True:
            try:
                tick = self.get_tick()
                if not tick:
                    time.sleep(TICK_SLEEP)
                    continue

                self.builder.update(tick)
                df = self.builder.get_df()

                if df is None:
                    time.sleep(TICK_SLEEP)
                    continue

                current_bar_time = df.iloc[-1]["time"]

                # Only act on a new completed bar (not the live forming one)
                if current_bar_time != last_bar_time and len(df) > 1:
                    last_bar_time = current_bar_time
                    bar_count    += 1
                    ts = datetime.now().strftime("%H:%M:%S")

                    closed_df = df.iloc[:-1]   # all completed bars

                    # ── MANAGE OPEN TRADE ──────────────────────
                    if self.position:
                        direction = self.position["direction"]
                        exit_reason = self.should_exit(closed_df, direction)
                        if exit_reason:
                            print(f"[{ts}] EXIT triggered: {exit_reason}")
                            self.close_trade(exit_reason)

                    # ── LOOK FOR NEW ENTRY ─────────────────────
                    if not self.position:
                        signal = self.detect_sweep(closed_df)
                        if signal:
                            ema_val = closed_df["ema"].iloc[-1]
                            sh      = closed_df["high"].iloc[-(LOOKBACK):].max()
                            sl      = closed_df["low"].iloc[-(LOOKBACK):].min()
                            print(f"[{ts}] 🔍 SWEEP detected: {signal}")
                            print(f"       Swing H: {sh:.2f} | Swing L: {sl:.2f} | EMA: {ema_val:.2f}")
                            self.position      = self.send_order(signal)
                            self.trade_open_ts = time.time()
                        else:
                            if bar_count % 10 == 0:
                                ema_val = closed_df["ema"].iloc[-1]
                                price   = closed_df["close"].iloc[-1]
                                print(f"[{ts}] Watching... Price: {price:.2f} | EMA: {ema_val:.2f} | Bars: {bar_count}")

                time.sleep(TICK_SLEEP)

            except KeyboardInterrupt:
                print("\n[BOT] Stopped by user.")
                if self.position:
                    print("[BOT] Closing open trade before exit...")
                    self.close_trade("manual_stop")
                mt5.shutdown()
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(2)


# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    bot = LiquidityScalper()
    bot.run()
