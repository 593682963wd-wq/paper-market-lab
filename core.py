"""作者：Wangyuan；日期：2026-10-29；职责：显式合成K线上的纯模拟策略与精确成本核算。"""
from crypto_shadow_lab.strategies import normalize_market_bars,classify_market,evaluate_strategy,STRATEGIES,ema,rsi,bollinger_bands
from crypto_shadow_lab.accounting import CostModel
from dataclasses import asdict
from decimal import Decimal

def run_strategies(payload):
    if not isinstance(payload,dict):raise TypeError('输入须对象')
    rows=payload.get('bars',[])
    if not isinstance(rows,list):raise TypeError('bars须数组')
    bars=normalize_market_bars(rows)
    if len(bars)!=len(rows):raise ValueError('存在无效/重复/未完成K线')
    state=classify_market(bars);decisions={name:asdict(evaluate_strategy(name,bars,state,None)) for name in STRATEGIES}
    prices=[bar.close for bar in bars]
    return {'bars':len(bars),'market_state':asdict(state),'decisions':decisions,'indicators':{'ema20':ema(prices,20),'rsi14':rsi(prices)},'notice':'模拟研究；无交易、无实时行情，不构成收益预测。'}


def run(payload):
    if isinstance(payload,dict) and payload.get('action')=='observe':
        from market_analytics import build_market_analysis
        return build_market_analysis(payload.get('snapshot',{}))
    if isinstance(payload,dict) and payload.get('action')=='orders':
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from stock.database import Base
        from stock.services import market_data,trading
        from stock.models import Account
        quotes=payload.get('quotes',{})
        if any(not quote.get('synthetic') for quote in quotes.values()):raise ValueError('此演示只接受明确synthetic标记的报价')
        market_data.QUOTES=quotes;engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                account=trading.get_or_create_account(db)
                for order in payload.get('orders',[]):
                    trading.place_order(db,account_id=account.id,**order)
                return {'orders':trading.list_orders(db,account_id=account.id),'cash':account.available_cash,'notice':'全部为内存合成模拟账本，未向任何市场下单。'}
        finally:engine.dispose()
    return run_strategies(payload)
