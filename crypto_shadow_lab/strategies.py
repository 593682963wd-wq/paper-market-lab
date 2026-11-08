"""Deterministic indicators and strategy decisions for the local shadow lab.

This module is deliberately pure: it consumes completed public-market bars and
returns serializable values.  It never imports the private trading client and it
does not execute stop/target collisions; the engine owns that conservative exit
ordering.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import pairwise
from typing import Protocol, cast

MARKET_STATES = frozenset(
    {"no_trade", "high_volatility", "trend_up", "trend_down", "sideways"}
)
TRADEABLE_STATES = frozenset({"trend_up", "trend_down", "sideways"})


@dataclass(frozen=True, slots=True)
class MarketBar:
    """A normalized, completed OHLCV candle with a UTC epoch timestamp."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def date(self) -> int:
        """CoinW-compatible timestamp alias."""

        return self.timestamp

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BollingerBands:
    middle: float
    upper: float
    lower: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataGap:
    after_timestamp: int
    before_timestamp: int
    missing_bars: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MarketState:
    name: str
    reason: str
    last_close: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    atr_percent: float | None = None

    def __post_init__(self) -> None:
        if self.name not in MARKET_STATES:
            raise ValueError(f"unknown market state: {self.name}")

    def to_dict(self) -> dict[str, str | float | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy: str
    action: str
    reason: str
    stop_price: float | None = None
    target_price: float | None = None
    trailing_stop: float | None = None

    def __post_init__(self) -> None:
        if self.action not in {"buy", "sell", "hold"}:
            raise ValueError(f"unknown strategy action: {self.action}")

    def to_dict(self) -> dict[str, str | float | None]:
        return asdict(self)


class PositionLike(Protocol):
    """Small read-only protocol accepted in addition to mappings."""

    quantity: float


PositionInput = Mapping[str, object] | PositionLike | None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_seconds(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0:
        return None
    # CoinW has historically returned seconds.  Accept milliseconds at the
    # boundary so a provider-format change cannot silently create huge gaps.
    if number >= 100_000_000_000:
        number /= 1000
    return int(number)


def _epoch_seconds(value: float | datetime | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _explicitly_incomplete(row: Mapping[str, object]) -> bool:
    if "complete" not in row and "is_complete" not in row:
        return False
    value = row.get("complete", row.get("is_complete"))
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "open", "incomplete"}
    return not bool(value)


def normalize_market_bar(row: Mapping[str, object] | MarketBar) -> MarketBar | None:
    """Normalize one provider row, returning ``None`` for malformed OHLCV."""

    if isinstance(row, MarketBar):
        return row
    if not isinstance(row, Mapping) or _explicitly_incomplete(row):
        return None
    timestamp = _timestamp_seconds(
        row.get("timestamp", row.get("date", row.get("time", row.get("open_time"))))
    )
    values = tuple(
        _finite_number(row.get(key))
        for key in ("open", "high", "low", "close", "volume")
    )
    if timestamp is None or any(value is None for value in values):
        return None
    open_price, high, low, close, volume = cast(
        tuple[float, float, float, float, float], values
    )
    if min(open_price, high, low, close) <= 0 or volume < 0:
        return None
    if low > min(open_price, close) or high < max(open_price, close) or low > high:
        return None
    return MarketBar(timestamp, open_price, high, low, close, volume)


def normalize_market_bars(
    rows: Iterable[Mapping[str, object] | MarketBar],
    *,
    period_seconds: int = 900,
    now: float | datetime | None = None,
    complete_only: bool = True,
) -> list[MarketBar]:
    """Normalize, sort and de-duplicate bars; optionally remove the open bar.

    A timestamp is treated as the candle's opening time.  When ``now`` is
    supplied, a candle is complete only when ``timestamp + period <= now``.
    """

    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    cutoff = _epoch_seconds(now)
    by_timestamp: dict[int, MarketBar] = {}
    for row in rows:
        bar = normalize_market_bar(row)
        if bar is None:
            continue
        if (
            complete_only
            and cutoff is not None
            and bar.timestamp + period_seconds > cutoff
        ):
            continue
        by_timestamp[bar.timestamp] = bar
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def ema(values: Sequence[float], period: int) -> float | None:
    """EMA seeded by the first-period simple mean."""

    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    seed = [float(value) for value in values[:period]]
    if not all(math.isfinite(value) for value in seed):
        return None
    result = sum(seed) / period
    multiplier = 2.0 / (period + 1.0)
    for raw in values[period:]:
        value = float(raw)
        if not math.isfinite(value):
            return None
        result += (value - result) * multiplier
    return result


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI, including deterministic all-gain/all-loss behavior."""

    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) <= period:
        return None
    numeric = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numeric):
        return None
    changes = [current - previous for previous, current in pairwise(numeric)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    if average_gain == 0:
        return 0.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def atr(bars: Sequence[MarketBar], period: int = 14) -> float | None:
    """Simple recent-period average true range."""

    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) <= period:
        return None
    true_ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(bars)
    ]
    return sum(true_ranges[-period:]) / period


def bollinger_bands(
    values: Sequence[float],
    period: int = 20,
    *,
    standard_deviations: float = 2.0,
) -> BollingerBands | None:
    if period <= 0 or standard_deviations < 0:
        raise ValueError("invalid Bollinger parameters")
    if len(values) < period:
        return None
    window = [float(value) for value in values[-period:]]
    if not all(math.isfinite(value) for value in window):
        return None
    middle = sum(window) / period
    deviation = statistics.pstdev(window)
    width = standard_deviations * deviation
    return BollingerBands(middle=middle, upper=middle + width, lower=middle - width)


def detect_time_gaps(
    bars: Sequence[MarketBar], *, period_seconds: int = 900
) -> tuple[DataGap, ...]:
    """Return missing or misaligned intervals between sorted unique candles."""

    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    ordered = sorted(
        {bar.timestamp: bar for bar in bars}.values(), key=lambda bar: bar.timestamp
    )
    gaps: list[DataGap] = []
    for previous, current in pairwise(ordered):
        delta = current.timestamp - previous.timestamp
        if delta != period_seconds:
            missing = max(1, math.ceil(delta / period_seconds) - 1)
            gaps.append(DataGap(previous.timestamp, current.timestamp, missing))
    return tuple(gaps)


def classify_market(
    bars: Sequence[MarketBar],
    *,
    data_fresh: bool = True,
    frozen: bool = False,
    spread_percent: float | None = None,
    max_spread_percent: float = 0.5,
    period_seconds: int = 900,
    high_volatility_atr_percent: float = 4.0,
) -> MarketState:
    """Classify completed bars into the five fixed shadow-lab states."""

    last_close = bars[-1].close if bars else None
    if not data_fresh:
        return MarketState("no_trade", "data_stale", last_close=last_close)
    if frozen:
        return MarketState("no_trade", "symbol_frozen", last_close=last_close)
    if spread_percent is not None and spread_percent > max_spread_percent:
        return MarketState("no_trade", "spread_too_wide", last_close=last_close)
    if len(bars) < 51:
        return MarketState("no_trade", "insufficient_bars", last_close=last_close)
    if detect_time_gaps(bars, period_seconds=period_seconds):
        return MarketState("no_trade", "time_gap", last_close=last_close)

    closes = [bar.close for bar in bars]
    ema20_value = ema(closes, 20)
    ema50_value = ema(closes, 50)
    atr_value = atr(bars, 14)
    atr_percent = (
        (atr_value / last_close * 100.0)
        if atr_value is not None and last_close
        else None
    )
    common = {
        "last_close": last_close,
        "ema20": ema20_value,
        "ema50": ema50_value,
        "atr_percent": atr_percent,
    }
    if None in (ema20_value, ema50_value, atr_percent):
        return MarketState("no_trade", "insufficient_indicators", **common)
    assert ema20_value is not None
    assert ema50_value is not None
    assert atr_percent is not None
    assert last_close is not None
    if atr_percent > high_volatility_atr_percent:
        return MarketState("high_volatility", "atr_percent_high", **common)

    direction = closes[-1] - closes[-6]
    trend_separation_percent = abs(ema20_value - ema50_value) / last_close * 100.0
    confirmed_separation = trend_separation_percent >= 0.1
    if (
        confirmed_separation
        and ema20_value > ema50_value
        and last_close > ema20_value
        and direction > 0
    ):
        return MarketState("trend_up", "ema20_above_ema50", **common)
    if (
        confirmed_separation
        and ema20_value < ema50_value
        and last_close < ema20_value
        and direction < 0
    ):
        return MarketState("trend_down", "ema20_below_ema50", **common)
    return MarketState("sideways", "no_confirmed_trend", **common)


def _state_name(market_state: MarketState | str) -> str:
    return (
        market_state.name
        if isinstance(market_state, MarketState)
        else str(market_state)
    )


def _quantity(position: PositionInput) -> float:
    if position is None:
        return 0.0
    value = (
        position.get("quantity", 0.0)
        if isinstance(position, Mapping)
        else getattr(position, "quantity", 0.0)
    )
    number = _finite_number(value)
    return number if number is not None and number > 0 else 0.0


def _decision(
    strategy: str,
    action: str,
    reason: str,
    *,
    stop_price: float | None = None,
    target_price: float | None = None,
    trailing_stop: float | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        strategy, action, reason, stop_price, target_price, trailing_stop
    )


def _entry_blocked(
    strategy: str, market_state: MarketState | str
) -> StrategyDecision | None:
    state = _state_name(market_state)
    if state not in TRADEABLE_STATES:
        return _decision(strategy, "hold", f"entry_blocked:{state}")
    return None


def current_shape(
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """Mirror the deployed rule shape without its absolute historical target."""

    name = "current_shape"
    closes = [bar.close for bar in bars]
    fast, slow, momentum = ema(closes, 12), ema(closes, 26), rsi(closes, 14)
    if _quantity(position):
        return _decision(name, "hold", "position_open")
    blocked = _entry_blocked(name, market_state)
    if blocked:
        return blocked
    if None in (fast, slow, momentum):
        return _decision(name, "hold", "insufficient_indicators")
    assert fast is not None
    assert slow is not None
    assert momentum is not None
    if fast > slow and momentum <= 35.0:
        close = closes[-1]
        return _decision(
            name,
            "buy",
            "ema12_above_ema26_and_rsi_oversold",
            stop_price=close * 0.95,
            target_price=close * 1.1125,
        )
    return _decision(name, "hold", "entry_conditions_not_met")


def trend_pullback(
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """Buy an EMA20 reclaim in an established uptrend with ATR protection."""

    name = "trend_pullback"
    closes = [bar.close for bar in bars]
    current_ema = ema(closes, 20)
    previous_ema = ema(closes[:-1], 20) if len(closes) > 20 else None
    range_value = atr(bars, 14)
    if _quantity(position):
        if current_ema is not None and closes and closes[-1] < current_ema:
            return _decision(name, "sell", "close_below_ema20")
        return _decision(name, "hold", "position_open")
    blocked = _entry_blocked(name, market_state)
    if blocked:
        return blocked
    if len(closes) < 2 or None in (current_ema, previous_ema, range_value):
        return _decision(name, "hold", "insufficient_indicators")
    assert current_ema is not None
    assert previous_ema is not None
    assert range_value is not None
    reclaimed = closes[-2] <= previous_ema and closes[-1] > current_ema
    if _state_name(market_state) == "trend_up" and reclaimed:
        distance = 2.0 * range_value
        return _decision(
            name,
            "buy",
            "ema20_reclaim",
            stop_price=max(0.0, closes[-1] - distance),
            trailing_stop=distance,
        )
    return _decision(name, "hold", "entry_conditions_not_met")


def breakout(
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """Buy a 20-bar high breakout with 1.2x volume confirmation."""

    name = "breakout"
    closes = [bar.close for bar in bars]
    current_ema = ema(closes, 20)
    if _quantity(position):
        if current_ema is not None and closes and closes[-1] < current_ema:
            return _decision(name, "sell", "close_below_ema20")
        return _decision(name, "hold", "position_open")
    blocked = _entry_blocked(name, market_state)
    if blocked:
        return blocked
    if len(bars) < 21:
        return _decision(name, "hold", "insufficient_bars")
    previous = bars[-21:-1]
    previous_high = max(bar.high for bar in previous)
    average_volume = sum(bar.volume for bar in previous) / len(previous)
    range_value = atr(bars, 14)
    confirmed = (
        bars[-1].close > previous_high and bars[-1].volume >= average_volume * 1.2
    )
    if (
        _state_name(market_state) == "trend_up"
        and confirmed
        and range_value is not None
    ):
        distance = 2.0 * range_value
        return _decision(
            name,
            "buy",
            "prior_high_breakout_with_volume",
            stop_price=max(0.0, bars[-1].close - distance),
            trailing_stop=distance,
        )
    return _decision(name, "hold", "entry_conditions_not_met")


def mean_reversion(
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """Trade a lower-band reclaim only while the market is sideways."""

    name = "mean_reversion"
    closes = [bar.close for bar in bars]
    bands = bollinger_bands(closes, 20)
    previous_bands = bollinger_bands(closes[:-1], 20) if len(closes) > 20 else None
    momentum = rsi(closes, 14)
    range_value = atr(bars, 14)
    if _quantity(position):
        if (
            bands is not None
            and momentum is not None
            and (closes[-1] >= bands.middle or momentum >= 55.0)
        ):
            return _decision(name, "sell", "mean_reversion_complete")
        return _decision(name, "hold", "position_open")
    blocked = _entry_blocked(name, market_state)
    if blocked:
        return blocked
    if None in (bands, previous_bands, momentum, range_value) or len(closes) < 2:
        return _decision(name, "hold", "insufficient_indicators")
    assert bands is not None
    assert previous_bands is not None
    assert momentum is not None
    assert range_value is not None
    reclaimed = closes[-2] <= previous_bands.lower and closes[-1] > bands.lower
    if _state_name(market_state) == "sideways" and reclaimed and momentum < 40.0:
        return _decision(
            name,
            "buy",
            "lower_band_rsi_reclaim",
            stop_price=max(0.0, closes[-1] - 2.0 * range_value),
            target_price=bands.middle,
        )
    return _decision(name, "hold", "entry_conditions_not_met")


def buy_hold(
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """One-time fully passive benchmark; it never emits a strategy sell."""

    name = "buy_hold"
    if _quantity(position):
        return _decision(name, "hold", "position_open")
    blocked = _entry_blocked(name, market_state)
    if blocked:
        return blocked
    if not bars:
        return _decision(name, "hold", "insufficient_bars")
    return _decision(name, "buy", "initial_benchmark_entry")


STRATEGIES = {
    "current_shape": current_shape,
    "trend_pullback": trend_pullback,
    "breakout": breakout,
    "mean_reversion": mean_reversion,
    "buy_hold": buy_hold,
}


def evaluate_strategy(
    name: str,
    bars: Sequence[MarketBar],
    market_state: MarketState | str,
    position: PositionInput = None,
) -> StrategyDecision:
    """Dispatch one of the five fixed strategies without external side effects."""

    try:
        strategy = STRATEGIES[name]
    except KeyError as exc:
        raise ValueError(f"unknown strategy: {name}") from exc
    return strategy(bars, market_state, position)


__all__ = [
    "MARKET_STATES",
    "STRATEGIES",
    "TRADEABLE_STATES",
    "BollingerBands",
    "DataGap",
    "MarketBar",
    "MarketState",
    "PositionLike",
    "StrategyDecision",
    "atr",
    "bollinger_bands",
    "breakout",
    "buy_hold",
    "classify_market",
    "current_shape",
    "detect_time_gaps",
    "ema",
    "evaluate_strategy",
    "mean_reversion",
    "normalize_market_bar",
    "normalize_market_bars",
    "rsi",
    "trend_pullback",
]
