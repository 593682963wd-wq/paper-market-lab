"""Isolated multi-strategy shadow engine with exact transaction-cost accounting."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from crypto_shadow_lab.accounting import CostModel, calculate_pnl
from crypto_shadow_lab.strategies import (
    STRATEGIES,
    TRADEABLE_STATES,
    MarketBar,
    MarketState,
    StrategyDecision,
    evaluate_strategy,
)

D = Decimal
ZERO = D("0")
DEFAULT_INITIAL_USDT = D("100")
DEFAULT_FEE_RATE = D("0.001")
DEFAULT_SLIPPAGE_RATE = D("0.001")
DEFAULT_RISK_PER_TRADE_PCT = D("1")
SCHEMA_VERSION = 1
STRATEGY_LABELS = {
    "current_shape": "当前结构基线",
    "trend_pullback": "趋势回调",
    "breakout": "突破跟随",
    "mean_reversion": "震荡回归",
    "buy_hold": "买入持有基准",
}
REGIME_LABELS = {
    "no_trade": "不可交易",
    "high_volatility": "高波动",
    "trend_up": "上涨趋势",
    "trend_down": "下跌趋势",
    "sideways": "区间震荡",
}


class ShadowStateError(RuntimeError):
    """Shadow state cannot be safely loaded or persisted."""


DecisionProvider = Callable[
    [str, Sequence[MarketBar], MarketState, dict[str, Any] | None],
    StrategyDecision,
]


@dataclass(frozen=True)
class ShadowEngineSettings:
    state_path: Path
    symbol: str = "SOL_USDT"
    period_seconds: int = 900
    initial_usdt: Decimal = DEFAULT_INITIAL_USDT
    fee_rate: Decimal = DEFAULT_FEE_RATE
    slippage_rate: Decimal = DEFAULT_SLIPPAGE_RATE
    risk_per_trade_pct: Decimal = DEFAULT_RISK_PER_TRADE_PCT
    cooldown_bars: int = 16
    max_fills_per_day: int = 6

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_path", Path(self.state_path))
        for field_name in (
            "initial_usdt",
            "fee_rate",
            "slippage_rate",
            "risk_per_trade_pct",
        ):
            value = D(str(getattr(self, field_name)))
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, value)
        if self.period_seconds <= 0:
            raise ValueError("period_seconds must be positive")
        if self.initial_usdt <= 0:
            raise ValueError("initial_usdt must be positive")
        if not (ZERO <= self.fee_rate < D("1")):
            raise ValueError("fee_rate must be between zero and one")
        if not (ZERO <= self.slippage_rate < D("1")):
            raise ValueError("slippage_rate must be between zero and one")
        if not (ZERO < self.risk_per_trade_pct <= D("100")):
            raise ValueError("risk_per_trade_pct must be in (0, 100]")
        if self.cooldown_bars < 0 or self.max_fills_per_day < 1:
            raise ValueError("invalid frequency limits")


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        number = D(str(value))
    except (ArithmeticError, ValueError):
        return D(default)
    return number if number.is_finite() else D(default)


def _strict_decimal(
    value: object,
    name: str,
    *,
    minimum: Decimal | None = None,
    strictly_greater: bool = False,
) -> Decimal:
    try:
        number = D(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ShadowStateError(f"{name} is not a valid decimal") from exc
    if not number.is_finite():
        raise ShadowStateError(f"{name} must be finite")
    if minimum is not None:
        invalid = number <= minimum if strictly_greater else number < minimum
        if invalid:
            comparison = "greater than" if strictly_greater else "at least"
            raise ShadowStateError(f"{name} must be {comparison} {minimum}")
    return number


def _finite_float(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ShadowStateError(f"{name} is not a valid number") from exc
    if not math.isfinite(number):
        raise ShadowStateError(f"{name} must be finite")
    return number


def _float(value: Decimal | object, digits: int = 8) -> float:
    number = float(_decimal(value))
    return round(number, digits)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ShadowStateError(
            f"cannot safely write shadow state: {path.name}"
        ) from exc


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowStateError(f"shadow state is corrupted: {path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ShadowStateError("shadow state schema is unsupported")
    return payload


class ShadowEngine:
    """Run five isolated ledgers on completed public candles only."""

    def __init__(
        self,
        settings: ShadowEngineSettings,
        *,
        decision_provider: DecisionProvider = evaluate_strategy,
        strategy_names: Sequence[str] | None = None,
    ) -> None:
        self.settings = settings
        self.decision_provider = decision_provider
        self.strategy_names = tuple(strategy_names or STRATEGIES.keys())
        unknown = [name for name in self.strategy_names if name not in STRATEGY_LABELS]
        if unknown:
            raise ValueError(f"unknown shadow strategies: {unknown}")
        self.cost_model = CostModel(
            fee_rate=settings.fee_rate,
            slippage_rate=settings.slippage_rate,
        )

    def process(
        self,
        bars: Sequence[MarketBar],
        market_state: MarketState,
        *,
        data_status: str = "live",
    ) -> dict[str, Any]:
        ordered = sorted(
            {bar.timestamp: bar for bar in bars}.values(), key=lambda bar: bar.timestamp
        )
        if not ordered:
            raise ValueError("at least one completed market bar is required")
        latest = ordered[-1]
        state = _load_json(self.settings.state_path)
        if state is None:
            state = self._new_state(latest, market_state, data_status=data_status)
            _atomic_write_json(self.settings.state_path, state)
            return self._summary(state)

        self._validate_state_identity(state)
        last_processed = int(state.get("last_processed_bar_ts") or 0)
        if latest.timestamp < last_processed:
            return self._summary(state)
        if latest.timestamp == last_processed:
            resolved_status = data_status
            if state.get("data_status") == "initializing" and data_status == "live":
                resolved_status = "initializing"
            candidate_market = self._market_payload(market_state, {**state, "last_observed_price": str(latest.close)})
            if (
                state.get("data_status") != resolved_status
                or state.get("market") != candidate_market
                or state.get("last_observed_price") != str(latest.close)
            ):
                state["data_status"] = resolved_status
                state["last_observed_price"] = str(latest.close)
                state["market"] = candidate_market
                _atomic_write_json(self.settings.state_path, state)
            return self._summary(state)

        new_bars = [bar for bar in ordered if bar.timestamp > last_processed]
        state["last_observed_price"] = str(latest.close)
        state["last_processed_bar_ts"] = latest.timestamp
        state["as_of"] = _iso_for_bar(latest.timestamp, self.settings.period_seconds)
        state["market"] = self._market_payload(market_state, state)

        previous_ts = last_processed
        missing = 0
        for bar in new_bars:
            missing += max(0, (bar.timestamp - previous_ts) // self.settings.period_seconds - 1)
            previous_ts = bar.timestamp
        if len(new_bars) > 1 or missing:
            # A backlog is deliberately skipped; absent intervals must also gate trading.
            missed = missing + (len(new_bars) if len(new_bars) > 1 else 0)
            state["missed_bars_total"] = (
                int(state.get("missed_bars_total") or 0) + missed
            )
            state["data_status"] = "degraded"
            state["market"] = self._market_payload(market_state, state)
            self._append_event(
                state,
                event_id=f"system:{latest.timestamp}:missed_bars",
                ts=latest.timestamp,
                kind="missed_bars",
                message=f"漏过 {missed} 根完整K线，本轮不补造历史成交。",
            )
            self._record_all_equities(state, latest.close, latest.timestamp)
            _atomic_write_json(self.settings.state_path, state)
            return self._summary(state)

        state["data_status"] = data_status
        current_bar = new_bars[0]
        bars_to_current = [
            bar for bar in ordered if bar.timestamp <= current_bar.timestamp
        ]
        for strategy_id in self.strategy_names:
            original = state["strategies"][strategy_id]
            working = deepcopy(original)
            try:
                self._process_strategy(
                    strategy_id,
                    working,
                    bars_to_current,
                    current_bar,
                    market_state,
                    allow_market_actions=data_status == "live",
                    state=state,
                )
                working["last_error"] = None
                state["strategies"][strategy_id] = working
            except Exception as exc:  # noqa: BLE001 -- isolate one strategy from peers.
                original["last_error"] = type(exc).__name__
                original["last_decision"] = "策略异常，本轮已隔离"
                self._append_event(
                    state,
                    event_id=f"{strategy_id}:{current_bar.timestamp}:error",
                    ts=current_bar.timestamp,
                    kind="strategy_error",
                    message=f"{STRATEGY_LABELS[strategy_id]} 本轮异常，其他策略继续运行。",
                    strategy_id=strategy_id,
                )
        self._record_all_equities(state, current_bar.close, current_bar.timestamp)
        _atomic_write_json(self.settings.state_path, state)
        return self._summary(state)

    def summary(self) -> dict[str, Any]:
        state = _load_json(self.settings.state_path)
        if state is None:
            return self._empty_summary()
        self._validate_state_identity(state)
        return self._summary(state)

    def _new_state(
        self,
        latest: MarketBar,
        market_state: MarketState,
        *,
        data_status: str,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mode": "shadow_public_only",
            "symbol": self.settings.symbol,
            "period_seconds": self.settings.period_seconds,
            "initial_usdt_per_strategy": str(self.settings.initial_usdt),
            "fee_rate": str(self.settings.fee_rate),
            "slippage_rate": str(self.settings.slippage_rate),
            "risk_per_trade_pct": str(self.settings.risk_per_trade_pct),
            "started_at": _iso_for_bar(latest.timestamp, self.settings.period_seconds),
            "as_of": _iso_for_bar(latest.timestamp, self.settings.period_seconds),
            "last_processed_bar_ts": latest.timestamp,
            "last_observed_price": str(latest.close),
            "data_status": "initializing" if data_status == "live" else data_status,
            "missed_bars_total": 0,
            "market": {},
            "strategies": {},
            "events": [],
        }
        state["market"] = self._market_payload(market_state, state)
        for strategy_id in self.strategy_names:
            state["strategies"][strategy_id] = {
                "strategy_id": strategy_id,
                "name": STRATEGY_LABELS[strategy_id],
                "cash_usdt": str(self.settings.initial_usdt),
                "position": None,
                "trades": [],
                "equity_curve": [],
                "fee_paid_usdt": "0",
                "slippage_paid_usdt": "0",
                "peak_equity_usdt": str(self.settings.initial_usdt),
                "max_drawdown_pct": 0.0,
                "last_decision": "等待下一根完整K线",
                "last_trade_bar_ts": None,
                "last_error": None,
            }
        self._append_event(
            state,
            event_id=f"system:{latest.timestamp}:initialized",
            ts=latest.timestamp,
            kind="initialized",
            message="影子盘已初始化；启动前不补做虚拟成交。",
        )
        return state

    def _validate_state_identity(self, state: dict[str, Any]) -> None:
        if state.get("symbol") != self.settings.symbol:
            raise ShadowStateError("shadow symbol differs from configured symbol")
        if int(state.get("period_seconds") or 0) != self.settings.period_seconds:
            raise ShadowStateError("shadow period differs from configured period")
        if set(state.get("strategies", {})) != set(self.strategy_names):
            raise ShadowStateError(
                "shadow strategy set differs from configured strategies"
            )
        identities = {
            "initial_usdt_per_strategy": self.settings.initial_usdt,
            "fee_rate": self.settings.fee_rate,
            "slippage_rate": self.settings.slippage_rate,
            "risk_per_trade_pct": self.settings.risk_per_trade_pct,
        }
        for field_name, expected in identities.items():
            actual = _strict_decimal(state.get(field_name), field_name, minimum=ZERO)
            if actual != expected:
                raise ShadowStateError(
                    f"shadow {field_name} differs from configured value"
                )
        _strict_decimal(
            state.get("last_observed_price"),
            "last_observed_price",
            minimum=ZERO,
            strictly_greater=True,
        )
        if not isinstance(state.get("events"), list):
            raise ShadowStateError("shadow events must be a list")
        for strategy_id, ledger in state["strategies"].items():
            self._validate_ledger(strategy_id, ledger)

    def _validate_ledger(self, strategy_id: str, ledger: object) -> None:
        if not isinstance(ledger, dict) or ledger.get("strategy_id") != strategy_id:
            raise ShadowStateError(f"shadow ledger identity is invalid: {strategy_id}")
        for field_name in ("cash_usdt", "fee_paid_usdt", "slippage_paid_usdt"):
            _strict_decimal(
                ledger.get(field_name),
                f"{strategy_id}.{field_name}",
                minimum=ZERO,
            )
        _strict_decimal(
            ledger.get("peak_equity_usdt"),
            f"{strategy_id}.peak_equity_usdt",
            minimum=ZERO,
            strictly_greater=True,
        )
        drawdown = _finite_float(
            ledger.get("max_drawdown_pct"), f"{strategy_id}.max_drawdown_pct"
        )
        if drawdown < 0:
            raise ShadowStateError(
                f"{strategy_id}.max_drawdown_pct must be non-negative"
            )
        trades = ledger.get("trades")
        curve = ledger.get("equity_curve")
        if not isinstance(trades, list) or not isinstance(curve, list):
            raise ShadowStateError(f"{strategy_id} trades/equity_curve must be lists")
        for trade in trades:
            if not isinstance(trade, dict) or trade.get("side") not in {"buy", "sell"}:
                raise ShadowStateError(f"{strategy_id} contains an invalid trade")
            if not isinstance(trade.get("id"), str) or not trade["id"]:
                raise ShadowStateError(f"{strategy_id} trade id is invalid")
            for field_name in (
                "reference_price",
                "fill_price",
                "coin_amount",
                "fee_usdt",
                "slippage_usdt",
            ):
                _strict_decimal(
                    trade.get(field_name),
                    f"{strategy_id}.trade.{field_name}",
                    minimum=ZERO,
                    strictly_greater=field_name
                    in {"reference_price", "fill_price", "coin_amount"},
                )
        position = ledger.get("position")
        if position is None:
            return
        if not isinstance(position, dict):
            raise ShadowStateError(f"{strategy_id}.position must be an object")
        for field_name in (
            "quantity",
            "entry_reference_price",
            "entry_fill_price",
            "quote_spent_usdt",
        ):
            _strict_decimal(
                position.get(field_name),
                f"{strategy_id}.position.{field_name}",
                minimum=ZERO,
                strictly_greater=True,
            )
        for field_name in (
            "stop_price",
            "target_price",
            "trailing_distance",
            "trailing_stop_price",
            "highest_close",
        ):
            _strict_decimal(
                position.get(field_name),
                f"{strategy_id}.position.{field_name}",
                minimum=ZERO,
            )

    def _process_strategy(
        self,
        strategy_id: str,
        ledger: dict[str, Any],
        bars: Sequence[MarketBar],
        bar: MarketBar,
        market_state: MarketState,
        *,
        allow_market_actions: bool,
        state: dict[str, Any],
    ) -> None:
        position = ledger.get("position")
        if position:
            exit_reason, exit_reference = self._intrabar_exit(position, bar)
            if exit_reason:
                self._sell(strategy_id, ledger, bar, exit_reference, exit_reason, state)
                return
            if not allow_market_actions:
                ledger["last_decision"] = "行情非实时，冻结策略动作"
                return
            decision = self.decision_provider(strategy_id, bars, market_state, position)
            ledger["last_decision"] = decision.reason
            if decision.action == "sell":
                self._sell(strategy_id, ledger, bar, bar.close, decision.reason, state)
                return
            self._advance_trailing(position, bar)
            return

        if not allow_market_actions or market_state.name not in TRADEABLE_STATES:
            ledger["last_decision"] = f"风控拦截新仓：{market_state.reason}"
            return
        decision = self.decision_provider(strategy_id, bars, market_state, None)
        ledger["last_decision"] = decision.reason
        if decision.action != "buy":
            return
        if self._entry_in_cooldown(ledger, bar.timestamp):
            ledger["last_decision"] = "冷却中，未模拟新开仓"
            return
        if self._fills_today(ledger, bar.timestamp) >= self.settings.max_fills_per_day:
            ledger["last_decision"] = "达到单日成交上限"
            return
        quote = self._quote_for_entry(
            strategy_id, ledger, bar.close, decision.stop_price
        )
        if quote < D("1"):
            ledger["last_decision"] = "风险预算低于1 USDT，未模拟开仓"
            return
        self._buy(strategy_id, ledger, bar, quote, decision, state)

    def _intrabar_exit(
        self, position: dict[str, Any], bar: MarketBar
    ) -> tuple[str | None, float]:
        stop_values = [
            _decimal(position.get("stop_price"), "0"),
            _decimal(position.get("trailing_stop_price"), "0"),
        ]
        stop = max(stop_values)
        target = _decimal(position.get("target_price"), "0")
        if stop > ZERO and D(str(bar.low)) <= stop:
            reference = min(D(str(bar.open)), stop)
            return "触发模拟保护止损", float(reference)
        if target > ZERO and D(str(bar.high)) >= target:
            reference = max(D(str(bar.open)), target)
            return "触发模拟目标退出", float(reference)
        return None, bar.close

    def _advance_trailing(self, position: dict[str, Any], bar: MarketBar) -> None:
        distance = _decimal(position.get("trailing_distance"), "0")
        if distance <= ZERO:
            return
        highest = max(_decimal(position.get("highest_close"), "0"), D(str(bar.close)))
        previous = _decimal(position.get("trailing_stop_price"), "0")
        candidate = max(ZERO, highest - distance)
        position["highest_close"] = str(highest)
        position["trailing_stop_price"] = str(max(previous, candidate))

    def _quote_for_entry(
        self,
        strategy_id: str,
        ledger: dict[str, Any],
        reference_price: float,
        stop_price: float | None,
    ) -> Decimal:
        cash = _decimal(ledger.get("cash_usdt"))
        if strategy_id == "buy_hold":
            return cash
        if stop_price is None:
            return ZERO
        reference = D(str(reference_price))
        stop = D(str(stop_price))
        if reference <= ZERO or stop <= ZERO or stop >= reference:
            return ZERO
        pnl = self._ledger_pnl(ledger, reference)
        risk_budget = pnl.equity_usdt * self.settings.risk_per_trade_pct / D("100")
        unit_buy = self.cost_model.buy(reference, D("1"))
        unit_exit = self.cost_model.sell(stop, unit_buy.coin_amount)
        loss_per_quote = D("1") - unit_exit.net_proceeds_usdt
        if loss_per_quote <= ZERO:
            return ZERO
        return min(cash, risk_budget / loss_per_quote)

    def _buy(
        self,
        strategy_id: str,
        ledger: dict[str, Any],
        bar: MarketBar,
        quote: Decimal,
        decision: StrategyDecision,
        state: dict[str, Any],
    ) -> None:
        event_id = self._trade_id(strategy_id, bar.timestamp, "buy")
        if self._trade_exists(ledger, event_id):
            return
        execution = self.cost_model.buy(D(str(bar.close)), quote)
        cash = _decimal(ledger.get("cash_usdt")) - quote
        ledger["cash_usdt"] = str(max(ZERO, cash))
        ledger["fee_paid_usdt"] = str(
            _decimal(ledger.get("fee_paid_usdt")) + execution.fee_usdt
        )
        ledger["slippage_paid_usdt"] = str(
            _decimal(ledger.get("slippage_paid_usdt")) + execution.slippage_usdt
        )
        trailing_distance = D(str(decision.trailing_stop or 0))
        trailing_stop = (
            max(ZERO, D(str(bar.close)) - trailing_distance)
            if trailing_distance > ZERO
            else ZERO
        )
        ledger["position"] = {
            "quantity": str(execution.coin_amount),
            "entry_reference_price": str(execution.reference_price),
            "entry_fill_price": str(execution.fill_price),
            "entry_ts": bar.timestamp,
            "quote_spent_usdt": str(quote),
            "stop_price": str(decision.stop_price or 0),
            "target_price": str(decision.target_price or 0),
            "trailing_distance": str(trailing_distance),
            "trailing_stop_price": str(trailing_stop),
            "highest_close": str(bar.close),
        }
        trade = {
            "id": event_id,
            "ts": bar.timestamp,
            "side": "buy",
            "reason": decision.reason,
            "reference_price": str(execution.reference_price),
            "fill_price": str(execution.fill_price),
            "quote_amount_usdt": str(execution.quote_amount_usdt),
            "coin_amount": str(execution.coin_amount),
            "fee_usdt": str(execution.fee_usdt),
            "slippage_usdt": str(execution.slippage_usdt),
            "net_pnl_usdt": None,
        }
        ledger["trades"].append(trade)
        ledger["last_trade_bar_ts"] = bar.timestamp
        self._append_event(
            state,
            event_id=event_id,
            ts=bar.timestamp,
            kind="trade",
            strategy_id=strategy_id,
            message=f"{STRATEGY_LABELS[strategy_id]} 模拟买入；买入手续费和滑点已计入。",
        )

    def _sell(
        self,
        strategy_id: str,
        ledger: dict[str, Any],
        bar: MarketBar,
        reference_price: float,
        reason: str,
        state: dict[str, Any],
    ) -> None:
        event_id = self._trade_id(strategy_id, bar.timestamp, "sell")
        if self._trade_exists(ledger, event_id):
            return
        position = ledger.get("position")
        if not position:
            return
        quantity = _decimal(position.get("quantity"))
        execution = self.cost_model.sell(D(str(reference_price)), quantity)
        cash = _decimal(ledger.get("cash_usdt")) + execution.net_proceeds_usdt
        ledger["cash_usdt"] = str(cash)
        ledger["fee_paid_usdt"] = str(
            _decimal(ledger.get("fee_paid_usdt")) + execution.fee_usdt
        )
        ledger["slippage_paid_usdt"] = str(
            _decimal(ledger.get("slippage_paid_usdt")) + execution.slippage_usdt
        )
        net_pnl = execution.net_proceeds_usdt - _decimal(
            position.get("quote_spent_usdt")
        )
        trade = {
            "id": event_id,
            "ts": bar.timestamp,
            "side": "sell",
            "reason": reason,
            "reference_price": str(execution.reference_price),
            "fill_price": str(execution.fill_price),
            "gross_proceeds_usdt": str(execution.gross_proceeds_usdt),
            "net_proceeds_usdt": str(execution.net_proceeds_usdt),
            "coin_amount": str(execution.coin_amount),
            "fee_usdt": str(execution.fee_usdt),
            "slippage_usdt": str(execution.slippage_usdt),
            "net_pnl_usdt": str(net_pnl),
        }
        ledger["trades"].append(trade)
        ledger["position"] = None
        ledger["last_trade_bar_ts"] = bar.timestamp
        self._append_event(
            state,
            event_id=event_id,
            ts=bar.timestamp,
            kind="trade",
            strategy_id=strategy_id,
            message=f"{STRATEGY_LABELS[strategy_id]} 模拟卖出；卖出手续费和滑点已计入。",
        )

    def _record_all_equities(
        self, state: dict[str, Any], price: float, ts: int
    ) -> None:
        for ledger in state["strategies"].values():
            pnl = self._ledger_pnl(ledger, D(str(price)))
            equity = pnl.equity_usdt
            peak = max(_decimal(ledger.get("peak_equity_usdt"), "0"), equity)
            drawdown = ((peak - equity) / peak * D("100")) if peak > ZERO else ZERO
            ledger["peak_equity_usdt"] = str(peak)
            ledger["max_drawdown_pct"] = max(
                float(ledger.get("max_drawdown_pct") or 0),
                float(drawdown),
            )
            ledger["equity_curve"] = (
                list(ledger.get("equity_curve") or [])
                + [{"ts": ts, "equity_usdt": str(equity), "price": str(price)}]
            )[-3000:]

    def _ledger_pnl(self, ledger: dict[str, Any], price: Decimal):
        position = ledger.get("position") or {}
        return calculate_pnl(
            initial_cash_usdt=self.settings.initial_usdt,
            cash_usdt=_decimal(ledger.get("cash_usdt")),
            coin_amount=_decimal(position.get("quantity")),
            current_reference_price=price,
            paid_fee_usdt=_decimal(ledger.get("fee_paid_usdt")),
            realized_slippage_usdt=_decimal(ledger.get("slippage_paid_usdt")),
            cost_model=self.cost_model,
        )

    def _entry_in_cooldown(self, ledger: dict[str, Any], ts: int) -> bool:
        last = ledger.get("last_trade_bar_ts")
        return bool(
            last is not None
            and ts - int(last)
            < self.settings.cooldown_bars * self.settings.period_seconds
        )

    def _fills_today(self, ledger: dict[str, Any], ts: int) -> int:
        day = ts // 86_400
        return sum(
            1
            for trade in ledger.get("trades", [])
            if int(trade.get("ts") or 0) // 86_400 == day
        )

    def _trade_id(self, strategy_id: str, ts: int, side: str) -> str:
        return f"{strategy_id}:{self.settings.symbol}:{self.settings.period_seconds}:{ts}:{side}"

    @staticmethod
    def _trade_exists(ledger: dict[str, Any], event_id: str) -> bool:
        return any(trade.get("id") == event_id for trade in ledger.get("trades", []))

    @staticmethod
    def _append_event(
        state: dict[str, Any],
        *,
        event_id: str,
        ts: int,
        kind: str,
        message: str,
        strategy_id: str | None = None,
    ) -> None:
        events = list(state.get("events") or [])
        if any(event.get("id") == event_id for event in events):
            return
        events.append(
            {
                "id": event_id,
                "ts": ts,
                "kind": kind,
                "strategy_id": strategy_id,
                "message": message,
            }
        )
        state["events"] = events[-200:]

    def _market_payload(
        self, market_state: MarketState, state: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "symbol": self.settings.symbol,
            "period_seconds": self.settings.period_seconds,
            "last_price": _float(state.get("last_observed_price")),
            "regime": market_state.name,
            "regime_label": REGIME_LABELS.get(market_state.name, market_state.name),
            "regime_reason": market_state.reason,
            "gate_reason": market_state.reason,
            "latest_complete_candle": state.get("as_of"),
            "missed_bars_total": int(state.get("missed_bars_total") or 0),
        }

    def _summary(self, state: dict[str, Any]) -> dict[str, Any]:
        price = _decimal(state.get("last_observed_price"), "1")
        strategy_rows = [
            self._strategy_summary(state["strategies"][strategy_id], price)
            for strategy_id in self.strategy_names
        ]
        strategy_rows.sort(key=lambda row: row["net_pnl_pct"], reverse=True)
        aggregate = self._aggregate_summary(strategy_rows)
        return {
            "ok": True,
            "mode": "shadow_public_only",
            "data_status": state.get("data_status", "initializing"),
            "as_of": state.get("as_of"),
            "aggregate": aggregate,
            "market": dict(state.get("market") or {}),
            "strategies": strategy_rows,
            "events": list(reversed(state.get("events", [])[-20:])),
            "methodology": [
                "每套策略使用独立虚拟本金，不共享仓位或资金。",
                "买入手续费、卖出手续费及买卖双边滑点均计入净盈亏。",
                "未平仓主数按当前价模拟卖出，并预扣退出手续费和滑点。",
                "影子盘不连接账户、不读取密钥、不产生真实订单。",
            ],
        }

    def _strategy_summary(
        self, ledger: dict[str, Any], price: Decimal
    ) -> dict[str, Any]:
        pnl = self._ledger_pnl(ledger, price)
        total_cost = (
            pnl.paid_fee_usdt
            + pnl.realized_slippage_usdt
            + pnl.estimated_exit_fee_usdt
            + pnl.estimated_exit_slippage_usdt
        )
        position = ledger.get("position")
        error = ledger.get("last_error")
        last_decision = str(ledger.get("last_decision") or "等待样本")
        if error:
            status = "策略异常"
        elif position:
            status = "模拟持仓"
        elif last_decision.startswith("风控拦截"):
            status = "风控拦截"
        else:
            status = "空仓观察"
        closed = [
            trade for trade in ledger.get("trades", []) if trade.get("side") == "sell"
        ]
        wins = [trade for trade in closed if _decimal(trade.get("net_pnl_usdt")) > ZERO]
        return {
            "strategy_id": ledger["strategy_id"],
            "name": ledger["name"],
            "status": status,
            "position_label": (
                f"持有 {_float(position.get('quantity'), 6)} SOL"
                if position
                else "空仓"
            ),
            "cash_usdt": _float(ledger.get("cash_usdt")),
            "gross_pnl_usdt": _float(pnl.gross_pnl_usdt),
            "actual_fee_usdt": _float(pnl.paid_fee_usdt),
            "actual_slippage_usdt": _float(pnl.realized_slippage_usdt),
            "estimated_exit_fee_usdt": _float(pnl.estimated_exit_fee_usdt),
            "estimated_exit_slippage_usdt": _float(pnl.estimated_exit_slippage_usdt),
            "total_cost_usdt": _float(total_cost),
            "net_liquidation_equity_usdt": _float(pnl.equity_usdt),
            "net_pnl_usdt": _float(pnl.net_pnl_usdt),
            "net_pnl_pct": _float(
                pnl.net_pnl_usdt / self.settings.initial_usdt * D("100")
            ),
            "max_drawdown_pct": round(float(ledger.get("max_drawdown_pct") or 0), 4),
            "last_decision": last_decision,
            "trade_count": len(ledger.get("trades", [])),
            "closed_trade_count": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
            "recent_trades": [
                self._public_trade(row) for row in ledger.get("trades", [])[-5:]
            ],
        }

    def _aggregate_summary(self, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        initial_total = self.settings.initial_usdt * len(rows)
        equity = sum(
            (_decimal(row["net_liquidation_equity_usdt"]) for row in rows), ZERO
        )
        gross_pnl = sum((_decimal(row["gross_pnl_usdt"]) for row in rows), ZERO)
        actual_fee = sum((_decimal(row["actual_fee_usdt"]) for row in rows), ZERO)
        actual_slippage = sum(
            (_decimal(row["actual_slippage_usdt"]) for row in rows), ZERO
        )
        estimated_fee = sum(
            (_decimal(row["estimated_exit_fee_usdt"]) for row in rows), ZERO
        )
        estimated_slippage = sum(
            (_decimal(row["estimated_exit_slippage_usdt"]) for row in rows), ZERO
        )
        total_cost = actual_fee + actual_slippage + estimated_fee + estimated_slippage
        net_pnl = equity - initial_total
        eligible = [row for row in rows if row["status"] != "策略异常"]
        any_trades = any(row["trade_count"] > 0 for row in eligible)
        leader = eligible[0] if eligible and any_trades else None
        return {
            "initial_total_usdt": _float(initial_total),
            "net_liquidation_equity_usdt": _float(equity),
            "gross_pnl_usdt": _float(gross_pnl),
            "actual_fee_usdt": _float(actual_fee),
            "actual_slippage_usdt": _float(actual_slippage),
            "estimated_exit_fee_usdt": _float(estimated_fee),
            "estimated_exit_slippage_usdt": _float(estimated_slippage),
            "total_cost_usdt": _float(total_cost),
            "net_pnl_usdt": _float(net_pnl),
            "net_pnl_pct": _float(net_pnl / initial_total * D("100")),
            "max_drawdown_pct": round(
                max((float(row["max_drawdown_pct"]) for row in rows), default=0.0),
                4,
            ),
            "leader_name": leader["name"] if leader else None,
            "strategy_count": len(rows),
        }

    @staticmethod
    def _public_trade(trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": trade.get("id"),
            "ts": trade.get("ts"),
            "side": trade.get("side"),
            "reason": trade.get("reason"),
            "reference_price": _float(trade.get("reference_price")),
            "fill_price": _float(trade.get("fill_price")),
            "fee_usdt": _float(trade.get("fee_usdt")),
            "slippage_usdt": _float(trade.get("slippage_usdt")),
            "net_pnl_usdt": (
                _float(trade.get("net_pnl_usdt"))
                if trade.get("net_pnl_usdt") is not None
                else None
            ),
        }

    def _empty_summary(self) -> dict[str, Any]:
        initial_total = self.settings.initial_usdt * len(self.strategy_names)
        return {
            "ok": True,
            "mode": "shadow_public_only",
            "data_status": "not_started",
            "as_of": None,
            "aggregate": {
                "initial_total_usdt": _float(initial_total),
                "net_liquidation_equity_usdt": _float(initial_total),
                "gross_pnl_usdt": 0.0,
                "actual_fee_usdt": 0.0,
                "actual_slippage_usdt": 0.0,
                "estimated_exit_fee_usdt": 0.0,
                "estimated_exit_slippage_usdt": 0.0,
                "total_cost_usdt": 0.0,
                "net_pnl_usdt": 0.0,
                "net_pnl_pct": 0.0,
                "leader_name": None,
                "strategy_count": len(self.strategy_names),
            },
            "market": {
                "symbol": self.settings.symbol,
                "period_seconds": self.settings.period_seconds,
                "last_price": None,
                "regime": "no_trade",
                "regime_label": REGIME_LABELS["no_trade"],
                "regime_reason": "not_started",
                "missed_bars_total": 0,
            },
            "strategies": [],
            "events": [],
            "methodology": ["影子盘尚未启动；没有虚拟成交。"],
        }


def _iso_for_bar(timestamp: int, period_seconds: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp + period_seconds, timezone.utc).isoformat()


__all__ = [
    "ShadowEngine",
    "ShadowEngineSettings",
    "ShadowStateError",
]
