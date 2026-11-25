"""Oversold-bounce bot for make.fun's "Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana"
(bars-replay/v1, hourly bars of FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH, WIF).

Idea: the dev window is a grind down with sharp oversold bounces. This bot sits in
cash most of the time and only buys a symbol when it is deeply oversold on two
independent gauges at once -- RSI(14) under 30 AND the close under its lower
Bollinger band (20 typical prices, 2 sigma) -- then takes a quick +1% profit,
because bounces in this market fade fast. It never shorts, never uses leverage,
and never holds more than one seventh of equity per symbol.

Exits (whole position, checked every bar):
  - take profit: position value >= 101% of its cost
  - stop loss:   position value <= 90% of its cost
  - trailing exit: once the close has peaked at least 1% above the entry
    close, a close 0.5% below that peak sells -- it banks a bounce that
    faded just short of the take-profit instead of holding it to the flat.
  - an RSI over 70 also exits (a bounce that ran hot)

Sizing: each buy is min(equity / 7, the 25% weight cap's room, cash left in this
reply), so all seven symbols can be bought on the same bar without ever
over-spending the cash. Slippage is capped at 100 bps per order, so a fill
through a gap is refused and retried on the next bar instead of filled badly.

Deterministic: only past bars are read (no clock, no randomness, no network),
so the two grading runs produce identical action logs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

RSI_BARS = 14
RSI_ENTRY_BELOW = 30
RSI_EXIT_ABOVE = 70
BAND_BARS = 20
BAND_SIGMAS = 2.0
TAKE_PROFIT = 101  # value >= 101% of cost
STOP_LOSS = 90     # value <= 90% of cost
TRAIL_ARM = 101    # trailing exit arms once peak close >= 101% of entry close
TRAIL_DROP = 995   # ... then a close at or under 99.5% of the peak sells
MAX_SLIPPAGE_BPS = 100
SLOTS = 7          # one seventh of equity per buy


class Series:
    """RSI (Wilder) + Bollinger lower band over one symbol's bars."""

    def __init__(self):
        self.typical = []
        self.last = None
        self.first = []
        self.gain = None
        self.loss = None

    def push(self, bar):
        c = int(bar["c"])
        typical = (int(bar["h"]) + int(bar["l"]) + c) / 3
        self.typical.append(typical)
        if len(self.typical) > BAND_BARS:
            self.typical.pop(0)
        if self.last is not None:
            change = c - self.last
            up = change if change > 0 else 0.0
            down = -change if change < 0 else 0.0
            if self.gain is None:
                self.first.append(change)
                if len(self.first) == RSI_BARS:
                    self.gain = sum(x for x in self.first if x > 0) / RSI_BARS
                    self.loss = sum(-x for x in self.first if x < 0) / RSI_BARS
            else:
                self.gain = (self.gain * (RSI_BARS - 1) + up) / RSI_BARS
                self.loss = (self.loss * (RSI_BARS - 1) + down) / RSI_BARS
        self.last = c

    def rsi(self):
        if self.gain is None:
            return None
        moved = self.gain + self.loss
        return 50.0 if moved == 0 else 100.0 * self.gain / moved

    def lower_band(self):
        if len(self.typical) < BAND_BARS:
            return None
        mean = sum(self.typical) / BAND_BARS
        var = sum((x - mean) ** 2 for x in self.typical) / BAND_BARS
        return mean - BAND_SIGMAS * var ** 0.5


SYMBOLS = []
SERIES = {}


def on_hello(state):
    global SYMBOLS
    SYMBOLS = [s["symbol"] for s in state.hello["symbols"]]
    for symbol in SYMBOLS:
        SERIES[symbol] = Series()


def on_batch(state, batch):
    # A buy sent last bar resolved in this batch's fills: a fill promotes its
    # decision close to the entry record (entry close, peak close) the
    # trailing exit reads; a rejection drops the pending entry so the symbol
    # can be bought again.
    for fill in batch.get("fills", []):
        if fill.get("side") != "buy":
            continue
        symbol = fill["symbol"]
        if fill.get("status") == "filled":
            ref = PENDING.pop(symbol, state.close(symbol))
            ENTRIES[symbol] = (ref, ref)
        else:
            PENDING.pop(symbol, None)

    for bar in batch["bars"]:
        SERIES[bar["symbol"]].push(bar)

    if state.warming_up:
        return []

    orders = []
    # Exits first: sells free no cash this reply, buys are sized after them.
    for symbol in SYMBOLS:
        held = state.positions.get(symbol)
        if held is None:
            continue
        series = SERIES[symbol]
        rsi = series.rsi()
        value = state.value(symbol, held.qty)
        entry = ENTRIES.get(symbol)
        trailing = False
        if entry is not None:
            ref, peak = entry
            close = state.close(symbol)
            if close is not None:
                peak = close if close > peak else peak
                ENTRIES[symbol] = (ref, peak)
                trailing = (peak * 100 >= ref * TRAIL_ARM
                            and close * 1000 <= peak * TRAIL_DROP)
        if (rsi is not None and rsi > RSI_EXIT_ABOVE
                or value * 100 >= held.cost * TAKE_PROFIT
                or value * 100 <= held.cost * STOP_LOSS
                or trailing):
            orders.append({"side": "sell", "symbol": symbol, "qty": held.qty,
                           "max_slippage_bps": MAX_SLIPPAGE_BPS})
            ENTRIES.pop(symbol, None)
            PENDING.pop(symbol, None)

    # Entries: deeply oversold on both gauges, on a symbol not held.
    cash_left = state.cash
    equity = state.equity
    for symbol in SYMBOLS:
        if symbol in state.positions or symbol in PENDING:
            continue
        series = SERIES[symbol]
        rsi = series.rsi()
        lower = series.lower_band()
        close = state.close(symbol)
        if rsi is None or lower is None or close is None:
            continue
        if rsi >= RSI_ENTRY_BELOW or close >= lower:
            continue
        stake = equity // SLOTS
        room = state.weight_room(symbol)
        spend = min(stake, room, cash_left)
        if spend < 1_000_000:
            continue
        orders.append({"side": "buy", "symbol": symbol,
                       "notional_quote_units": spend,
                       "max_slippage_bps": MAX_SLIPPAGE_BPS})
        cash_left -= spend
        PENDING[symbol] = close
    return orders


# Decision close of a buy sent last bar, awaiting its fill; promoted to
# ENTRIES once the fill lands (the trailing exit reads it from there).
PENDING = {}
ENTRIES = {}

makefun_bars.run(on_batch, on_hello=on_hello)
