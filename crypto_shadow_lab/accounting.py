"""Exact fee, slippage, and P&L accounting for the shadow trading lab."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

__all__ = [
    "BuyExecution",
    "CostModel",
    "PnLBreakdown",
    "SellExecution",
    "calculate_pnl",
]


ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
DEFAULT_FEE_RATE: Final = Decimal("0.001")
DEFAULT_SLIPPAGE_RATE: Final = Decimal("0.001")
USDT_QUANTUM: Final = Decimal("0.000000000000000001")


def _as_decimal(value: Decimal | int | str, *, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # Decimal raises several conversion-specific errors.
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value: Decimal | int | str, *, name: str) -> Decimal:
    result = _as_decimal(value, name=name)
    if result <= ZERO:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _non_negative(value: Decimal | int | str, *, name: str) -> Decimal:
    result = _as_decimal(value, name=name)
    if result < ZERO:
        raise ValueError(f"{name} must not be negative")
    return result


def _usdt(value: Decimal) -> Decimal:
    """Normalize monetary fields so displayed ledger values recompute exactly."""

    return value.quantize(USDT_QUANTUM)


@dataclass(frozen=True)
class BuyExecution:
    """A complete simulated buy paid from one quote-currency amount."""

    side: str
    reference_price: Decimal
    fill_price: Decimal
    fee_usdt: Decimal
    slippage_usdt: Decimal
    quote_amount_usdt: Decimal
    coin_amount: Decimal


@dataclass(frozen=True)
class SellExecution:
    """A complete simulated sale of one position quantity."""

    side: str
    reference_price: Decimal
    fill_price: Decimal
    fee_usdt: Decimal
    slippage_usdt: Decimal
    coin_amount: Decimal
    gross_proceeds_usdt: Decimal
    net_proceeds_usdt: Decimal


@dataclass(frozen=True)
class PnLBreakdown:
    """A mark-to-market result whose gross/net identity is directly reproducible."""

    equity_usdt: Decimal
    gross_pnl_usdt: Decimal
    paid_fee_usdt: Decimal
    realized_slippage_usdt: Decimal
    estimated_exit_fee_usdt: Decimal
    estimated_exit_slippage_usdt: Decimal
    estimated_exit_cost_usdt: Decimal
    net_pnl_usdt: Decimal


@dataclass(frozen=True)
class CostModel:
    """Apply configured fees and adverse slippage to complete buys and sells."""

    fee_rate: Decimal = DEFAULT_FEE_RATE
    slippage_rate: Decimal = DEFAULT_SLIPPAGE_RATE

    def __post_init__(self) -> None:
        fee_rate = _non_negative(self.fee_rate, name="fee_rate")
        slippage_rate = _non_negative(self.slippage_rate, name="slippage_rate")
        if fee_rate >= ONE:
            raise ValueError("fee_rate must be less than one")
        if slippage_rate >= ONE:
            raise ValueError("slippage_rate must be less than one")
        object.__setattr__(self, "fee_rate", fee_rate)
        object.__setattr__(self, "slippage_rate", slippage_rate)

    def buy(
        self,
        reference_price: Decimal | int | str,
        quote_amount: Decimal | int | str,
    ) -> BuyExecution:
        """Spend the full ``quote_amount`` and return the simulated coin fill."""

        reference = _positive(reference_price, name="reference_price")
        quote = _usdt(_positive(quote_amount, name="quote_amount"))
        fill_price = reference * (ONE + self.slippage_rate)
        fee_usdt = _usdt(quote * self.fee_rate)
        coin_amount = (quote - fee_usdt) / fill_price
        # Derive the quote loss from the actual rounded coin quantity so the
        # ledger identity remains exact under Decimal's finite division context.
        slippage_usdt = _usdt(quote - fee_usdt - (coin_amount * reference))
        return BuyExecution(
            side="buy",
            reference_price=reference,
            fill_price=fill_price,
            fee_usdt=fee_usdt,
            slippage_usdt=slippage_usdt,
            quote_amount_usdt=quote,
            coin_amount=coin_amount,
        )

    def sell(
        self,
        reference_price: Decimal | int | str,
        coin_amount: Decimal | int | str,
    ) -> SellExecution:
        """Sell the full ``coin_amount`` and return its net quote proceeds."""

        reference = _positive(reference_price, name="reference_price")
        coin = _positive(coin_amount, name="coin_amount")
        fill_price = reference * (ONE - self.slippage_rate)
        gross_proceeds = _usdt(coin * fill_price)
        fee_usdt = _usdt(gross_proceeds * self.fee_rate)
        slippage_usdt = _usdt(coin * (reference - fill_price))
        return SellExecution(
            side="sell",
            reference_price=reference,
            fill_price=fill_price,
            fee_usdt=fee_usdt,
            slippage_usdt=slippage_usdt,
            coin_amount=coin,
            gross_proceeds_usdt=gross_proceeds,
            net_proceeds_usdt=gross_proceeds - fee_usdt,
        )

    def estimate_liquidation(
        self,
        reference_price: Decimal | int | str,
        coin_amount: Decimal | int | str,
    ) -> SellExecution:
        """Estimate a full exit using the same fee/slippage rules as an actual sale."""

        return self.sell(reference_price=reference_price, coin_amount=coin_amount)


def calculate_pnl(
    *,
    initial_cash_usdt: Decimal | int | str,
    cash_usdt: Decimal | int | str,
    coin_amount: Decimal | int | str,
    current_reference_price: Decimal | int | str,
    paid_fee_usdt: Decimal | int | str,
    realized_slippage_usdt: Decimal | int | str,
    cost_model: CostModel,
) -> PnLBreakdown:
    """Calculate closed or estimated-liquidation P&L from explicit ledger totals."""

    initial_cash = _positive(initial_cash_usdt, name="initial_cash_usdt")
    cash = _non_negative(cash_usdt, name="cash_usdt")
    coin = _non_negative(coin_amount, name="coin_amount")
    current_price = _positive(current_reference_price, name="current_reference_price")
    paid_fee = _non_negative(paid_fee_usdt, name="paid_fee_usdt")
    realized_slippage = _non_negative(
        realized_slippage_usdt,
        name="realized_slippage_usdt",
    )

    if coin > ZERO:
        exit_estimate = cost_model.estimate_liquidation(
            reference_price=current_price,
            coin_amount=coin,
        )
        estimated_exit_fee = exit_estimate.fee_usdt
        estimated_exit_slippage = exit_estimate.slippage_usdt
        equity = cash + exit_estimate.net_proceeds_usdt
    else:
        estimated_exit_fee = ZERO
        estimated_exit_slippage = ZERO
        equity = cash

    estimated_exit_cost = estimated_exit_fee + estimated_exit_slippage
    net_pnl = equity - initial_cash
    gross_pnl = net_pnl + paid_fee + realized_slippage + estimated_exit_cost
    return PnLBreakdown(
        equity_usdt=equity,
        gross_pnl_usdt=gross_pnl,
        paid_fee_usdt=paid_fee,
        realized_slippage_usdt=realized_slippage,
        estimated_exit_fee_usdt=estimated_exit_fee,
        estimated_exit_slippage_usdt=estimated_exit_slippage,
        estimated_exit_cost_usdt=estimated_exit_cost,
        net_pnl_usdt=net_pnl,
    )
