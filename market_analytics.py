"""把 CoinW 公开行情转换成可复算的只读观察指标。

作者：Wangyuan
日期：2026-08-28
职责：描述市场状态与风险证据，不输出买卖指令或收益承诺。
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any


def build_market_analysis(snapshot: dict[str, Any]) -> dict[str, Any]:
    """生成趋势、动量、波动、流动性和成交结构摘要。"""

    candles = _normalize_candles(snapshot.get("klines", []))
    closes = [row["close"] for row in candles]
    ticker = snapshot.get("ticker") if isinstance(snapshot.get("ticker"), dict) else {}
    book = snapshot.get("order_book") if isinstance(snapshot.get("order_book"), dict) else {}
    trades = snapshot.get("recent_trades") if isinstance(snapshot.get("recent_trades"), list) else []

    last = _number(ticker.get("last")) or (closes[-1] if closes else None)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rsi14 = _rsi(closes, 14)
    return_5 = _percent_change(closes[-6], closes[-1]) if len(closes) >= 6 else None
    volatility_20 = _return_volatility(closes[-21:]) if len(closes) >= 21 else None
    atr14_pct = _atr_percent(candles, 14)
    spread = _spread_metrics(book)
    trade_flow = _trade_flow(trades)

    trend = _trend_state(last, ema20, ema50, return_5)
    momentum = _momentum_state(rsi14)
    volatility = _volatility_state(atr14_pct, volatility_20)
    liquidity = _liquidity_state(spread.get("spread_bps"))
    risks = _risk_flags(ticker, momentum, volatility, liquidity, closes)
    decision_state = _decision_state(risks, closes)

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "provider": "CoinW",
            "mode": "public_read_only",
            "authenticated": False,
            "account_connected": False,
        },
        "market": {
            "symbol": snapshot.get("symbol"),
            "pair_code": str(ticker.get("id")) if ticker.get("id") is not None else None,
            "period_seconds": snapshot.get("period"),
            "last": last,
            "change_24h_percent": _scaled_percent(ticker.get("percentChange")),
            "high_24h": _number(ticker.get("high24hr")),
            "low_24h": _number(ticker.get("low24hr")),
            "quote_volume_24h": _number(ticker.get("baseVolume")),
            "best_bid": _number(ticker.get("highestBid")),
            "best_ask": _number(ticker.get("lowestAsk")),
            "is_frozen": int(_number(ticker.get("isFrozen")) or 0) == 1,
            "candle_count": len(candles),
        },
        "indicators": {
            "ema20": ema20,
            "ema50": ema50,
            "rsi14": rsi14,
            "return_5_candles_percent": return_5,
            "return_volatility_20_percent": volatility_20,
            "atr14_percent": atr14_pct,
            **spread,
            **trade_flow,
        },
        "states": {
            "trend": trend,
            "momentum": momentum,
            "volatility": volatility,
            "liquidity": liquidity,
            "decision": decision_state,
        },
        "summary": _summary_text(trend, momentum, volatility, liquidity, risks),
        "risk_flags": risks,
        "candles": candles,
        "order_book": _public_book(book),
        "recent_trades": [_public_trade(row) for row in trades[:20]],
        "symbol_info": snapshot.get("symbol_info", {}),
        "methodology": [
            "趋势：最新价与 EMA20、EMA50 的相对关系，并参考近 5 根 K 线涨跌。",
            "动量：RSI14；超过 70 仅表示偏热，低于 30 仅表示偏冷。",
            "波动：ATR14 占价格比例与最近 20 根收益率离散度。",
            "流动性：最优买卖价差和五档订单簿数量结构。",
            "所有状态只用于观察和复核，不构成买卖建议。",
        ],
    }


def _normalize_candles(rows: Any) -> list[dict[str, float | int]]:
    normalized: list[dict[str, float | int]] = []
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = {name: _number(row.get(name)) for name in ("open", "high", "low", "close", "volume")}
        timestamp = _number(row.get("date"))
        if timestamp is None or any(value is None for value in values.values()):
            continue
        normalized.append({"date": int(timestamp), **values})
    normalized.sort(key=lambda item: int(item["date"]))
    return normalized


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scaled_percent(value: Any) -> float | None:
    number = _number(value)
    return number * 100 if number is not None else None


def _percent_change(start: float, end: float) -> float | None:
    return ((end / start) - 1) * 100 if start else None


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def _rsi(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _return_volatility(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    returns = [_percent_change(previous, current) for previous, current in zip(values, values[1:])]
    usable = [value for value in returns if value is not None]
    return statistics.pstdev(usable) if len(usable) >= 2 else None


def _atr_percent(candles: list[dict[str, float | int]], period: int) -> float | None:
    if len(candles) <= period:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        high = float(current["high"])
        low = float(current["low"])
        previous_close = float(previous["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = sum(true_ranges[-period:]) / period
    last = float(candles[-1]["close"])
    return atr / last * 100 if last else None


def _spread_metrics(book: dict[str, Any]) -> dict[str, float | None]:
    asks = _book_levels(book.get("asks"))
    bids = _book_levels(book.get("bids"))
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    midpoint = ((best_ask + best_bid) / 2) if best_ask is not None and best_bid is not None else None
    spread_bps = ((best_ask - best_bid) / midpoint * 10000) if midpoint else None
    bid_quantity = sum(level[1] for level in bids)
    ask_quantity = sum(level[1] for level in asks)
    total = bid_quantity + ask_quantity
    return {
        "spread_bps": spread_bps,
        "book_bid_quantity": bid_quantity,
        "book_ask_quantity": ask_quantity,
        "book_bid_ratio": (bid_quantity / total) if total else None,
    }


def _book_levels(value: Any) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return levels
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, quantity = _number(row[0]), _number(row[1])
        if price is not None and quantity is not None:
            levels.append((price, quantity))
    return levels


def _trade_flow(rows: list[Any]) -> dict[str, float | int | None]:
    buy_amount = sell_amount = 0.0
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        total = _number(row.get("total"))
        if total is None:
            price = _number(row.get("price"))
            amount = _number(row.get("amount"))
            total = price * amount if price is not None and amount is not None else None
        if total is None:
            continue
        count += 1
        if str(row.get("type", "")).upper() == "BUY":
            buy_amount += total
        else:
            sell_amount += total
    aggregate = buy_amount + sell_amount
    return {
        "recent_trade_count": count,
        "recent_buy_amount": buy_amount,
        "recent_sell_amount": sell_amount,
        "recent_buy_ratio": buy_amount / aggregate if aggregate else None,
    }


def _trend_state(last: float | None, ema20: float | None, ema50: float | None, return_5: float | None) -> str:
    if None in (last, ema20, ema50, return_5):
        return "数据不足"
    if last > ema20 > ema50 and return_5 > 0:
        return "趋势偏强"
    if last < ema20 < ema50 and return_5 < 0:
        return "趋势偏弱"
    return "区间震荡"


def _momentum_state(rsi: float | None) -> str:
    if rsi is None:
        return "数据不足"
    if rsi >= 70:
        return "动量偏热"
    if rsi <= 30:
        return "动量偏冷"
    return "动量中性"


def _volatility_state(atr_pct: float | None, return_volatility: float | None) -> str:
    candidates = [value for value in (atr_pct, return_volatility) if value is not None]
    if not candidates:
        return "数据不足"
    level = max(candidates)
    if level >= 1.2:
        return "波动较高"
    if level >= 0.5:
        return "波动中等"
    return "波动较低"


def _liquidity_state(spread_bps: float | None) -> str:
    if spread_bps is None:
        return "数据不足"
    if spread_bps <= 2:
        return "价差较窄"
    if spread_bps <= 8:
        return "价差偏宽"
    return "流动性风险"


def _risk_flags(
    ticker: dict[str, Any], momentum: str, volatility: str, liquidity: str, closes: list[float]
) -> list[str]:
    flags: list[str] = []
    if int(_number(ticker.get("isFrozen")) or 0) == 1:
        flags.append("交易对处于冻结状态")
    if momentum == "动量偏热":
        flags.append("RSI 偏热，追涨风险上升")
    elif momentum == "动量偏冷":
        flags.append("RSI 偏冷，但不代表价格已经见底")
    if volatility == "波动较高":
        flags.append("短周期波动较高，价格可能快速反向")
    if liquidity in {"价差偏宽", "流动性风险"}:
        flags.append("买卖价差偏宽，成交滑点风险增加")
    change_24h = abs(_scaled_percent(ticker.get("percentChange")) or 0)
    if change_24h >= 8:
        flags.append("24 小时涨跌较大，注意极端波动")
    if len(closes) < 50:
        flags.append("K 线样本不足 50 根，趋势证据不完整")
    return flags


def _decision_state(risks: list[str], closes: list[float]) -> str:
    if len(closes) < 20:
        return "数据不足，暂不形成判断"
    if len(risks) >= 3:
        return "风险信号较多，优先控制风险"
    if risks:
        return "存在风险提示，需要人工复核"
    return "数据状态正常，仍需自行判断"


def _summary_text(trend: str, momentum: str, volatility: str, liquidity: str, risks: list[str]) -> str:
    base = f"当前呈现{trend}、{momentum}，{volatility}，订单簿{liquidity}。"
    if risks:
        return base + " 主要注意：" + "；".join(risks[:2]) + "。"
    return base + " 暂未发现突出结构性风险，但单一时点不能替代持续观察。"


def _public_book(book: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    return {
        "asks": [{"price": price, "quantity": quantity} for price, quantity in _book_levels(book.get("asks"))],
        "bids": [{"price": price, "quantity": quantity} for price, quantity in _book_levels(book.get("bids"))],
    }


def _public_trade(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {
        "id": row.get("id"),
        "side": str(row.get("type", "")).upper(),
        "price": _number(row.get("price")),
        "amount": _number(row.get("amount")),
        "total": _number(row.get("total")),
        "time": row.get("time"),
    }
