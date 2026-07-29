import unittest
import sys
import os

# Append project path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion import blockscout_client as bs

class FIFOQueue:
    """A helper implementation of a FIFO queue for cost-basis accounting testing."""
    def __init__(self):
        self.lots = [] # list of dicts: {"amount": float, "price": float}

    def buy(self, amount: float, price: float):
        self.lots.append({"amount": amount, "price": price})

    def sell(self, amount: float, revenue_usd: float) -> float:
        """Exhaust lots and return realized profit/loss."""
        remaining_to_sell = amount
        total_cost = 0.0
        
        while remaining_to_sell > 0 and self.lots:
            oldest_lot = self.lots[0]
            if oldest_lot["amount"] <= remaining_to_sell:
                # Exhaust this lot
                total_cost += oldest_lot["amount"] * oldest_lot["price"]
                remaining_to_sell -= oldest_lot["amount"]
                self.lots.pop(0)
            else:
                # Partially exhaust this lot
                total_cost += remaining_to_sell * oldest_lot["price"]
                oldest_lot["amount"] -= remaining_to_sell
                remaining_to_sell = 0.0
                
        return revenue_usd - total_cost

class TestFIFOAccounting(unittest.TestCase):
    def test_basic_fifo(self):
        fifo = FIFOQueue()
        # Buy 10 tokens at $2/token (total cost $20)
        fifo.buy(10.0, 2.0)
        # Buy 5 tokens at $3/token (total cost $15)
        fifo.buy(5.0, 3.0)
        
        # Sell 12 tokens for $40 total revenue
        # FIFO matches:
        # - 10 tokens from lot 1 ($2 each = $20 cost)
        # - 2 tokens from lot 2 ($3 each = $6 cost)
        # Total cost = $26. Profit = $40 - $26 = $14
        profit = fifo.sell(12.0, 40.0)
        self.assertAlmostEqual(profit, 14.0)
        self.assertEqual(len(fifo.lots), 1)
        self.assertAlmostEqual(fifo.lots[0]["amount"], 3.0)
        self.assertAlmostEqual(fifo.lots[0]["price"], 3.0)

class TestIngestionEnhancements(unittest.TestCase):
    def test_rpc_block_search(self):
        # Fetch timestamp of latest block
        latest = bs.get_latest_block_number()
        latest_ts = bs.get_block_timestamp(latest)
        self.assertGreater(latest_ts, 0)
        
        # Binary search for block by its own timestamp
        found_block = bs.get_block_by_timestamp(latest_ts, latest_block=latest)
        self.assertEqual(bs.get_block_timestamp(found_block), latest_ts)

    def test_json_rpc_batching(self):
        # Fetch some recent transactions of a known wallet
        tx_list = bs._get("/addresses/0x02ed7d45040fc9b930040e30a2cdbc796792035b/transactions", params={})
        items = tx_list.get("items", [])
        if not items:
            self.skipTest("No transactions found to test batching")
            
        hashes = [tx.get("hash") for tx in items[:5] if tx.get("hash")]
        if not hashes:
            self.skipTest("No transaction hashes found")
            
        # Run batching call
        batch_results = bs.get_transaction_token_transfers_batch(hashes)
        self.assertEqual(len(batch_results), len(hashes))
        for h in hashes:
            self.assertIn(h, batch_results)

if __name__ == "__main__":
    unittest.main()
