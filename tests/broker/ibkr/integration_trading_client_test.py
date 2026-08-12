import time
from src.broker.ibkr.trading_client import TradingClient
from src.broker.ibkr.config import PORT


def run_integration_test():
    print("Connecting to IB Gateway...")
    client = TradingClient()

    try:
        # Use a unique client ID for integration tests
        client.connect(host='127.0.0.1', port=PORT, client_id=99)
        print("Connected successfully!")

        # 1. Test Account Summary Ingestion
        print("\nFetching Account Summary...")
        summary = client.get_account_summary()
        print(f"Account Summary Received: {summary}")

        # 2. Test Limit Order Placement (Safely away from the market)
        print("\nPlacing Test Limit Order for AAPL...")
        # Placing a buy order way below market price to ensure it doesn't fill
        trade = client.place_limit_order(symbol='AAPL', action='BUY', quantity=1, limit_price=10.00)

        print("Limit order sent! Waiting briefly for status update...")
        time.sleep(2)

        print(f"Order Status: {trade.orderStatus.status}")
        print("\nIntegration test logic completed successfully!")

    except Exception as e:
        print(f"\n[ERROR] Integration Test Failed: {e}")

    finally:
        # This block ALWAYS runs, guaranteeing the socket is closed
        print("\nDisconnecting from IB Gateway...")
        try:
            client.disconnect()
            print("Disconnected safely. Client ID 99 released.")
        except Exception as e:
            print(f"Failed to disconnect cleanly: {e}")


if __name__ == "__main__":
    run_integration_test()