"""仅接受调用方给定的合成报价，完全没有行情源和网络请求。"""
QUOTES={}
def normalize_symbol(value):
    value=str(value).strip()
    if not value or len(value)>32:raise ValueError('symbol无效')
    return value
def get_spot_row(symbol):return QUOTES.get(symbol)
def get_spot_batch(symbols):return {symbol:QUOTES.get(symbol) for symbol in symbols}
def limit_pct(symbol,name=''):return 0.1
def quote_is_tradeable(quote):return bool(quote.get('synthetic') and not quote.get('stale'))
