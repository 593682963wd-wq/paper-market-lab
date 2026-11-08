import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from core import run
from crypto_shadow_lab.engine import ShadowEngine,ShadowEngineSettings
from crypto_shadow_lab.strategies import MarketBar,MarketState
from market_analytics import _rsi

class ApiRegressions(unittest.TestCase):
    def make_engine(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return ShadowEngine(ShadowEngineSettings(state_path=Path(temp.name)/'shadow.json'))
    def bar(self,ts,price=100):return MarketBar(ts,price,price,price,price,1)
    def test_flat_rsi_is_neutral(self):
        self.assertEqual(_rsi([100.0]*20,14),50)
    def test_old_batches_do_not_revalue_latest_state(self):
        engine=self.make_engine();market=MarketState('no_trade','synthetic')
        engine.process([self.bar(900)],market)
        current=engine.process([self.bar(1800,110)],market)
        before=engine.settings.state_path.read_bytes()
        old=engine.process([self.bar(900,10)],MarketState('trend_down','old'))
        self.assertEqual(old,current)
        self.assertEqual(engine.settings.state_path.read_bytes(),before)
        revised=engine.process([self.bar(1800,120)],market)
        self.assertEqual(revised['market']['last_price'],120)
    def test_single_new_bar_gap_pauses_strategy(self):
        calls=[]
        def provider(*args):calls.append(args);raise AssertionError('must not trade on missing bars')
        engine=self.make_engine();engine.decision_provider=provider
        market=MarketState('trend_up','synthetic')
        engine.process([self.bar(900)],market)
        result=engine.process([self.bar(3600)],market)
        self.assertEqual(result['market']['missed_bars_total'],2)
        self.assertEqual(result['data_status'],'degraded')
        self.assertEqual(calls,[])
    def test_cli_trade_rejection_is_controlled(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'orders.json'
            path.write_text(json.dumps({'action':'orders','quotes':{'SYN':{'synthetic':True,'stale':True,'price':10,'prev_close':10}},'orders':[{'symbol':'SYN','side':'buy','quantity':100,'price':10,'order_type':'limit','trade_reason':'synthetic','invalid_condition':'synthetic'}]}))
            result=subprocess.run([sys.executable,'-B','cli.py',str(path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('Traceback',result.stderr)
