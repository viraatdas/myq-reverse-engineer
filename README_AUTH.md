# MyQ Authentication Options

## Summary

After extensive testing, here are the working options for MyQ authentication:

### ✅ Option 1: Auto-Capture Proxy (RECOMMENDED)
The most reliable method is running a proxy that automatically captures tokens when you open the myQ app on your phone.

**Setup:**
```bash
# Install mitmproxy
pip install mitmproxy

# Run the auto-capture proxy
python auto_capture_proxy.py
```

Then configure your phone's WiFi proxy to point to your computer's IP on port 8888. When you open the myQ app, tokens are automatically captured and saved to `myq_tokens.json`.

**Pros:**
- Works reliably every time
- Captures fresh tokens, refresh tokens, and Cloudflare cookies
- No need to manually copy tokens

**Cons:**
- Requires phone proxy configuration
- Need to install mitmproxy CA certificate on phone

### ✅ Option 2: Manual Token Capture
Use Proxyman, Charles, or similar tools to capture tokens from the myQ app.

**Setup:**
1. Configure proxy on your phone to your Mac running Proxyman
2. Open myQ app and login
3. Copy tokens from the captured `/connect/token` response
4. Run `python update_tokens.py` to update the tokens file

**Pros:**
- Simple, works reliably
- No additional setup needed if you already use Proxyman

**Cons:**
- Manual process every few weeks

### ⚠️ Option 3: Browser Login (Experimental)
Automated browser login using undetected-chromedriver.

**Status:** Gets past Cloudflare and successfully logs in, but token exchange fails with "application unauthorized" error. This appears to be a security measure where MyQ only allows token exchanges from actual mobile app contexts.

**Usage (if you want to try):**
```bash
python browser_login.py
```

### ❌ Option 4: Direct API Login
Pure HTTP API login without browser automation.

**Status:** Blocked by Cloudflare rate limiting (429) and bot detection.

---

## Token Refresh

Once you have valid tokens, the `myq_api.py` handles automatic token refresh:
- Access tokens expire in ~30 minutes
- Refresh tokens work for longer (weeks/months)
- When refresh token expires, you'll need to capture new tokens

## Files

| File | Description |
|------|-------------|
| `myq_api.py` | Main API client with auto-refresh |
| `server.py` | FastAPI server |
| `auto_capture_proxy.py` | mitmproxy script for auto-capturing tokens |
| `browser_login.py` | Experimental browser-based login |
| `update_tokens.py` | Helper to manually update tokens |
| `myq_tokens.json` | Stored tokens (auto-updated) |

## Recommendations

1. **For daily use:** Just use captured tokens. Token refresh works automatically.
2. **For convenience:** Set up the auto-capture proxy once, then just open the myQ app when tokens expire.
3. **Long-term:** Consider [ratgdo](https://paulwieland.github.io/ratgdo/) for local control without MyQ cloud.

