import os
import sys
import time
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# Load environment variables
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RPC_URL = os.getenv("POLYGON_RPC")

if not PRIVATE_KEY:
    print("Error: PRIVATE_KEY not found in .env file.")
    sys.exit(1)

def main():
    print("--- Polymarket Execution Layer: Smoke Test ---")
    
    # 1. Initialize Client
    print("\n1. Initializing ClobClient...")
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=0, # 0 for EOA (Phantom/Metamask), 1 for Agent, 2 for Gnosis Safe
        )
        print("   Success! Client initialized.")
        print(f"   Address: {client.get_address()}")
        
        # Create/Derive API Credentials (L2)
        print("   Deriving API Credentials...")
        client.set_api_creds(client.create_or_derive_api_creds())
        print("   Credentials set.")

    except Exception as e:
        print(f"   Failed to initialize client: {e}")
        return

    # 2. Check Allowances (Approvals)
    print("\n2. Checking Token Approvals...")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    
    try:
        # Check USDC allowance (Collateral)
        print("   Checking/Approving USDC (Collateral)...")
        tx_hash = client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if tx_hash:
             print(f"   Approved USDC! Tx Hash: {tx_hash}")
        else:
             print("   USDC already approved.")

        # Check Conditional Tokens allowance
        print("   Checking/Approving Conditional Tokens...")
        try:
            tx_hash_ct = client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
            )
            if tx_hash_ct:
                print(f"   Approved Conditional Tokens! Tx Hash: {tx_hash_ct}")
            else:
                print("   Conditional Tokens already approved.")
        except Exception as e:
            print(f"   [Warning] CTF Approval skipped/failed: {e}")
            print("   You may need to 'Enable Trading' on polymarket.com for Selling later.")
            
    except Exception as e:
        print(f"   Critical Approval Error: {e}")
        return

    # 3. Smoke Test Order (Optional)
    # We will try to place a LIMIT order far away from the market price to avoid execution
    # OR a tiny market order if you are ready.
    # For safety, let's just fetch markets first to prove API read access.
    
    print("\n3. Verifying Market Access (ReadOnly)...")
    try:
        # Fetch a popular market to check connectivity
        # Example: "Will Bitcoin hit $100k in 2024?" (or any active market)
        # We'll just list some markets to be safe.
        resp = client.get_markets(next_cursor="")
        if resp and len(resp.get('data', [])) > 0:
            print(f"   Success! Fetched {len(resp['data'])} markets.")
            example_market = resp['data'][0]
            print(f"   Example Market: {example_market.get('question', 'Unknown')}")
            print(f"   Condition ID: {example_market.get('condition_id')}")
        else:
            print("   Fetched markets but list was empty.")
    except Exception as e:
        print(f"   Failed to fetch markets: {e}")
        return

    print("\n--- READY FOR LIVE TRADING ---")
    print("Next steps:")
    print("1. Ensure you have USDC (Polygon) for trading.")
    print("2. Ensure you have a small amount of MATIC (Polygon) for gas (executions are gasless, but approvals need gas).")
    print("3. Wait for the dashboard code!")

if __name__ == "__main__":
    main()
