"""
╔══════════════════════════════════════════════════════════════╗
║   XAUUSD Absorption Zone Scalper — Tick-Based                ║
║   Strategy : Bid/Ask Spread Compression → Breakout Fade      ║
║   SL       : $2.00 USD fixed                                 ║
║   TP       : $2.00 USD fixed  (1:1 RR)                       ║
╚══════════════════════════════════════════════════════════════╝

HOW IT WORKS:
─────────────
Absorption zones appear when the bid/ask spread COMPRESSES
tightly — institutions are absorbing all available orders at
a price level, creating a "wall." Once absorption ends, price
breaks out sharply in one direction.

Detection logic (from YOUR tick chart — bid=blue, ask=red):
  1. Track the rolling spread (ask - bid) over last N ticks
  2. When current spread < COMPRESSION_THRESHOLD × avg spread
     → Absorption zone detected
  3. When price then breaks OUT of the zone by BREAKOUT_PIPS:
       - Break UP  → BUY
       - Break DOWN → SELL
  4. SL = $2 USD | TP = $2 USD from entry

INSTALL:
  pip install MetaTrader5 pandas numpy

RUN:
  python xauusd_absorption_scalper.py
"""

import time
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime
from collections import deque

# ══════════════════════════════════════════════════════
#  ⚙️  SETTINGS
# ══════════════════════════════════════════════════════
SYMBOL               = "XAUUSD"
LOT_SIZE             = 0.01        # Micro lot
MAGIC                = 888002

# Absorption detection
TICK_WINDOW          = 30          # Rolling ticks to measure avg spread
COMPRESSION_RATIO    = 0.5         # Spread must be < 50% of avg to trigger zone
MIN_ZONE_TICKS       = 5           # Minimum ticks inside zone before breakout
BREAKOUT_POINTS      = 0.20        # Points price must move to confirm breakout

# Money management
SL_USD               = 2.00        # Stop loss in USD
TP_USD               = 2.00        # Take profit in USD

# Timing
MAX_HOLD_SEC         = 60          # Force-exit after 60 seconds
TICK_SLEEP           = 0.1         # Poll speed
COOLDOWN_SEC         = 5           # Wait after close before next trade
# ══════════════════════════════════════════════════════


def usd_to_points(usd_amount, symbol_info, lot):
    """
    Convert a USD profit/loss target into price points.
    For XAUUSD: 1 point = $1 per 0.01 lot (approx).
    Formula: points = usd / (lot × point_value_per_lot)
    """
    tick_value = symbol_info.trade_tick_value   # USD per tick per lot
    tick_size  = symbol_info.trade_tick_size    # price movement per tick

    if tick_value == 0 or tick_size == 0:
        # Fallback: XAUUSD approx $1 per 0.01 lot per $1 move
        return usd_amount / lot

    # points = (usd / lot) / (tick_value / tick_size)
    point_value = tick_value / tick_size
    points = usd_amount / (lot * point_value)
    return round(points, 2)


class AbsorptionScalper:

    def __init__(self):
        self.ticks         = deque(maxlen=TICK_WINDOW + 50)
        self.position      = None
        self.trade_open_ts = None
        self.last_close_ts = None
        self.zone_active   = False
        self.zone_ticks    = 0
        self.zone_mid      = None   # mid-price when zone formed
        self.symbol_info   = None

    # ── Connection ──────────────────────────────────────

    def connect(self):
        if not mt5.initialize():
            print(f"[ERROR] MT5 init failed: {mt5.last_error()}")
            return False
        mt5.symbol_select(SYMBOL, True)
        self.symbol_info = mt5.symbol_info(SYMBOL)
        if self.symbol_info is None:
            print(f"[ERROR] Symbol {SYMBOL} not found.")
            return False
        acc = mt5.account_info()
        info = mt5.terminal_info()
        print(f"[OK] Connected to {info.company}")
        print(f"[OK] Account #{acc.login} | Balance: {acc.balance:.2f} {acc.currency}")
        print(f"[OK] {SYMBOL} | Digits: {self.symbol_info.digits} | "
              f"Tick size: {self.symbol_info.trade_tick_size} | "
              f"Tick value: {self.symbol_info.trade_tick_value:.4f}")

        # Pre-calculate SL/TP in points
        self.sl_points = usd_to_points(SL_USD, self.symbol_info, LOT_SIZE)
        self.tp_points = usd_to_points(TP_USD, self.symbol_info, LOT_SIZE)
        print(f"[OK] SL: ${SL_USD} = {self.sl_points:.2f} pts | "
              f"TP: ${TP_USD} = {self.tp_points:.2f} pts")
        return True

    # ── Order helpers ────────────────────────────────────

    def open_trade(self, direction, tick):
        if direction == "BUY":
            price      = tick.ask
            sl         = round(price - self.sl_points, self.symbol_info.digits)
            tp         = round(price + self.tp_points, self.symbol_info.digits)
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price      = tick.bid
            sl         = round(price + self.sl_points, self.symbol_info.digits)
            tp         = round(price - self.tp_points, self.symbol_info.digits)
            order_type = mt5.ORDER_TYPE_SELL

        req = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : SYMBOL,
            "volume"      : LOT_SIZE,
            "type"        : order_type,
            "price"       : price,
            "sl"          : sl,
            "tp"          : tp,
            "deviation"   : 30,
            "magic"       : MAGIC,
            "comment"     : f"abs_{direction.lower()}",
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(req)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  ✅ {direction} @ {price:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | #{result.order}")
            return {"ticket": result.order, "direction": direction, "entry": price}
        else:
            print(f"  ❌ Order failed: {result.retcode} — {result.comment}")
            return None

    def force_close(self, reason="timeout"):
        """Force-close if still open (SL/TP not hit)."""
        positions = mt5.positions_get(symbol=SYMBOL)
        if not positions:
            self.position = None
            return
        for pos in positions:
            if pos.magic != MAGIC:
                continue
            tick = mt5.symbol_info_tick(SYMBOL)
            if pos.type == 0:
                close_type = mt5.ORDER_TYPE_SELL
                price      = tick.bid
            else:
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
                print(f"  🔒 Force-closed #{pos.ticket} [{reason}] @ {price:.2f} | PnL: {pnl:+.2f}")
        self.position      = None
        self.trade_open_ts = None
        self.last_close_ts = time.time()

    def check_if_closed_by_broker(self):
        """Check if SL/TP was hit by broker."""
        if not self.position:
            return
        positions = mt5.positions_get(symbol=SYMBOL)
        open_tickets = [p.ticket for p in positions] if positions else []
        if self.position["ticket"] not in open_tickets:
            # Closed by SL or TP
            deals = mt5.history_deals_get(
                mt5.datetime(2020, 1, 1),
                datetime.now()
            )
            pnl = 0
            if deals:
                for d in reversed(deals):
                    if d.order == self.position["ticket"] or d.position_id == self.position["ticket"]:
                        pnl = d.profit
                        break
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"  [{ts}] 🎯 SL/TP hit | PnL: {pnl:+.2f}")
            self.position      = None
            self.trade_open_ts = None
            self.last_close_ts = time.time()

    # ── Absorption logic ─────────────────────────────────

    def process_tick(self, tick):
        spread = tick.ask - tick.bid
        mid    = (tick.ask + tick.bid) / 2.0
        self.ticks.append({"spread": spread, "mid": mid, "ask": tick.ask, "bid": tick.bid})

    def get_avg_spread(self):
        if len(self.ticks) < 5:
            return None
        spreads = [t["spread"] for t in self.ticks]
        return np.mean(spreads)

    def detect_absorption(self):
        """
        Returns True when current spread is compressed vs average.
        This signals institutions absorbing at a price level.
        """
        if len(self.ticks) < TICK_WINDOW:
            return False
        avg_spread     = np.mean([t["spread"] for t in list(self.ticks)[-TICK_WINDOW:]])
        current_spread = self.ticks[-1]["spread"]
        return current_spread < (avg_spread * COMPRESSION_RATIO)

    def detect_breakout(self):
        """
        After absorption zone, detect which direction price breaks.
        Compare current mid to zone mid-price.
        Returns 'BUY', 'SELL', or None.
        """
        if self.zone_mid is None or len(self.ticks) < 2:
            return None
        current_mid = self.ticks[-1]["mid"]
        delta = current_mid - self.zone_mid
        if delta >= BREAKOUT_POINTS:
            return "BUY"
        elif delta <= -BREAKOUT_POINTS:
            return "SELL"
        return None

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        print("═" * 60)
        print("  XAUUSD Absorption Zone Scalper")
        print(f"  Spread compression ratio : {COMPRESSION_RATIO*100:.0f}% of avg")
        print(f"  Min zone ticks           : {MIN_ZONE_TICKS}")
        print(f"  Breakout confirmation    : {BREAKOUT_POINTS} pts")
        print(f"  SL: ${SL_USD} | TP: ${TP_USD} | Lot: {LOT_SIZE}")
        print("  Press Ctrl+C to stop.")
        print("═" * 60 + "\n")

        if not self.connect():
            return

        last_log = time.time()
        tick_count = 0

        while True:
            try:
                tick = mt5.symbol_info_tick(SYMBOL)
                if not tick:
                    time.sleep(TICK_SLEEP)
                    continue

                self.process_tick(tick)
                tick_count += 1
                ts = datetime.now().strftime("%H:%M:%S")

                # ── CHECK IF SL/TP HIT ─────────────────────
                if self.position:
                    self.check_if_closed_by_broker()

                # ── FORCE-CLOSE ON TIMEOUT ─────────────────
                if self.position and self.trade_open_ts:
                    if (time.time() - self.trade_open_ts) >= MAX_HOLD_SEC:
                        print(f"[{ts}] ⏰ Timeout — force closing")
                        self.force_close("timeout")

                # ── COOLDOWN AFTER CLOSE ───────────────────
                in_cooldown = (
                    self.last_close_ts and
                    (time.time() - self.last_close_ts) < COOLDOWN_SEC
                )

                # ── ABSORPTION + BREAKOUT LOGIC ────────────
                if not self.position and not in_cooldown:

                    absorbing = self.detect_absorption()

                    if absorbing:
                        if not self.zone_active:
                            # Zone just started
                            self.zone_active = True
                            self.zone_ticks  = 1
                            self.zone_mid    = self.ticks[-1]["mid"]
                            print(f"[{ts}] 🟡 Absorption zone started @ {self.zone_mid:.2f} "
                                  f"| spread: {self.ticks[-1]['spread']:.3f}")
                        else:
                            self.zone_ticks += 1
                            # Update zone mid as average
                            self.zone_mid = np.mean(
                                [t["mid"] for t in list(self.ticks)[-self.zone_ticks:]]
                            ) if self.zone_ticks <= len(self.ticks) else self.zone_mid
                    else:
                        if self.zone_active and self.zone_ticks >= MIN_ZONE_TICKS:
                            # Zone ended — check for breakout
                            direction = self.detect_breakout()
                            if direction:
                                avg_spread = self.get_avg_spread()
                                curr_spread = self.ticks[-1]["spread"]
                                print(f"[{ts}] 💥 BREAKOUT {direction} after {self.zone_ticks} zone ticks")
                                print(f"       Zone mid: {self.zone_mid:.2f} | "
                                      f"Avg spread: {avg_spread:.3f} | "
                                      f"Now: {curr_spread:.3f}")
                                self.position = self.open_trade(direction, tick)
                                self.trade_open_ts = time.time()
                            else:
                                print(f"[{ts}] ⚪ Zone ended — no breakout (zone ticks: {self.zone_ticks})")

                        # Reset zone
                        self.zone_active = False
                        self.zone_ticks  = 0
                        self.zone_mid    = None

                # ── STATUS LOG every 5 sec ─────────────────
                if time.time() - last_log >= 5:
                    last_log = time.time()
                    avg_s = self.get_avg_spread()
                    curr_s = self.ticks[-1]["spread"] if self.ticks else 0
                    pos_str = f"IN {self.position['direction']}" if self.position else "FLAT"
                    zone_str = f"ZONE({self.zone_ticks})" if self.zone_active else "watching"
                    avg_s_display = f"{avg_s:.3f}" if avg_s else "0.000"
                    print(f"[{ts}] {pos_str} | {zone_str} | "
                          f"Spread: {curr_s:.3f} (avg {avg_s_display}) | "
                          f"Ticks: {tick_count}")

                time.sleep(TICK_SLEEP)

            except KeyboardInterrupt:
                print("\n[BOT] Stopped by user.")
                if self.position:
                    print("[BOT] Closing open trade...")
                    self.force_close("manual_stop")
                mt5.shutdown()
                print("[BOT] MT5 disconnected. Goodbye.")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(1)


# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    bot = AbsorptionScalper()
    bot.run()
