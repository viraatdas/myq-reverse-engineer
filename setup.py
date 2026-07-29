#!/usr/bin/env python3
"""
MyQ Garage Door Controller - Setup & Launch

Usage:
    python setup.py              # Login with Playwright + start server
    python setup.py --login      # Just login (get tokens)
    python setup.py --server     # Just start the server (tokens must exist)
    python setup.py --proxy      # Start mitmproxy token capture
    python setup.py --status     # Check current token status
    python setup.py --test       # Test the API (door status)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"


def check_tokens():
    """Check token status"""
    if not TOKENS_FILE.exists():
        print("No tokens found.")
        return None

    data = json.loads(TOKENS_FILE.read_text())
    has_access = bool(data.get('access_token'))
    has_refresh = bool(data.get('refresh_token'))
    expires_at = data.get('expires_at', 0)
    is_expired = time.time() > expires_at
    account_id = data.get('account_id', '')
    device_serial = data.get('device_serial', '')

    print(f"Access Token:  {'Yes' if has_access else 'MISSING'}")
    print(f"Refresh Token: {'Yes' if has_refresh else 'MISSING'}")
    print(f"Expired:       {'YES - needs refresh' if is_expired else 'No (valid)'}")
    print(f"Account ID:    {account_id or 'Not set'}")
    print(f"Device Serial: {device_serial or 'Not set'}")

    if has_access and has_refresh:
        if is_expired:
            print("\nTokens exist but are expired. The server will auto-refresh on startup.")
        else:
            remaining = int(expires_at - time.time())
            print(f"\nTokens valid for {remaining // 60} minutes.")
        return data

    print("\nTokens incomplete. Run: python setup.py --login")
    return None


async def do_login():
    """Perform login via Playwright"""
    email = os.getenv("MYQ_EMAIL")
    password = os.getenv("MYQ_PASSWORD")

    if not email or not password:
        print("Set MYQ_EMAIL and MYQ_PASSWORD in .env file")
        sys.exit(1)

    print(f"Logging in as {email}...")

    from myq_api import MyQAPI
    api = MyQAPI()
    try:
        await api.login(email, password)
        print("\nLogin successful! Token status:")
        check_tokens()
    except Exception as e:
        print(f"\nLogin failed: {e}")
        print("\nAlternative: capture tokens from your phone")
        print("  python setup.py --proxy")
        sys.exit(1)
    finally:
        await api.close()


async def do_test():
    """Test the API by getting door status"""
    if not TOKENS_FILE.exists():
        print("No tokens. Run: python setup.py --login")
        sys.exit(1)

    from myq_api import MyQAPI
    api = MyQAPI()
    try:
        state = await api.get_door_state()
        print(f"Door: {state.name}")
        print(f"State: {state.state}")
        print(f"Online: {state.online}")
        print(f"Serial: {state.serial_number}")
        print(f"\nAPI is working!")
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
    finally:
        await api.close()


def start_server():
    """Start the FastAPI server"""
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"\nStarting MyQ Garage Door API on {host}:{port}")
    print(f"  GET  /status  - Door status")
    print(f"  POST /open    - Open door")
    print(f"  POST /close   - Close door")
    print(f"  POST /toggle  - Toggle door")
    print(f"  GET  /devices - List devices")
    print(f"  GET  /health  - Health check")
    print()

    uvicorn.run("server:app", host=host, port=port, reload=False)


def start_proxy():
    """Start the mitmproxy token capture"""
    import subprocess
    subprocess.run([sys.executable, "auto_capture_proxy.py"])


def main():
    parser = argparse.ArgumentParser(description="MyQ Garage Door Controller Setup")
    parser.add_argument("--login", action="store_true", help="Login and get tokens")
    parser.add_argument("--server", action="store_true", help="Start API server only")
    parser.add_argument("--proxy", action="store_true", help="Start mitmproxy token capture")
    parser.add_argument("--status", action="store_true", help="Check token status")
    parser.add_argument("--test", action="store_true", help="Test API (get door status)")
    args = parser.parse_args()

    if args.status:
        check_tokens()
    elif args.login:
        asyncio.run(do_login())
    elif args.test:
        asyncio.run(do_test())
    elif args.proxy:
        start_proxy()
    elif args.server:
        check_tokens()
        start_server()
    else:
        # Default: login if no tokens, then start server
        tokens = check_tokens()
        if not tokens:
            print("\nNo tokens found. Attempting login...")
            asyncio.run(do_login())
        start_server()


if __name__ == "__main__":
    main()
