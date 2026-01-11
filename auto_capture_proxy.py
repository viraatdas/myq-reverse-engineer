#!/usr/bin/env python3
"""
MyQ Auto-Capture Proxy

Automatically captures MyQ authentication tokens when you use the myQ app.
Works with both iOS and Android devices.

Usage:
    python auto_capture_proxy.py

Then configure your phone's WiFi proxy settings to point to this computer.
"""

import json
import time
import re
import gzip
import socket
import sys
import threading
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# Only import mitmproxy components when running as addon
try:
    from mitmproxy import http, ctx
    MITMPROXY_AVAILABLE = True
except ImportError:
    MITMPROXY_AVAILABLE = False


TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"
PROXY_PORT = 8888
STATUS_PORT = 8889


def get_local_ip():
    """Get the local IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "YOUR_COMPUTER_IP"


def decompress_body(body: bytes, encoding: str) -> bytes:
    """Decompress response body if needed"""
    try:
        if encoding == 'gzip':
            return gzip.decompress(body)
        elif encoding == 'br':
            import brotli
            return brotli.decompress(body)
        elif encoding == 'deflate':
            import zlib
            return zlib.decompress(body)
    except Exception:
        pass
    return body


class MyQTokenCapture:
    """Mitmproxy addon that captures MyQ tokens from traffic"""
    
    def __init__(self):
        self.tokens = self._load_existing_tokens()
        self.last_capture = None
        self.capture_count = 0
    
    def _load_existing_tokens(self) -> dict:
        """Load existing tokens to preserve account_id, etc."""
        if TOKENS_FILE.exists():
            try:
                return json.loads(TOKENS_FILE.read_text())
            except Exception:
                pass
        return {}
    
    def _save_tokens(self, source: str = ""):
        """Save tokens to file"""
        self.tokens['_last_updated'] = datetime.now().isoformat()
        self.tokens['_capture_source'] = source
        TOKENS_FILE.write_text(json.dumps(self.tokens, indent=2))
        self.last_capture = datetime.now()
        self.capture_count += 1
        ctx.log.info(f"✅ Tokens saved! (capture #{self.capture_count})")
    
    def _extract_jwt_expiry(self, token: str) -> int:
        """Extract expiry from JWT token"""
        import base64
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return int(time.time()) + 1800
            
            payload = parts[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            return decoded.get('exp', int(time.time()) + 1800)
        except Exception:
            return int(time.time()) + 1800
    
    def request(self, flow: http.HTTPFlow):
        """Intercept requests to capture Authorization header and cookies"""
        host = flow.request.host
        
        # Only process myQ traffic
        if "myq-cloud.com" not in host and "myq.com" not in host:
            return
        
        # Capture Bearer token from Authorization header
        auth_header = flow.request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token != self.tokens.get('access_token'):
                self.tokens['access_token'] = token
                self.tokens['expires_at'] = self._extract_jwt_expiry(token)
                self.tokens['expires_in'] = 1800
                self.tokens['token_type'] = 'Bearer'
                self.tokens['scope'] = 'MyQ_Residential offline_access'
                ctx.log.info(f"🔑 Captured access token from request to {host}")
                self._save_tokens("request_auth_header")
        
        # Capture Cloudflare cookie
        cookie_header = flow.request.headers.get("Cookie", "")
        if "__cf_bm=" in cookie_header:
            match = re.search(r'__cf_bm=[^;]+', cookie_header)
            if match:
                cf_cookie = match.group(0)
                if cf_cookie != self.tokens.get('cf_cookie'):
                    self.tokens['cf_cookie'] = cf_cookie
                    ctx.log.info(f"🍪 Captured Cloudflare cookie")
                    self._save_tokens("request_cf_cookie")
    
    def response(self, flow: http.HTTPFlow):
        """Intercept responses to capture tokens and account info"""
        host = flow.request.host
        path = flow.request.path
        
        # Only process myQ traffic
        if "myq-cloud.com" not in host and "myq.com" not in host:
            return
        
        # Decompress response if needed
        encoding = flow.response.headers.get("Content-Encoding", "")
        body = decompress_body(flow.response.content, encoding)
        
        # Capture token response from OAuth endpoint
        if "partner-identity.myq-cloud.com" in host and "/connect/token" in path:
            try:
                data = json.loads(body)
                if 'access_token' in data:
                    self.tokens['access_token'] = data['access_token']
                    self.tokens['refresh_token'] = data.get('refresh_token', self.tokens.get('refresh_token', ''))
                    self.tokens['expires_at'] = int(time.time()) + data.get('expires_in', 1800)
                    self.tokens['expires_in'] = data.get('expires_in', 1800)
                    self.tokens['token_type'] = data.get('token_type', 'Bearer')
                    self.tokens['scope'] = data.get('scope', 'MyQ_Residential offline_access')
                    self.tokens['token_scope'] = data.get('scope', 'MyQ_Residential offline_access')
                    ctx.log.info(f"🎉 Captured fresh tokens from OAuth!")
                    self._save_tokens("oauth_token_response")
            except Exception as e:
                ctx.log.warn(f"Failed to parse token response: {e}")
        
        # Capture account info
        if "/Accounts" in path and flow.request.method == "GET" and not "/Devices" in path:
            try:
                data = json.loads(body)
                accounts = data.get('items', [])
                if accounts and 'id' in accounts[0]:
                    account_id = accounts[0]['id']
                    if account_id != self.tokens.get('account_id'):
                        self.tokens['account_id'] = account_id
                        ctx.log.info(f"📋 Captured account ID: {account_id}")
                        self._save_tokens("accounts_response")
            except Exception:
                pass
        
        # Capture device info
        if "/Devices" in path and flow.request.method == "GET":
            try:
                data = json.loads(body)
                for device in data.get('items', []):
                    if device.get('device_family') == 'garagedoor':
                        serial = device.get('serial_number')
                        name = device.get('name', 'Unknown')
                        if serial and serial != self.tokens.get('device_serial'):
                            self.tokens['device_serial'] = serial
                            self.tokens['device_name'] = name
                            ctx.log.info(f"🚗 Captured garage door: {name} ({serial})")
                            self._save_tokens("devices_response")
                        break
            except Exception:
                pass
        
        # Capture Cloudflare cookies from response
        for header_name in ['Set-Cookie', 'set-cookie']:
            set_cookie = flow.response.headers.get(header_name, "")
            if "__cf_bm=" in set_cookie:
                match = re.search(r'__cf_bm=[^;]+', set_cookie)
                if match:
                    cf_cookie = match.group(0)
                    if cf_cookie != self.tokens.get('cf_cookie'):
                        self.tokens['cf_cookie'] = cf_cookie
                        ctx.log.info(f"🍪 Captured Cloudflare cookie from response")
                        self._save_tokens("response_cf_cookie")


# Status page handler
class StatusHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for status page"""
    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def do_GET(self):
        local_ip = get_local_ip()
        
        # Load current tokens
        tokens = {}
        if TOKENS_FILE.exists():
            try:
                tokens = json.loads(TOKENS_FILE.read_text())
            except Exception:
                pass
        
        # Check token status
        has_access = bool(tokens.get('access_token'))
        has_refresh = bool(tokens.get('refresh_token'))
        has_account = bool(tokens.get('account_id'))
        has_device = bool(tokens.get('device_serial'))
        expires_at = tokens.get('expires_at', 0)
        is_expired = time.time() > expires_at if expires_at else True
        last_updated = tokens.get('_last_updated', 'Never')
        
        # Format last updated time nicely
        last_updated_display = '—'
        if last_updated and last_updated != 'Never':
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                last_updated_display = dt.strftime('%b %d, %Y at %I:%M %p')
            except:
                last_updated_display = last_updated
        
        # Status banner
        if has_access and has_refresh and not is_expired:
            status_banner = '<div class="banner ok-banner">Tokens Active - All Systems Go</div>'
        elif has_access and has_refresh and is_expired:
            status_banner = '<div class="banner warn-banner">Tokens Expiring Soon - Open myQ App to Refresh</div>'
        else:
            status_banner = '<div class="banner error-banner">Tokens Needed - Follow Setup Below</div>'
        
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>MyQ Garage Controller</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="5">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ 
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            font-weight: 300;
            background: #fff;
            color: #1a1a1a;
            min-height: 100vh;
            padding: 40px 20px;
            line-height: 1.7;
        }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        h1 {{ 
            font-weight: 600;
            font-size: 28px;
            letter-spacing: -0.5px;
            margin-bottom: 12px;
            text-align: center;
        }}
        .subtitle {{
            text-align: center;
            color: #666;
            margin-bottom: 32px;
            font-size: 15px;
        }}
        .banner {{
            padding: 16px 20px;
            border-radius: 8px;
            text-align: center;
            font-weight: 500;
            margin-bottom: 32px;
            font-size: 14px;
        }}
        .ok-banner {{ background: #dcfce7; color: #166534; }}
        .warn-banner {{ background: #fef3c7; color: #92400e; }}
        .error-banner {{ background: #fee2e2; color: #991b1b; }}
        .card {{
            background: #fff;
            border: 1px solid #e5e5e5;
            border-radius: 12px;
            padding: 28px;
            margin-bottom: 24px;
        }}
        .card h2 {{
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #999;
            margin-bottom: 20px;
        }}
        .proxy-box {{
            background: #f8f8f8;
            border: 2px dashed #ddd;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 28px;
        }}
        .proxy-label {{
            font-size: 12px;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }}
        .proxy-value {{
            font-size: 15px;
            font-family: 'SF Mono', Monaco, monospace;
            font-weight: 500;
        }}
        .proxy-row {{
            display: flex;
            gap: 24px;
            margin-top: 12px;
        }}
        .proxy-item {{ flex: 1; }}
        .section-title {{
            font-size: 16px;
            font-weight: 600;
            margin: 28px 0 16px 0;
            padding-top: 20px;
            border-top: 1px solid #eee;
        }}
        .section-title:first-of-type {{
            margin-top: 0;
            padding-top: 0;
            border-top: none;
        }}
        .step {{
            display: flex;
            gap: 14px;
            padding: 14px 0;
            border-bottom: 1px solid #f5f5f5;
        }}
        .step:last-child {{ border-bottom: none; }}
        .step-num {{
            width: 22px;
            height: 22px;
            background: #1a1a1a;
            color: #fff;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 600;
            flex-shrink: 0;
            margin-top: 2px;
        }}
        .step-content {{ flex: 1; }}
        .step-title {{ font-weight: 500; color: #1a1a1a; margin-bottom: 4px; }}
        .step-detail {{ font-size: 14px; color: #666; }}
        .step-detail code {{
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 12px;
            color: #1a1a1a;
        }}
        .status-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }}
        .status-item {{
            padding: 14px;
            background: #fafafa;
            border-radius: 8px;
        }}
        .status-label {{ 
            font-size: 11px; 
            color: #888; 
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }}
        .status-value {{ font-weight: 500; font-size: 14px; }}
        .ok {{ color: #16a34a; }}
        .warn {{ color: #d97706; }}
        .error {{ color: #dc2626; }}
        .footer {{
            text-align: center;
            color: #bbb;
            font-size: 11px;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #f0f0f0;
        }}
        .done-note {{
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            border-radius: 8px;
            padding: 16px;
            margin-top: 20px;
            font-size: 14px;
            color: #166534;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>MyQ Garage Controller</h1>
        <p class="subtitle">Token Capture &amp; Status</p>
        
        {status_banner}
        
        <div class="card">
            <h2>Token Status</h2>
            <div class="status-grid">
                <div class="status-item">
                    <div class="status-label">Access Token</div>
                    <div class="status-value {'ok' if has_access else 'error'}">{'Valid' if has_access else 'Missing'}</div>
                </div>
                <div class="status-item">
                    <div class="status-label">Refresh Token</div>
                    <div class="status-value {'ok' if has_refresh else 'error'}">{'Valid' if has_refresh else 'Missing'}</div>
                </div>
                <div class="status-item">
                    <div class="status-label">Token Status</div>
                    <div class="status-value {'ok' if not is_expired else 'error'}">{'Active' if not is_expired else 'Expired'}</div>
                </div>
                <div class="status-item">
                    <div class="status-label">Last Updated</div>
                    <div class="status-value" style="color: #666; font-size: 12px;">{last_updated_display}</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h2>iPhone Setup Instructions</h2>
            
            <div class="proxy-box">
                <div class="proxy-row">
                    <div class="proxy-item">
                        <div class="proxy-label">Server</div>
                        <div class="proxy-value">{local_ip}</div>
                    </div>
                    <div class="proxy-item">
                        <div class="proxy-label">Port</div>
                        <div class="proxy-value">{PROXY_PORT}</div>
                    </div>
                </div>
            </div>
            
            <div class="section-title">Part 1: Configure Proxy</div>
            
            <div class="step">
                <span class="step-num">1</span>
                <div class="step-content">
                    <div class="step-title">Open Settings</div>
                    <div class="step-detail">Tap the Settings app on your iPhone</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">2</span>
                <div class="step-content">
                    <div class="step-title">Go to Wi-Fi</div>
                    <div class="step-detail">Tap <strong>Wi-Fi</strong> in the settings menu</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">3</span>
                <div class="step-content">
                    <div class="step-title">Tap the info button</div>
                    <div class="step-detail">Tap the blue <strong>(i)</strong> icon next to your connected network</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">4</span>
                <div class="step-content">
                    <div class="step-title">Configure Proxy</div>
                    <div class="step-detail">Scroll down and tap <strong>Configure Proxy</strong>, then select <strong>Manual</strong></div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">5</span>
                <div class="step-content">
                    <div class="step-title">Enter proxy details</div>
                    <div class="step-detail">Server: <code>{local_ip}</code><br>Port: <code>{PROXY_PORT}</code><br>Leave Authentication OFF</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">6</span>
                <div class="step-content">
                    <div class="step-title">Save</div>
                    <div class="step-detail">Tap <strong>Save</strong> in the top right corner</div>
                </div>
            </div>
            
            <div class="section-title">Part 2: Install Certificate</div>
            
            <div class="step">
                <span class="step-num">7</span>
                <div class="step-content">
                    <div class="step-title">Open Safari</div>
                    <div class="step-detail">Open the Safari browser (not Chrome)</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">8</span>
                <div class="step-content">
                    <div class="step-title">Visit mitm.it</div>
                    <div class="step-detail">Type <code>http://mitm.it</code> in the address bar and go</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">9</span>
                <div class="step-content">
                    <div class="step-title">Download certificate</div>
                    <div class="step-detail">Tap the <strong>Apple</strong> button to download the iOS certificate, then tap <strong>Allow</strong></div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">10</span>
                <div class="step-content">
                    <div class="step-title">Install profile</div>
                    <div class="step-detail">Go to <strong>Settings</strong> &rarr; <strong>General</strong> &rarr; <strong>VPN &amp; Device Management</strong> &rarr; tap the mitmproxy profile &rarr; <strong>Install</strong></div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">11</span>
                <div class="step-content">
                    <div class="step-title">Trust certificate</div>
                    <div class="step-detail">Go to <strong>Settings</strong> &rarr; <strong>General</strong> &rarr; <strong>About</strong> &rarr; <strong>Certificate Trust Settings</strong> &rarr; Toggle ON for mitmproxy</div>
                </div>
            </div>
            
            <div class="section-title">Part 3: Capture Tokens</div>
            
            <div class="step">
                <span class="step-num">12</span>
                <div class="step-content">
                    <div class="step-title">Open myQ app</div>
                    <div class="step-detail">Open the myQ app on your iPhone. Tokens will be captured automatically.</div>
                </div>
            </div>
            
            <div class="step">
                <span class="step-num">13</span>
                <div class="step-content">
                    <div class="step-title">Check this page</div>
                    <div class="step-detail">Refresh this page - the status above should show all green</div>
                </div>
            </div>
            
            <div class="done-note">
                <strong>When done:</strong> Go back to Settings &rarr; Wi-Fi &rarr; (i) &rarr; Configure Proxy &rarr; <strong>Off</strong> to disable the proxy
            </div>
        </div>
        
        <div class="footer">
            Page auto-refreshes every 5 seconds<br>
            API: http://{local_ip}:8000
        </div>
    </div>
</body>
</html>"""
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())


def run_status_server():
    """Run the status web server in background"""
    try:
        server = HTTPServer(('0.0.0.0', STATUS_PORT), StatusHandler)
        server.serve_forever()
    except Exception:
        pass


def print_banner():
    """Print startup banner"""
    local_ip = get_local_ip()
    
    print("""
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║   ███╗   ███╗██╗   ██╗ ██████╗     ██████╗ █████╗ ██████╗ ████████╗  ║
║   ████╗ ████║╚██╗ ██╔╝██╔═══██╗   ██╔════╝██╔══██╗██╔══██╗╚══██╔══╝  ║
║   ██╔████╔██║ ╚████╔╝ ██║   ██║   ██║     ███████║██████╔╝   ██║     ║
║   ██║╚██╔╝██║  ╚██╔╝  ██║▄▄ ██║   ██║     ██╔══██║██╔═══╝    ██║     ║
║   ██║ ╚═╝ ██║   ██║   ╚██████╔╝   ╚██████╗██║  ██║██║        ██║     ║
║   ╚═╝     ╚═╝   ╚═╝    ╚══▀▀═╝     ╚═════╝╚═╝  ╚═╝╚═╝        ╚═╝     ║
║                                                                       ║
║                   Auto Token Capture Proxy                            ║
╠═══════════════════════════════════════════════════════════════════════╣
║                                                                       ║
║  📱 PHONE SETUP                                                       ║
║  ─────────────────────────────────────────────────────────────────    ║""")
    print(f"║  Configure your phone's WiFi proxy to:                               ║")
    print(f"║                                                                       ║")
    print(f"║       Server: {local_ip:<20}  Port: {PROXY_PORT:<20}   ║")
    print(f"║                                                                       ║")
    print("""║  ─────────────────────────────────────────────────────────────────    ║
║                                                                       ║
║  📋 SETUP STEPS                                                       ║
║  ─────────────────────────────────────────────────────────────────    ║
║                                                                       ║
║  iOS:                                                                 ║
║    1. Settings → WiFi → (i) next to network → Configure Proxy        ║
║    2. Select "Manual" and enter the server/port above                 ║
║    3. Open Safari → http://mitm.it → Install iOS certificate         ║
║    4. Settings → General → About → Certificate Trust Settings        ║
║    5. Enable trust for mitmproxy certificate                          ║
║                                                                       ║
║  Android:                                                             ║
║    1. Settings → WiFi → Long press network → Modify → Advanced        ║
║    2. Set Proxy to "Manual" and enter the server/port above          ║
║    3. Open Browser → http://mitm.it → Download Android certificate   ║
║    4. Settings → Security → Install certificate from storage         ║
║                                                                       ║
║  ─────────────────────────────────────────────────────────────────    ║
║                                                                       ║""")
    print(f"║  🌐 Status Page: http://{local_ip}:{STATUS_PORT:<31}    ║")
    print("""║                                                                       ║
║  ─────────────────────────────────────────────────────────────────    ║
║                                                                       ║
║  Once configured, just open the myQ app on your phone.                ║
║  Tokens will be captured automatically!                               ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
""")


# Export addon for mitmproxy
addons = [MyQTokenCapture()]


if __name__ == "__main__":
    import subprocess
    
    # Check if mitmproxy is installed
    if not MITMPROXY_AVAILABLE:
        print("❌ mitmproxy not installed!")
        print("   Run: pip install mitmproxy")
        print("   Or:  uv add mitmproxy")
        sys.exit(1)
    
    print_banner()
    
    # Start status server in background
    status_thread = threading.Thread(target=run_status_server, daemon=True)
    status_thread.start()
    print(f"✓ Status server started on port {STATUS_PORT}")
    
    # Run mitmdump
    print(f"✓ Starting proxy on port {PROXY_PORT}...")
    print("─" * 71)
    print("Waiting for myQ app traffic... (Press Ctrl+C to stop)\n")
    
    try:
        subprocess.run([
            "mitmdump", 
            "-s", __file__, 
            "-p", str(PROXY_PORT),
            "--set", "ssl_insecure=true",
            "--set", "stream_large_bodies=0",  # Don't stream, capture full response
        ])
    except KeyboardInterrupt:
        print("\n\n✓ Proxy stopped.")
        
        # Show final token status
        if TOKENS_FILE.exists():
            tokens = json.loads(TOKENS_FILE.read_text())
            if tokens.get('access_token'):
                print("✓ Tokens saved to myq_tokens.json")
            else:
                print("⚠ No tokens captured yet")
