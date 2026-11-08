"""模拟交易核心：可解释撮合、整数分账本、FIFO lot、T+1、幂等和并发保护。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Account,
    AssetSnapshot,
    CashLedgerEntry,
    Execution,
    Order,
    Position,
    PositionLot,
)
from . import market_data


class TradeError(Exception):
    """业务拒绝原因（中文，直接返回前端）。"""


_TRADE_LOCK = threading.RLock()
_CENT = Decimal("0.01")


def _to_cents(value: object) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise TradeError(f"金额格式错误：{value}") from exc
    if not amount.is_finite() or amount <= 0:
        raise TradeError(f"金额必须大于 0：{value}")
    return int((amount.quantize(_CENT, rounding=ROUND_HALF_UP) * 100))


def _money(cents: int | None) -> float:
    return float((Decimal(int(cents or 0)) / 100).quantize(_CENT, rounding=ROUND_HALF_UP))


def _unit_cost_from_total(total_cost_cents: int, quantity: int) -> int:
    """把精确总成本换算成展示单位成本；账本计算不得反向依赖该值。"""
    if quantity <= 0:
        return 0
    return int(
        (Decimal(total_cost_cents) / Decimal(quantity)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _lot_total_cost(lot: PositionLot) -> int:
    """读取 lot 精确总成本，并兼容迁移前仅有单位成本的内存对象。"""
    quantity = int(lot.quantity or 0)
    exact = lot.total_cost_cents
    if exact is not None and (int(exact) != 0 or quantity <= 0 or int(lot.cost_cents or 0) == 0):
        return int(exact)
    return quantity * int(lot.cost_cents or 0)


def _allocate_lot_cost(total_cost_cents: int, sold_quantity: int, lot_quantity: int) -> int:
    """按剩余 lot 比例半入分摊；最后一次卖清时承接全部尾差。"""
    if sold_quantity <= 0 or lot_quantity <= 0 or sold_quantity > lot_quantity:
        raise TradeError("持仓成本分摊参数无效")
    if sold_quantity == lot_quantity:
        return total_cost_cents
    allocated = int(
        (Decimal(total_cost_cents) * Decimal(sold_quantity) / Decimal(lot_quantity)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return min(total_cost_cents, max(0, allocated))


def _r2(value: object) -> float:
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def calc_fees(side: str, amount: float | Decimal) -> dict:
    """费用内部按 Decimal/整数分计算，返回元字段仅供展示兼容。"""
    if side not in ("buy", "sell"):
        raise TradeError("side 仅支持 buy/sell")
    amount_dec = Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)
    commission = max(
        amount_dec * Decimal(str(settings.commission_rate)),
        Decimal(str(settings.commission_min)),
    ).quantize(_CENT, rounding=ROUND_HALF_UP)
    transfer = (amount_dec * Decimal(str(settings.transfer_fee_rate))).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    tax = (
        (amount_dec * Decimal(str(settings.stamp_tax_rate))).quantize(_CENT, rounding=ROUND_HALF_UP)
        if side == "sell"
        else Decimal("0.00")
    )
    fee = (commission + transfer).quantize(_CENT, rounding=ROUND_HALF_UP)
    return {
        "commission": float(commission),
        "transfer": float(transfer),
        "tax": float(tax),
        "fee": float(fee),
        "commission_cents": int(commission * 100),
        "transfer_cents": int(transfer * 100),
        "tax_cents": int(tax * 100),
        "fee_cents": int(fee * 100),
    }


def _ensure_account_money(acct: Account) -> None:
    if acct.initial_cash_cents in (None, 0) and acct.initial_cash:
        acct.initial_cash_cents = _to_cents(acct.initial_cash)
    if acct.available_cash_cents is None:
        acct.available_cash_cents = _to_cents(acct.available_cash or 0) if acct.available_cash else 0
    if acct.initial_cash_cents:
        acct.initial_cash = _money(acct.initial_cash_cents)
    if acct.available_cash_cents is not None:
        acct.available_cash = _money(acct.available_cash_cents)


def _cash_balance_cents(db: Session, acct: Account) -> int:
    latest = (
        db.query(CashLedgerEntry)
        .filter(CashLedgerEntry.account_id == acct.id)
        .order_by(CashLedgerEntry.id.desc())
        .first()
    )
    if latest is not None:
        return int(latest.balance_after_cents)
    _ensure_account_money(acct)
    return int(acct.available_cash_cents or 0)


def _ensure_opening_ledger(db: Session, acct: Account) -> None:
    if db.query(CashLedgerEntry).filter(CashLedgerEntry.account_id == acct.id).first():
        return
    _ensure_account_money(acct)
    initial = int(acct.initial_cash_cents or 0)
    db.add(
        CashLedgerEntry(
            account_id=acct.id,
            entry_type="initial_cash",
            amount_cents=initial,
            balance_after_cents=initial,
            created_at=acct.created_at or datetime.now(),
        )
    )
    acct.available_cash_cents = initial
    acct.available_cash = _money(initial)


def _reserved_cash_cents(db: Session, account_id: int) -> int:
    rows = (
        db.query(Order.reserved_cash_cents)
        .filter(
            Order.account_id == account_id,
            Order.status.in_(("pending", "partial")),
        )
        .all()
    )
    return sum(int(row[0] or 0) for row in rows)


def available_cash_cents(db: Session, account_id: int) -> int:
    acct = db.get(Account, account_id)
    if acct is None:
        return 0
    return max(0, _cash_balance_cents(db, acct) - _reserved_cash_cents(db, account_id))


def get_or_create_account(
    db: Session, account_id: int | None = None, *, commit: bool = True
) -> Account:
    if account_id is not None:
        acct = db.get(Account, account_id)
        if acct:
            _ensure_account_money(acct)
            _ensure_opening_ledger(db, acct)
            if commit:
                db.commit()
            return acct
    acct = db.query(Account).order_by(Account.id.asc()).first()
    if acct is None:
        initial = _to_cents(settings.initial_cash)
        acct = Account(
            name="模拟账户",
            initial_cash=_money(initial),
            available_cash=_money(initial),
            initial_cash_cents=initial,
            available_cash_cents=initial,
        )
        db.add(acct)
        db.flush()
        db.add(
            CashLedgerEntry(
                account_id=acct.id,
                entry_type="initial_cash",
                amount_cents=initial,
                balance_after_cents=initial,
                created_at=acct.created_at or datetime.now(),
            )
        )
        db.add(
            AssetSnapshot(
                account_id=acct.id,
                cash=_money(initial),
                market_value=0.0,
                total_assets=_money(initial),
                valuation_complete=True,
                valuation_note="无持仓，仅现金",
                source_summary=json.dumps(
                    {
                        "no_positions": True,
                        "latest_quote": [],
                        "stale_quote": [],
                        "cost_basis_fallback": [],
                        "unknown": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
    else:
        _ensure_account_money(acct)
        _ensure_opening_ledger(db, acct)
    if commit:
        db.commit()
        db.refresh(acct)
    return acct


def _begin_immediate(db: Session) -> None:
    """在 Session 尚未执行查询时打开 SQLite 写事务。"""
    if db.in_transaction():
        db.rollback()
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def today_bought_quantity(db: Session, account_id: int, symbol: str) -> int:
    """按订单创建时间判定 T+1；保留旧订单兼容性和可回放性。"""
    today = datetime.now().date()
    orders = (
        db.query(Order)
        .filter(
            Order.account_id == account_id,
            Order.symbol == symbol,
            Order.side == "buy",
            Order.status.in_(("filled", "partial")),
        )
        .all()
    )
    return sum(
        int(o.filled_quantity or o.quantity or 0)
        for o in orders
        if o.created_at and o.created_at.date() == today
    )


def get_available_quantity(db: Session, account_id: int, symbol: str) -> int:
    pos = (
        db.query(Position)
        .filter(Position.account_id == account_id, Position.symbol == symbol)
        .first()
    )
    if pos is None:
        return 0
    pending_sell = (
        db.query(Order.remaining_quantity)
        .filter(
            Order.account_id == account_id,
            Order.symbol == symbol,
            Order.side == "sell",
            Order.status.in_(("pending", "partial")),
        )
        .all()
    )
    reserved = sum(int(row[0] or 0) for row in pending_sell)
    return max(0, int(pos.quantity or 0) - today_bought_quantity(db, account_id, symbol) - reserved)


def _request_hash(
    symbol: str,
    side: str,
    quantity: int,
    price: object,
    order_type: str,
    trade_reason: str | None = None,
    invalid_condition: str | None = None,
) -> str:
    """按实际订单语义生成摘要，忽略市价单价格并把限价统一到整数分。"""
    body = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price_cents": (
            None
            if order_type == "market" or price is None
            else _to_cents(price)
        ),
        "order_type": order_type,
        "trade_reason": trade_reason or "",
        "invalid_condition": invalid_condition or "",
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _legacy_request_hash(
    symbol: str,
    side: str,
    quantity: int,
    price: object,
    order_type: str,
    trade_reason: str | None = None,
    invalid_condition: str | None = None,
) -> str:
    """复现 V3 原始字符串摘要，仅用于兼容升级前已保存的订单。"""
    body = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": None if price is None else str(price),
        "order_type": order_type,
        "trade_reason": trade_reason or "",
        "invalid_condition": invalid_condition or "",
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _stored_order_request_hash(order: Order) -> str:
    """从订单原始字段恢复请求摘要，兼容早期未写 request_hash 的记录。"""
    order_type = order.order_type or "limit"
    if order_type == "limit":
        request_price = (
            _money(order.limit_price_cents)
            if order.limit_price_cents is not None
            else order.price
        )
    else:
        request_price = None
    return _request_hash(
        order.symbol,
        order.side,
        int(order.quantity or 0),
        request_price,
        order_type,
        order.trade_reason,
        order.invalid_condition,
    )


def _assert_idempotent_replay(
    previous: Order,
    request_hash: str,
    legacy_request_hash: str,
) -> None:
    """仅允许与历史订单完全相同的请求复用幂等键。"""
    if previous.request_hash:
        matches = previous.request_hash in {request_hash, legacy_request_hash}
    else:
        matches = _stored_order_request_hash(previous) == request_hash
    if not matches:
        raise TradeError("幂等键已用于不同订单请求")


def _validate_sell_quantity(quantity: int, available: int) -> None:
    if quantity <= 0:
        raise TradeError("卖出数量必须大于 0")
    remainder = available % 100
    if quantity % 100 == 0 or quantity == available or (remainder and quantity == remainder):
        return
    raise TradeError("卖出不足 100 股时必须一次性卖出剩余零股")


def _quote_level(quote: dict) -> str:
    return str(quote.get("market_data_level") or quote.get("data_level") or "snapshot")


def _assert_tradeable_quote(quote: dict) -> None:
    source = str(quote.get("source_type") or "")
    level = _quote_level(quote)
    if (
        quote.get("stale")
        or source in {"cache", "historical", "daily"}
        or level in {"historical", "daily"}
        or not market_data.quote_is_tradeable(quote)
    ):
        raise TradeError(
            "当前不在可核对交易时段，或行情陈旧/时间已经过期；仅可浏览，禁止模拟成交"
        )


def _limit_cents(prev_close: object, pct: float, *, up: bool) -> int:
    base = Decimal(str(prev_close or 0))
    value = base * (Decimal("1") + (Decimal(str(pct)) if up else -Decimal(str(pct))))
    return int(value.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


def _match_order(
    quote: dict,
    side: str,
    order_type: str,
    limit_price_cents: int | None,
    limit_up_cents: int,
    limit_down_cents: int,
) -> tuple[str, int | None, str, str]:
    latest_cents = _to_cents(quote["price"])
    best_ask = quote.get("best_ask")
    best_bid = quote.get("best_bid")
    best_ask_cents = _to_cents(best_ask) if best_ask else None
    best_bid_cents = _to_cents(best_bid) if best_bid else None
    if side == "buy" and latest_cents >= limit_up_cents and best_ask_cents is None:
        return "rejected", None, "snapshot", "涨停无可核对盘口对手盘，模拟器保守不成交"
    if side == "sell" and latest_cents <= limit_down_cents and best_bid_cents is None:
        return "rejected", None, "snapshot", "跌停无可核对盘口对手盘，模拟器保守不成交"
    if order_type == "market":
        return "filled", latest_cents, _quote_level(quote), "最新快照市价简化成交；不代表交易所真实撮合"
    if limit_price_cents is None:
        raise TradeError("限价单必须填写有效委托价")
    if side == "buy":
        threshold = best_ask_cents or latest_cents
        if limit_price_cents < threshold:
            return "pending", None, _quote_level(quote), "简化撮合：买入委托价低于可成交卖价，等待穿价"
        return "filled", min(limit_price_cents, threshold), _quote_level(quote), "简化撮合：买入委托已穿过可成交卖价"
    threshold = best_bid_cents or latest_cents
    if limit_price_cents > threshold:
        return "pending", None, _quote_level(quote), "简化撮合：卖出委托价高于可成交买价，等待穿价"
    return "filled", max(limit_price_cents, threshold), _quote_level(quote), "简化撮合：卖出委托已穿过可成交买价"


def _new_order(
    *, acct: Account, symbol: str, name: str, side: str, order_type: str,
    quantity: int, display_price_cents: int | None, limit_price_cents: int | None,
    status: str, reject_reason: str = "", fee_cents: int = 0, tax_cents: int = 0,
    filled_quantity: int = 0, remaining_quantity: int | None = None,
    reserved_cash_cents: int = 0, idempotency_key: str | None = None,
    request_hash: str | None = None, market_data_level: str | None = None,
    simulation_assumption: str | None = None, trade_reason: str | None = None,
    invalid_condition: str | None = None, expires_at: datetime | None = None,
) -> Order:
    return Order(
        account_id=acct.id,
        symbol=symbol,
        name=name,
        side=side,
        order_type=order_type,
        price=_money(display_price_cents) if display_price_cents else None,
        quantity=quantity,
        status=status,
        reject_reason=reject_reason,
        fee=_money(fee_cents),
        tax=_money(tax_cents),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        limit_price_cents=limit_price_cents,
        filled_quantity=filled_quantity,
        remaining_quantity=quantity - filled_quantity if remaining_quantity is None else remaining_quantity,
        reserved_cash_cents=reserved_cash_cents,
        market_data_level=market_data_level,
        simulation_assumption=simulation_assumption,
        trade_reason=trade_reason,
        invalid_condition=invalid_condition,
        expires_at=(
            expires_at
            if expires_at is not None
            else (datetime.now() + timedelta(days=1) if status in ("pending", "partial") else None)
        ),
    )


def _add_legacy_lot_if_needed(db: Session, acct: Account, symbol: str) -> None:
    pos = (
        db.query(Position)
        .filter(Position.account_id == acct.id, Position.symbol == symbol)
        .first()
    )
    if pos is None or int(pos.quantity or 0) <= 0:
        return
    lots = (
        db.query(PositionLot)
        .filter(PositionLot.account_id == acct.id, PositionLot.symbol == symbol)
        .all()
    )
    known_quantity = sum(int(lot.quantity or 0) for lot in lots)
    known_total_cost = sum(_lot_total_cost(lot) for lot in lots)
    missing = int(pos.quantity) - known_quantity
    if missing <= 0:
        return
    cost = int(pos.avg_cost_cents or _to_cents(pos.avg_cost or 0))
    projected_total = int(pos.total_cost_cents or 0)
    missing_total_cost = (
        projected_total - known_total_cost
        if projected_total > known_total_cost
        else missing * cost
    )
    db.add(
        PositionLot(
            account_id=acct.id,
            symbol=symbol,
            name=pos.name,
            quantity=missing,
            available_quantity=missing,
            cost_cents=cost,
            total_cost_cents=missing_total_cost,
            acquired_at=pos.created_at or datetime.now(),
        )
    )
    db.flush()


def _consume_lots(db: Session, acct: Account, symbol: str, quantity: int) -> int:
    _add_legacy_lot_if_needed(db, acct, symbol)
    lots = (
        db.query(PositionLot)
        .filter(
            PositionLot.account_id == acct.id,
            PositionLot.symbol == symbol,
            PositionLot.quantity > 0,
        )
        .order_by(PositionLot.acquired_at.asc(), PositionLot.id.asc())
        .all()
    )
    locked = today_bought_quantity(db, acct.id, symbol)
    locked_by_lot: dict[int, int] = {}
    for lot in sorted(lots, key=lambda row: (row.acquired_at or datetime.min, row.id), reverse=True):
        if locked <= 0:
            break
        amount = min(int(lot.quantity), locked)
        locked_by_lot[lot.id] = amount
        locked -= amount
    remaining = quantity
    cost = 0
    for lot in lots:
        original_quantity = int(lot.quantity)
        eligible = original_quantity - locked_by_lot.get(lot.id, 0)
        if eligible <= 0:
            continue
        take = min(eligible, remaining)
        original_total_cost = _lot_total_cost(lot)
        allocated_cost = _allocate_lot_cost(original_total_cost, take, original_quantity)
        lot.quantity -= take
        lot.total_cost_cents = original_total_cost - allocated_cost
        lot.cost_cents = _unit_cost_from_total(
            int(lot.total_cost_cents), int(lot.quantity)
        )
        lot.available_quantity = max(0, int(lot.quantity) - locked_by_lot.get(lot.id, 0))
        cost += allocated_cost
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise TradeError("持仓 lot 不足，无法重建可卖数量")
    return cost


def _sync_position_projection(db: Session, acct: Account, symbol: str, name: str = "") -> None:
    db.flush()
    pos = (
        db.query(Position)
        .filter(Position.account_id == acct.id, Position.symbol == symbol)
        .first()
    )
    lots = (
        db.query(PositionLot)
        .filter(PositionLot.account_id == acct.id, PositionLot.symbol == symbol, PositionLot.quantity > 0)
        .all()
    )
    quantity = sum(int(lot.quantity) for lot in lots)
    if quantity <= 0:
        if pos is not None:
            db.delete(pos)
        return
    total_cost = sum(_lot_total_cost(lot) for lot in lots)
    if pos is None:
        pos = Position(account_id=acct.id, symbol=symbol, name=name or symbol)
        db.add(pos)
    pos.name = name or pos.name or symbol
    pos.quantity = quantity
    pos.total_cost_cents = total_cost
    pos.avg_cost_cents = _unit_cost_from_total(total_cost, quantity)
    pos.avg_cost = _money(pos.avg_cost_cents)
    pos.available_quantity = get_available_quantity(db, acct.id, symbol)
    pos.reserved_quantity = max(0, quantity - pos.available_quantity - today_bought_quantity(db, acct.id, symbol))


def _record_execution(
    db: Session, acct: Account, order: Order, *, price_cents: int, fee_cents: int,
    tax_cents: int, market_data_level: str, simulation_assumption: str,
) -> None:
    gross = price_cents * order.quantity
    amount = -(gross + fee_cents) if order.side == "buy" else gross - fee_cents - tax_cents
    balance = _cash_balance_cents(db, acct) + amount
    db.add(
        Execution(
            order_id=order.id,
            account_id=acct.id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price_cents=price_cents,
            gross_cents=gross,
            fee_cents=fee_cents,
            tax_cents=tax_cents,
            market_data_level=market_data_level,
            simulation_assumption=simulation_assumption,
        )
    )
    db.add(
        CashLedgerEntry(
            account_id=acct.id,
            order_id=order.id,
            entry_type="trade_buy" if order.side == "buy" else "trade_sell",
            amount_cents=amount,
            balance_after_cents=balance,
            idempotency_key=order.idempotency_key,
        )
    )
    acct.available_cash_cents = balance
    acct.available_cash = _money(balance)


def _snapshot_valuation(db: Session, account_id: int) -> tuple[float, bool, str, dict]:
    """按实际行情可用性计算快照市值，并返回可持久化的来源摘要。"""
    positions = db.query(Position).filter(Position.account_id == account_id).all()
    source_summary = {
        "no_positions": not positions,
        "latest_quote": [],
        "stale_quote": [],
        "cost_basis_fallback": [],
        "unknown": False,
    }
    if not positions:
        return 0.0, True, "无持仓，仅现金", source_summary
    quotes = market_data.get_spot_batch([p.symbol for p in positions])
    total_cents = 0
    for pos in positions:
        quote = quotes.get(pos.symbol)
        quote_price = quote.get("price") if quote else None
        if isinstance(quote_price, (int, float)) and not isinstance(quote_price, bool) and quote_price > 0:
            price_cents = _to_cents(quote_price)
            total_cents += price_cents * int(pos.quantity or 0)
            source_key = "stale_quote" if quote.get("stale") else "latest_quote"
            source_summary[source_key].append(pos.symbol)
            continue

        exact_total_cost = int(pos.total_cost_cents or 0)
        if exact_total_cost <= 0:
            exact_total_cost = int(pos.avg_cost_cents or 0) * int(pos.quantity or 0)
        total_cents += exact_total_cost
        source_summary["cost_basis_fallback"].append(pos.symbol)

    missing = source_summary["cost_basis_fallback"]
    stale = source_summary["stale_quote"]
    if missing and stale:
        note = "部分持仓缺少行情且部分行情陈旧；缺行情部分按持仓成本估算，整体不代表实时估值"
    elif missing:
        note = "部分持仓缺少行情，相关市值按持仓成本估算，不代表实时估值"
    elif stale:
        note = "部分持仓仅有陈旧行情，资产估值不代表当前实时价格"
    else:
        note = "全部持仓均按当前可核对行情估值"
    return _money(total_cents), not missing and not stale, note, source_summary


def _append_snapshot(db: Session, acct: Account) -> None:
    db.flush()
    cash = _cash_balance_cents(db, acct)
    market_value, valuation_complete, valuation_note, source_summary = _snapshot_valuation(
        db, acct.id
    )
    db.add(
        AssetSnapshot(
            account_id=acct.id,
            cash=_money(cash),
            market_value=market_value,
            total_assets=_r2(_money(cash) + market_value),
            valuation_complete=valuation_complete,
            valuation_note=valuation_note,
            source_summary=json.dumps(
                source_summary,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )


def record_asset_snapshot(db: Session, account_id: int | None = None) -> None:
    """主动写入一条资产快照，供定时任务与验收调用。"""
    acct = get_or_create_account(db, account_id)
    _append_snapshot(db, acct)
    db.commit()


def preview_order(
    db: Session,
    symbol: str,
    side: str,
    quantity: int,
    price: float | None = None,
    order_type: str = "limit",
    account_id: int | None = None,
) -> dict:
    """预检模拟委托，不创建订单，也不改动现金、持仓和事件账本。"""
    symbol = market_data.normalize_symbol(symbol)
    if side not in ("buy", "sell"):
        raise TradeError("side 仅支持 buy/sell")
    if order_type not in ("limit", "market"):
        raise TradeError("order_type 仅支持 limit/market")
    if quantity <= 0:
        raise TradeError("数量必须大于 0")
    if side == "buy" and quantity % 100 != 0:
        raise TradeError("买入数量必须是 100 股的整数倍")
    if order_type == "limit" and (price is None or price <= 0):
        raise TradeError("限价单必须填写有效委托价")

    quote = market_data.get_spot_row(symbol)
    if quote is None:
        raise TradeError("该股票停牌或无行情数据，暂不可交易")
    _assert_tradeable_quote(quote)
    if quote.get("price") is None or quote["price"] <= 0:
        raise TradeError("该股票当前无有效价格（可能停牌）")

    prev_close = quote.get("prev_close") or quote["price"]
    pct = market_data.limit_pct(symbol, quote.get("name", ""))
    limit_up_cents = _limit_cents(prev_close, pct, up=True)
    limit_down_cents = _limit_cents(prev_close, pct, up=False)
    limit_price_cents = _to_cents(price) if order_type == "limit" else None
    if limit_price_cents is not None and not (
        limit_down_cents <= limit_price_cents <= limit_up_cents
    ):
        raise TradeError(
            "委托价必须在当日价格范围 "
            f"{_money(limit_down_cents):,.2f}—{_money(limit_up_cents):,.2f} 元内"
        )

    status, fill_price_cents, level, assumption = _match_order(
        quote,
        side,
        order_type,
        limit_price_cents,
        limit_up_cents,
        limit_down_cents,
    )
    acct = get_or_create_account(db, account_id)
    cash_cents = available_cash_cents(db, acct.id)
    available_quantity = get_available_quantity(db, acct.id, symbol)
    position = (
        db.query(Position)
        .filter(Position.account_id == acct.id, Position.symbol == symbol)
        .first()
    )
    position_quantity = int(position.quantity or 0) if position else 0
    checks: list[dict[str, str]] = [
        {
            "level": "pass",
            "label": "行情可核对",
            "detail": (
                f"{market_data_level_label(level)}，采集时间 "
                f"{quote.get('captured_at') or '未标注'}"
            ),
        }
    ]
    block_reason = ""

    if side == "sell":
        if quantity > available_quantity:
            locked = today_bought_quantity(db, acct.id, symbol)
            block_reason = (
                f"可卖数量不足（T+1：今日买入 {locked} 股不可卖出）"
                if locked > 0
                else f"持仓不足：可卖 {available_quantity} 股，想卖 {quantity} 股"
            )
        else:
            try:
                _validate_sell_quantity(quantity, available_quantity)
            except TradeError as error:
                block_reason = str(error)
        checks.append(
            {
                "level": "block" if block_reason else "pass",
                "label": "持仓与 T+1",
                "detail": block_reason or f"当前可卖 {available_quantity} 股",
            }
        )

    estimate_price_cents = (
        fill_price_cents
        or limit_price_cents
        or _to_cents(quote["price"])
    )
    amount_cents = estimate_price_cents * quantity
    fees = calc_fees(side, _money(amount_cents))
    total_cost_cents = amount_cents + fees["fee_cents"]
    proceeds_cents = amount_cents - fees["fee_cents"] - fees["tax_cents"]
    if side == "buy":
        if total_cost_cents > cash_cents:
            block_reason = (
                f"可用资金不足：预计需要 {_money(total_cost_cents):,.2f} 元，"
                f"当前可用 {_money(cash_cents):,.2f} 元"
            )
        checks.append(
            {
                "level": "block" if total_cost_cents > cash_cents else "pass",
                "label": "可用资金",
                "detail": block_reason or f"提交后预计剩余 {_money(cash_cents - total_cost_cents):,.2f} 元",
            }
        )

    if status == "pending":
        checks.append(
            {
                "level": "warn",
                "label": "预计进入挂单",
                "detail": assumption,
            }
        )
    elif status == "filled":
        checks.append(
            {
                "level": "pass",
                "label": "预计立即成交",
                "detail": assumption,
            }
        )
    else:
        block_reason = block_reason or assumption
        checks.append(
            {
                "level": "block",
                "label": "撮合条件",
                "detail": assumption,
            }
        )

    can_submit = not block_reason and status != "rejected"
    effective_status = status if can_submit else "rejected"
    if not can_submit:
        cash_after_cents = cash_cents
        position_after = position_quantity
        available_quantity_after = available_quantity
    elif status == "pending":
        cash_after_cents = cash_cents - total_cost_cents if side == "buy" else cash_cents
        position_after = position_quantity
        available_quantity_after = (
            available_quantity - quantity if side == "sell" else available_quantity
        )
    else:
        cash_after_cents = (
            cash_cents - total_cost_cents
            if side == "buy"
            else cash_cents + proceeds_cents
        )
        position_after = (
            position_quantity + quantity
            if side == "buy"
            else position_quantity - quantity
        )
        available_quantity_after = (
            available_quantity - quantity if side == "sell" else available_quantity
        )
    return {
        "symbol": symbol,
        "name": quote.get("name", symbol),
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "quote_price": _r2(quote["price"]),
        "expected_price": _money(estimate_price_cents),
        "expected_status": effective_status,
        "amount": _money(amount_cents),
        "fee": _money(fees["fee_cents"]),
        "tax": _money(fees["tax_cents"]),
        "total_cost": _money(total_cost_cents) if side == "buy" else None,
        "net_proceeds": _money(proceeds_cents) if side == "sell" else None,
        "available_cash": _money(cash_cents),
        "cash_after": _money(cash_after_cents),
        "position_quantity": position_quantity,
        "available_quantity": available_quantity,
        "position_after": position_after,
        "available_quantity_after": available_quantity_after,
        "limit_up": _money(limit_up_cents),
        "limit_down": _money(limit_down_cents),
        "market_data_level": level,
        "source_type": quote.get("source_type"),
        "captured_at": quote.get("captured_at"),
        "stale": bool(quote.get("stale")),
        "simulation_assumption": assumption,
        "checks": checks,
        "can_submit": can_submit,
        "block_reason": block_reason or None,
    }


def market_data_level_label(level: str) -> str:
    """把行情层级转换为预检可读文本。"""
    labels = {
        "snapshot": "当前行情快照",
        "realtime": "实时行情",
        "historical": "历史行情",
        "daily": "日线行情",
    }
    return labels.get(level, level or "行情层级未标注")


def place_order(
    db: Session,
    symbol: str,
    side: str,
    quantity: int,
    price: float | None = None,
    order_type: str = "limit",
    account_id: int | None = None,
    idempotency_key: str | None = None,
    trade_reason: str | None = None,
    invalid_condition: str | None = None,
) -> dict:
    """校验、快照撮合、挂单或成交；所有资金变化在同一写事务内完成。"""
    symbol = market_data.normalize_symbol(symbol)
    if side not in ("buy", "sell"):
        raise TradeError("side 仅支持 buy/sell")
    if order_type not in ("limit", "market"):
        raise TradeError("order_type 仅支持 limit/market")
    if quantity <= 0:
        raise TradeError("数量必须大于 0")
    if side == "buy" and quantity % 100 != 0:
        raise TradeError("买入数量必须是 100 股的整数倍")
    if order_type == "limit" and (price is None or price <= 0):
        raise TradeError("限价单必须填写有效委托价")
    if idempotency_key and len(idempotency_key) > 128:
        raise TradeError("幂等键长度不能超过 128 个字符")
    trade_reason = trade_reason.strip() if trade_reason else None
    invalid_condition = invalid_condition.strip() if invalid_condition else None
    if not trade_reason:
        raise TradeError("必须填写本次模拟委托的交易理由")
    if not invalid_condition:
        raise TradeError("必须填写本次交易假设的失效条件")
    if trade_reason and len(trade_reason) > 500:
        raise TradeError("交易理由不能超过 500 个字符")
    if invalid_condition and len(invalid_condition) > 500:
        raise TradeError("失效条件不能超过 500 个字符")

    request_hash = _request_hash(
        symbol, side, quantity, price, order_type, trade_reason, invalid_condition
    )
    legacy_request_hash = _legacy_request_hash(
        symbol, side, quantity, price, order_type, trade_reason, invalid_condition
    )
    # 响应丢失后的同请求重试必须先返回已有结果，不能再次依赖此刻的行情状态。
    # 新订单仍会在写锁内二次检查同一幂等键，避免并发重复写入。
    if idempotency_key:
        existing_account = get_or_create_account(db, account_id)
        previous = (
            db.query(Order)
            .filter(
                Order.account_id == existing_account.id,
                Order.idempotency_key == idempotency_key,
            )
            .first()
        )
        if previous is not None:
            _assert_idempotent_replay(previous, request_hash, legacy_request_hash)
            return _order_payload(previous)

    quote = market_data.get_spot_row(symbol)
    if quote is None:
        raise TradeError("该股票停牌或无行情数据，暂不可交易")
    _assert_tradeable_quote(quote)
    if quote.get("price") is None or quote["price"] <= 0:
        raise TradeError("该股票当前无有效价格（可能停牌）")

    with _TRADE_LOCK:
        try:
            _begin_immediate(db)
            acct = get_or_create_account(db, account_id, commit=False)
            if idempotency_key:
                previous = (
                    db.query(Order)
                    .filter(Order.account_id == acct.id, Order.idempotency_key == idempotency_key)
                    .first()
                )
                if previous is not None:
                    _assert_idempotent_replay(previous, request_hash, legacy_request_hash)
                    db.commit()
                    return _order_payload(previous)

            available = get_available_quantity(db, acct.id, symbol) if side == "sell" else None
            if side == "sell" and available is not None:
                if quantity > available:
                    locked = today_bought_quantity(db, acct.id, symbol)
                    if locked > 0:
                        reason = f"可卖数量不足（T+1 限制：今日买入 {locked} 股不可卖出）"
                    else:
                        reason = f"持仓不足：可卖 {available} 股，想卖 {quantity} 股"
                    order = _new_order(
                        acct=acct,
                        symbol=symbol,
                        name=quote.get("name", symbol),
                        side=side,
                        order_type=order_type,
                        quantity=quantity,
                        display_price_cents=(
                            _to_cents(price)
                            if order_type == "limit" and price is not None
                            else _to_cents(quote["price"])
                        ),
                        limit_price_cents=(
                            _to_cents(price)
                            if order_type == "limit" and price is not None
                            else None
                        ),
                        status="rejected",
                        reject_reason=reason,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        market_data_level=_quote_level(quote),
                        simulation_assumption="下单前持仓与 T+1 检查未通过",
                        trade_reason=trade_reason,
                        invalid_condition=invalid_condition,
                    )
                    db.add(order)
                    db.flush()
                    db.commit()
                    return _order_payload(order)
                _validate_sell_quantity(quantity, available)

            prev_close = quote.get("prev_close") or quote["price"]
            pct = market_data.limit_pct(symbol, quote.get("name", ""))
            limit_up_cents = _limit_cents(prev_close, pct, up=True)
            limit_down_cents = _limit_cents(prev_close, pct, up=False)
            limit_price_cents = _to_cents(price) if order_type == "limit" else None
            if limit_price_cents is not None and not (
                limit_down_cents <= limit_price_cents <= limit_up_cents
            ):
                raise TradeError(
                    "委托价必须在当日价格范围 "
                    f"{_money(limit_down_cents):,.2f}—{_money(limit_up_cents):,.2f} 元内"
                )
            status, fill_price_cents, level, assumption = _match_order(
                quote, side, order_type, limit_price_cents, limit_up_cents, limit_down_cents
            )

            if status == "rejected":
                reason = assumption
                order = _new_order(
                    acct=acct, symbol=symbol, name=quote.get("name", symbol), side=side,
                    order_type=order_type, quantity=quantity,
                    display_price_cents=limit_price_cents or _to_cents(quote["price"]),
                    limit_price_cents=limit_price_cents, status="rejected", reject_reason=reason,
                    idempotency_key=idempotency_key, request_hash=request_hash,
                    market_data_level=level, simulation_assumption=assumption,
                    trade_reason=trade_reason, invalid_condition=invalid_condition,
                )
                db.add(order)
                db.flush()
                db.commit()
                return _order_payload(order)

            if status == "pending":
                reserve_price = limit_price_cents or _to_cents(quote["price"])
                reserve_amount = reserve_price * quantity
                estimate = calc_fees(side, _money(reserve_amount))
                reserve_cash = reserve_amount + estimate["fee_cents"] if side == "buy" else 0
                if side == "buy" and reserve_cash > available_cash_cents(db, acct.id):
                    reason = (
                        f"可用资金不足：挂单预计占用 {_money(reserve_cash):,.2f} 元，"
                        f"当前可用 {_money(available_cash_cents(db, acct.id)):,.2f} 元"
                    )
                    order = _new_order(
                        acct=acct,
                        symbol=symbol,
                        name=quote.get("name", symbol),
                        side=side,
                        order_type=order_type,
                        quantity=quantity,
                        display_price_cents=reserve_price,
                        limit_price_cents=limit_price_cents,
                        status="rejected",
                        reject_reason=reason,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        market_data_level=level,
                        simulation_assumption=f"{assumption}；下单前资金检查未通过",
                        trade_reason=trade_reason,
                        invalid_condition=invalid_condition,
                    )
                    db.add(order)
                    db.flush()
                    db.commit()
                    return _order_payload(order)
                else:
                    order = _new_order(
                        acct=acct, symbol=symbol, name=quote.get("name", symbol), side=side,
                        order_type=order_type, quantity=quantity, display_price_cents=reserve_price,
                        limit_price_cents=limit_price_cents, status="pending",
                        reserved_cash_cents=reserve_cash, idempotency_key=idempotency_key,
                        request_hash=request_hash, market_data_level=level,
                        simulation_assumption=assumption,
                        trade_reason=trade_reason, invalid_condition=invalid_condition,
                    )
                    db.add(order)
                    db.flush()
                    db.commit()
                    return _order_payload(order)
            else:
                reject_reason = ""

            if fill_price_cents is None:
                raise TradeError("撮合未产生有效成交价")
            amount_cents = fill_price_cents * quantity
            fees = calc_fees(side, _money(amount_cents))
            if side == "buy":
                total_cost = amount_cents + fees["fee_cents"]
                available_cash = available_cash_cents(db, acct.id)
                if total_cost > available_cash:
                    reject_reason = (
                        f"可用资金不足：需要 {_money(total_cost):,.2f} 元，"
                        f"当前可用 {_money(available_cash):,.2f} 元"
                    )
                    status = "rejected"
            if side == "sell" and quantity > (available or 0):
                reject_reason = f"持仓不足：可卖 {available} 股，想卖 {quantity} 股"
                status = "rejected"

            order = _new_order(
                acct=acct, symbol=symbol, name=quote.get("name", symbol), side=side,
                order_type=order_type, quantity=quantity, display_price_cents=fill_price_cents,
                limit_price_cents=limit_price_cents, status="rejected" if reject_reason else "filled",
                reject_reason=reject_reason, fee_cents=fees["fee_cents"], tax_cents=fees["tax_cents"],
                filled_quantity=0 if reject_reason else quantity,
                idempotency_key=idempotency_key, request_hash=request_hash,
                market_data_level=level, simulation_assumption=assumption,
                trade_reason=trade_reason, invalid_condition=invalid_condition,
            )
            db.add(order)
            db.flush()
            if reject_reason:
                db.commit()
                return _order_payload(order)

            if side == "buy":
                db.add(
                    PositionLot(
                        account_id=acct.id,
                        symbol=symbol,
                        name=quote.get("name", symbol),
                        source_order_id=order.id,
                        quantity=quantity,
                        available_quantity=quantity,
                        total_cost_cents=amount_cents + fees["fee_cents"],
                        cost_cents=_unit_cost_from_total(
                            amount_cents + fees["fee_cents"], quantity
                        ),
                        acquired_at=order.created_at or datetime.now(),
                    )
                )
            else:
                _consume_lots(db, acct, symbol, quantity)
            _record_execution(
                db, acct, order, price_cents=fill_price_cents,
                fee_cents=fees["fee_cents"], tax_cents=fees["tax_cents"],
                market_data_level=level, simulation_assumption=assumption,
            )
            _sync_position_projection(db, acct, symbol, quote.get("name", symbol))
            _append_snapshot(db, acct)
            db.commit()
            return _order_payload(order)
        except Exception:
            db.rollback()
            raise


def cancel_order(db: Session, order_id: int, account_id: int | None = None) -> dict:
    with _TRADE_LOCK:
        try:
            _begin_immediate(db)
            acct = get_or_create_account(db, account_id, commit=False)
            order = db.query(Order).filter(Order.id == order_id, Order.account_id == acct.id).first()
            if order is None:
                raise TradeError("订单不存在")
            if order.status not in ("pending", "partial"):
                raise TradeError(f"订单当前状态为 {order.status}，不能撤单")
            order.status = "cancelled"
            order.reject_reason = "用户主动撤单"
            order.reserved_cash_cents = 0
            order.updated_at = datetime.now()
            db.commit()
            return _order_payload(order)
        except Exception:
            db.rollback()
            raise


def expire_pending_orders(
    db: Session, now: datetime | None = None, account_id: int | None = None
) -> list[dict]:
    """将到期挂单转为 expired，并释放其预留资金。"""
    now = now or datetime.now()
    with _TRADE_LOCK:
        try:
            _begin_immediate(db)
            acct = get_or_create_account(db, account_id, commit=False)
            rows = (
                db.query(Order)
                .filter(
                    Order.account_id == acct.id,
                    Order.status.in_(("pending", "partial")),
                    Order.expires_at.isnot(None),
                    Order.expires_at <= now,
                )
                .order_by(Order.id.asc())
                .all()
            )
            for order in rows:
                order.status = "expired"
                order.reject_reason = "挂单超过有效期"
                order.reserved_cash_cents = 0
                order.updated_at = now
            db.commit()
            return [_order_payload(order) for order in rows]
        except Exception:
            db.rollback()
            raise


def rebuild_account_state(db: Session, account_id: int | None = None) -> dict:
    acct = get_or_create_account(db, account_id)
    cash = _cash_balance_cents(db, acct)
    lots = (
        db.query(PositionLot)
        .filter(PositionLot.account_id == acct.id, PositionLot.quantity > 0)
        .order_by(PositionLot.symbol, PositionLot.id)
        .all()
    )
    positions: dict[str, dict] = {}
    for lot in lots:
        row = positions.setdefault(lot.symbol, {"quantity": 0, "total_cost_cents": 0})
        row["quantity"] += int(lot.quantity)
        row["total_cost_cents"] += _lot_total_cost(lot)
    for row in positions.values():
        row["avg_cost_cents"] = _unit_cost_from_total(
            row["total_cost_cents"], row["quantity"]
        )
    return {
        "account_id": acct.id,
        "cash_balance_cents": cash,
        "available_cash_cents": available_cash_cents(db, acct.id),
        "positions": positions,
    }


def _order_payload(order: Order) -> dict:
    return {
        "id": order.id,
        "symbol": order.symbol,
        "name": order.name,
        "side": order.side,
        "order_type": order.order_type,
        "price": order.price,
        "limit_price": _money(order.limit_price_cents) if order.limit_price_cents else None,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity or 0,
        "remaining_quantity": order.remaining_quantity or 0,
        "status": order.status,
        "reject_reason": order.reject_reason,
        "fee": order.fee or 0.0,
        "tax": order.tax or 0.0,
        "market_data_level": order.market_data_level,
        "simulation_assumption": order.simulation_assumption,
        "trade_reason": order.trade_reason,
        "invalid_condition": order.invalid_condition,
        "expires_at": order.expires_at.strftime("%Y-%m-%d %H:%M:%S") if order.expires_at else None,
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else "",
    }


def list_orders(db: Session, limit: int = 100, account_id: int | None = None) -> list[dict]:
    acct = get_or_create_account(db, account_id)
    rows = (
        db.query(Order)
        .filter(Order.account_id == acct.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [_order_payload(row) for row in rows]
