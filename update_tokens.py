#!/usr/bin/env python3
"""
Helper script to update myq_tokens.json from captured HTTP requests.

Usage:
    python update_tokens.py --access-token "..." --refresh-token "..." [--cookie "..."]
    
Or interactively:
    python update_tokens.py
"""

import json
import time
import argparse
import sys
from pathlib import Path

TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"


def decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification."""
    import base64
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        
        # Add padding if needed
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding
        
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def update_tokens(access_token: str, refresh_token: str, cf_cookie: str = ""):
    """Update the tokens file with new values."""
    
    # Load existing tokens to preserve account_id and device_serial
    existing = {}
    if TOKENS_FILE.exists():
        try:
            existing = json.loads(TOKENS_FILE.read_text())
        except Exception:
            pass
    
    # Try to get expiry from JWT
    jwt_payload = decode_jwt_payload(access_token)
    expires_at = jwt_payload.get('exp', time.time() + 1800)
    
    # Get account ID from JWT if available
    account_id = existing.get('account_id', '')
    if jwt_payload.get('sub'):
        # The 'sub' claim might be the user ID, not account ID
        # Keep existing account_id if we have it
        pass
    
    data = {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_at': expires_at,
        'account_id': account_id or existing.get('account_id', ''),
        'device_serial': existing.get('device_serial', ''),
        'expires_in': 1800,
        'token_type': 'Bearer',
        'scope': 'MyQ_Residential offline_access',
        'cf_cookie': cf_cookie or existing.get('cf_cookie', ''),
    }
    
    TOKENS_FILE.write_text(json.dumps(data, indent=2))
    print(f"✅ Tokens updated in {TOKENS_FILE}")
    print(f"   Access token expires at: {time.ctime(expires_at)}")
    print(f"   Account ID: {data['account_id'] or '(will be fetched on first API call)'}")
    print(f"   Device serial: {data['device_serial'] or '(will be fetched on first API call)'}")
    print(f"   CF Cookie: {'Set' if data['cf_cookie'] else 'Not set'}")


def main():
    parser = argparse.ArgumentParser(
        description="Update myQ tokens from captured HTTP requests"
    )
    parser.add_argument('--access-token', '-a', help='JWT access token (Bearer token)')
    parser.add_argument('--refresh-token', '-r', help='Refresh token')
    parser.add_argument('--cookie', '-c', help='Cloudflare __cf_bm cookie value')
    
    args = parser.parse_args()
    
    if args.access_token and args.refresh_token:
        update_tokens(args.access_token, args.refresh_token, args.cookie or '')
    else:
        # Interactive mode
        print("=" * 60)
        print("MyQ Token Updater")
        print("=" * 60)
        print("\nCapture these from the myQ app using a proxy (Proxyman/Charles):")
        print("1. Look for requests to partner-identity.myq-cloud.com")
        print("2. Find the Authorization header (Bearer token)")
        print("3. Find the refresh_token in the response body")
        print("4. Optionally find the __cf_bm cookie")
        print()
        
        access_token = input("Paste Access Token (without 'Bearer '): ").strip()
        if access_token.startswith('Bearer '):
            access_token = access_token[7:]
        
        if not access_token:
            print("❌ Access token required")
            sys.exit(1)
        
        refresh_token = input("Paste Refresh Token: ").strip()
        if not refresh_token:
            print("❌ Refresh token required")
            sys.exit(1)
        
        cf_cookie = input("Paste __cf_bm cookie (optional, press Enter to skip): ").strip()
        
        update_tokens(access_token, refresh_token, cf_cookie)


if __name__ == "__main__":
    main()

