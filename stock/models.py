"""ORM 模型：旧展示投影 + 可重建的订单、成交、资金和持仓 lot 事件。"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from .database import Base


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="模拟账户")
    initial_cash = Column(Float, default=0)
    available_cash = Column(Float, default=0)
    initial_cash_cents = Column(Integer, default=0, nullable=True)
    available_cash_cents = Column(Integer, default=0, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_account_symbol"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True)
    symbol = Column(String(16), index=True)
    name = Column(String(64))
    quantity = Column(Integer, default=0)
    available_quantity = Column(Integer, default=0)  # 展示用，实时以 T+1 计算为准
    avg_cost = Column(Float, default=0)
    avg_cost_cents = Column(Integer, default=0, nullable=True)
    total_cost_cents = Column(Integer, default=0, nullable=True)
    reserved_quantity = Column(Integer, default=0, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True)
    symbol = Column(String(16), index=True)
    name = Column(String(64))
    side = Column(String(8))            # buy / sell
    order_type = Column(String(8), default="limit")  # limit / market
    price = Column(Float)
    quantity = Column(Integer)
    status = Column(String(16), default="filled")    # filled / rejected
    reject_reason = Column(String(128), default="")
    fee = Column(Float, default=0)      # 佣金+过户费
    tax = Column(Float, default=0)      # 印花税（仅卖出）
    created_at = Column(DateTime, default=datetime.now, index=True)
    idempotency_key = Column(String(128), index=True, nullable=True)
    request_hash = Column(String(64), nullable=True)
    limit_price_cents = Column(Integer, nullable=True)
    filled_quantity = Column(Integer, default=0, nullable=True)
    remaining_quantity = Column(Integer, default=0, nullable=True)
    reserved_cash_cents = Column(Integer, default=0, nullable=True)
    market_data_level = Column(String(32), nullable=True)
    simulation_assumption = Column(String(256), nullable=True)
    trade_reason = Column(Text, nullable=True)
    invalid_condition = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=True)


class AssetSnapshot(Base):
    """资产快照；估值来源随数值一同持久化，避免历史曲线伪装成实时行情。"""

    __tablename__ = "asset_snapshots"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True)
    cash = Column(Float)
    market_value = Column(Float)
    total_assets = Column(Float)
    valuation_complete = Column(Boolean, default=False, nullable=False)
    valuation_note = Column(Text, default="历史快照估值来源未知，不能视为实时估值", nullable=False)
    source_summary = Column(Text, default="{}", nullable=False)
    captured_at = Column(DateTime, default=datetime.now)


class Watchlist(Base):
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), unique=True, index=True)
    name = Column(String(64))
    created_at = Column(DateTime, default=datetime.now)


class AIReport(Base):
    __tablename__ = "ai_reports"
    id = Column(Integer, primary_key=True)
    kind = Column(String(16), index=True)   # daily / stock / holdings
    title = Column(String(128))
    content = Column(Text)
    source = Column(String(16), default="deepseek")  # deepseek / local
    params = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.now, index=True)


class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"
    id = Column(Integer, primary_key=True)
    category = Column(String(32), index=True)
    title = Column(String(128))
    content = Column(Text)
    sort_order = Column(Integer, default=0)


class StockCache(Base):
    __tablename__ = "stock_cache"
    symbol = Column(String(16), primary_key=True)
    name = Column(String(64))
    market = Column(String(16))
    kline_json = Column(Text)
    updated_at = Column(DateTime)


class Execution(Base):
    """成交事件：金额以整数分保存，订单只是命令，成交才改变账本。"""

    __tablename__ = "executions"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, index=True, nullable=False)
    account_id = Column(Integer, index=True, nullable=False)
    symbol = Column(String(16), index=True, nullable=False)
    side = Column(String(8), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False)
    gross_cents = Column(Integer, nullable=False)
    fee_cents = Column(Integer, default=0, nullable=False)
    tax_cents = Column(Integer, default=0, nullable=False)
    market_data_level = Column(String(32), nullable=True)
    simulation_assumption = Column(String(256), nullable=True)
    executed_at = Column(DateTime, default=datetime.now, index=True)


class CashLedgerEntry(Base):
    """现金事件：amount_cents 为有符号变化，余额可由 opening + entries 重建。"""

    __tablename__ = "cash_ledger_entries"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True, nullable=False)
    order_id = Column(Integer, index=True, nullable=True)
    entry_type = Column(String(32), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    balance_after_cents = Column(Integer, nullable=False)
    idempotency_key = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


class PositionLot(Base):
    """持仓 lot：总成本按分精确保存，单位成本仅保留兼容展示。"""

    __tablename__ = "position_lots"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, index=True, nullable=False)
    symbol = Column(String(16), index=True, nullable=False)
    name = Column(String(64), nullable=True)
    source_order_id = Column(Integer, index=True, nullable=True)
    quantity = Column(Integer, nullable=False)
    available_quantity = Column(Integer, nullable=False)
    cost_cents = Column(Integer, nullable=False)
    total_cost_cents = Column(Integer, default=0, nullable=False)
    acquired_at = Column(DateTime, default=datetime.now, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
