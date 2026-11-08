import unittest,tempfile,json
from pathlib import Path
from core import run
from decimal import Decimal

class Tests(unittest.TestCase):
 def test_empty(self):self.assertEqual(run({})['bars'],0)
 def test_bad(self):
  with self.assertRaises(ValueError):run({'bars':[{'close':-2}]})
 def test_trend(self):
  rows=[{'timestamp':i*900+900,'open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':100} for i in range(80)]
  r=run({'bars':rows});self.assertEqual(r['bars'],80);self.assertEqual(len(r['decisions']),5)
 def test_fees(self):
  from crypto_shadow_lab.accounting import CostModel
  with self.assertRaises(ValueError):CostModel(fee_rate=Decimal('-1'))

if __name__=="__main__":unittest.main()

class LedgerTests(unittest.TestCase):
 def test_empty_ledger(self):self.assertEqual(run({'action':'orders'})['orders'],[])
 def test_external_quote_refused(self):
  with self.assertRaises(ValueError):run({'action':'orders','quotes':{'X':{'price':1}}})
 def test_observe(self):self.assertIsInstance(run({'action':'observe'}),dict)
 def test_cost_allocation(self):
  from stock.services.trading import _allocate_lot_cost
  self.assertEqual(_allocate_lot_cost(100,1,3)+_allocate_lot_cost(67,2,2),100)

class OrderTests(unittest.TestCase):
 def test_fill_idempotency_and_tplusone(self):
  quote={'synthetic':True,'price':10,'prev_close':10,'name':'Synthetic','best_ask':10,'best_bid':10}
  order={'symbol':'SYN','side':'buy','quantity':100,'price':10,'order_type':'limit','idempotency_key':'test-order','trade_reason':'Synthetic unit test','invalid_condition':'Synthetic invalid quote'}
  r=run({'action':'orders','quotes':{'SYN':quote},'orders':[order,order]})
  self.assertEqual(len(r['orders']),1);self.assertEqual(r['orders'][0]['filled_quantity'],100);self.assertEqual(r['cash'],9000)
  from stock.services.trading import TradeError
  rejected=run({'action':'orders','quotes':{'SYN':quote},'orders':[order,{**order,'side':'sell','idempotency_key':'sell-a'}]})
  sell=next(row for row in rejected['orders'] if row['side']=='sell');self.assertEqual(sell['status'],'rejected');self.assertIn('T+1',sell['reject_reason']);self.assertEqual(rejected['cash'],9000)
