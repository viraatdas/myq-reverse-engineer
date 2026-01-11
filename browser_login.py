#!/usr/bin/env python3
"""
Browser-based MyQ Login

Uses undetected-chromedriver to bypass Cloudflare protection by running
a real Chrome browser that looks like a normal user.

This is more likely to succeed than pure HTTP requests because:
1. Real browser fingerprint
2. JavaScript execution
3. Proper TLS handshake
4. Cloudflare JS challenge solving

Requirements:
    pip install undetected-chromedriver selenium

Usage:
    python browser_login.py
"""

import json
import time
import os
import base64
import hashlib
import secrets
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"

# OAuth constants - Try iOS since user's captured tokens work
# Set USE_IOS=1 to use iOS client, otherwise uses Android
USE_IOS = os.getenv("USE_IOS", "1").lower() in ("1", "true", "yes")

if USE_IOS:
    OAUTH_CLIENT_ID = "IOS_CGI_MYQ"
    OAUTH_CLIENT_SECRET = ""  # iOS doesn't use client secret for PKCE
    OAUTH_REDIRECT_URI = "com.myqops://ios"
    MYQ_APP_ID = "c4be0d49-a710-4490-a6dc-7e903c5c3b58"
    MYQ_APP_VERSION = "5.299.1.59103"
    MYQ_API_USER_AGENT = "myQ/299.1.59103 CFNetwork/3860.200.71 Darwin/25.1.0"
else:
    OAUTH_CLIENT_ID = "ANDROID_CGI_MYQ"
    OAUTH_CLIENT_SECRET = base64.b64decode("VUQ0RFhuS3lQV3EyNUJTdw==").decode()
    OAUTH_REDIRECT_URI = "com.myqops://android"
    MYQ_APP_ID = "D9D7B25035D549D8A3EA16A9FFB8C927D4A19B55B8944011B2670A8321BF8312"
    MYQ_APP_VERSION = "5.242.0.72704"
    MYQ_API_USER_AGENT = "sdk_gphone_x86/Android 11"

print(f"   Using {'iOS' if USE_IOS else 'Android'} client: {OAUTH_CLIENT_ID}")


def generate_pkce_pair():
    """Generate PKCE code verifier and challenge."""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    return code_verifier, code_challenge


def load_existing_tokens() -> dict:
    """Load existing tokens to preserve account_id, etc."""
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_tokens(tokens: dict):
    """Save tokens to file"""
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    print(f"✅ Tokens saved to {TOKENS_FILE}")


def browser_login():
    """Perform OAuth login using undetected Chrome browser"""
    
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        print("❌ Required packages not installed.")
        print("   Run: pip install undetected-chromedriver selenium")
        return False
    
    email = os.getenv("MYQ_EMAIL")
    password = os.getenv("MYQ_PASSWORD")
    
    if not email or not password:
        print("❌ MYQ_EMAIL and MYQ_PASSWORD must be set in .env file")
        return False
    
    print(f"🔐 Starting browser login for {email[:3]}***...")
    
    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()
    
    # Build auth URL
    from urllib.parse import quote
    redirect_uri_encoded = quote(OAUTH_REDIRECT_URI, safe='')  # com.myqops%3A%2F%2Fios or android
    
    auth_params = (
        f"acr_values=unified_flow%3Av1%20%20brand%3Amyq"  # double space encoded as %20%20
        f"&client_id={OAUTH_CLIENT_ID}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&prompt=login"
        f"&ui_locales=en-US"
        f"&redirect_uri={redirect_uri_encoded}"
        f"&response_type=code"
        f"&scope=MyQ_Residential%20offline_access"
    )
    auth_url = f"https://partner-identity.myq-cloud.com/connect/authorize?{auth_params}"
    
    # Create undetected Chrome driver
    print("🌐 Launching Chrome browser...")
    options = uc.ChromeOptions()
    options.add_argument("--disable-popup-blocking")
    
    # Enable performance logging to capture network requests
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL', 'browser': 'ALL'})
    
    # Run headless if HEADLESS env var is set
    if os.getenv("HEADLESS", "").lower() in ("1", "true", "yes"):
        options.add_argument("--headless=new")
    
    driver = None
    try:
        driver = uc.Chrome(options=options)
        driver.set_page_load_timeout(30)
        
        # Enable CDP network domain to intercept requests
        driver.execute_cdp_cmd('Network.enable', {})
        
        # Navigate to auth URL
        print("📄 Loading authorization page...")
        driver.get(auth_url)
        
        # Wait for login form
        print("⏳ Waiting for login form...")
        wait = WebDriverWait(driver, 30)
        
        # Wait a bit for Cloudflare challenge to complete
        time.sleep(5)
        
        # Save screenshot for debugging
        screenshot_path = Path(__file__).parent / "login_debug.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"📸 Saved screenshot to {screenshot_path}")
        
        # Check for Cloudflare challenge
        page_source = driver.page_source.lower()
        if "checking your browser" in page_source or "cf-browser-verification" in page_source:
            print("⏳ Cloudflare challenge detected, waiting...")
            time.sleep(10)
            driver.save_screenshot(str(screenshot_path))
        
        # Wait for email field to be present
        try:
            email_field = wait.until(EC.presence_of_element_located((By.NAME, "Email")))
        except Exception:
            # Try alternative selectors
            print("⚠️  Email field not found by name, trying alternatives...")
            driver.save_screenshot(str(screenshot_path))
            for selector in ["input[type='email']", "input[name='email']", "#Email", "input[placeholder*='email']"]:
                try:
                    email_field = driver.find_element(By.CSS_SELECTOR, selector)
                    if email_field:
                        print(f"✓ Found email field with: {selector}")
                        break
                except Exception:
                    continue
            else:
                print(f"❌ Could not find email field. Page title: {driver.title}")
                print(f"   Current URL: {driver.current_url}")
                raise Exception("Email field not found")
        
        # Fill in credentials
        print("📝 Entering credentials...")
        
        # Clear and fill email
        email_field.click()
        email_field.clear()
        time.sleep(0.3)
        email_field.send_keys(email)
        time.sleep(0.3)
        
        # Find and fill password field
        password_field = driver.find_element(By.NAME, "Password")
        password_field.click()
        password_field.clear()
        time.sleep(0.3)
        # Enter password directly (not character by character)
        password_field.send_keys(password)
        time.sleep(0.5)
        
        # Verify password was entered
        password_value = password_field.get_attribute("value")
        if len(password_value) != len(password):
            print(f"⚠️  Password entry issue: expected {len(password)} chars, got {len(password_value)}")
            # Try JavaScript to set the value
            driver.execute_script(f"arguments[0].value = arguments[1];", password_field, password)
            # Trigger input event so React/Vue picks up the change
            driver.execute_script("""
                var event = new Event('input', { bubbles: true });
                arguments[0].dispatchEvent(event);
            """, password_field)
            time.sleep(0.3)
        
        # Save screenshot before submit
        driver.save_screenshot(str(Path(__file__).parent / "login_before_submit.png"))
        print("📸 Saved pre-submit screenshot")
        
        # Submit form - click the "Sign In" button
        print("🚀 Submitting login...")
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        
        # Try to find Sign In button with multiple methods
        submit_button = None
        
        # Method 1: XPath with text
        try:
            submit_button = driver.find_element(By.XPATH, "//button[contains(text(), 'Sign In')]")
            print("   Found button via XPath")
        except Exception:
            pass
        
        # Method 2: CSS selectors
        if not submit_button:
            for selector in ["button[type='submit']", "button.btn-primary", "button.sign-in-btn"]:
                try:
                    submit_button = driver.find_element(By.CSS_SELECTOR, selector)
                    print(f"   Found button via CSS: {selector}")
                    break
                except Exception:
                    continue
        
        # Method 3: All buttons and find the right one
        if not submit_button:
            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    if "sign in" in btn.text.lower() or "login" in btn.text.lower():
                        submit_button = btn
                        print(f"   Found button by text: {btn.text}")
                        break
            except Exception:
                pass
        
        if submit_button:
            # Use JavaScript click as it's more reliable
            driver.execute_script("arguments[0].click();", submit_button)
            print("   Clicked via JavaScript")
        else:
            # Fallback 1: Try to submit form directly via JavaScript
            print("   Trying to submit form via JavaScript...")
            try:
                # Find and submit the form
                driver.execute_script("""
                    var form = document.querySelector('form');
                    if (form) {
                        form.submit();
                        return true;
                    }
                    return false;
                """)
            except Exception as e:
                print(f"   Form submit failed: {e}")
            
            # Fallback 2: Click the button via coordinate
            time.sleep(0.5)
            try:
                # Find button by visual position (the blue Sign In button)
                sign_in_btn = driver.execute_script("""
                    return Array.from(document.querySelectorAll('button, input[type="submit"]'))
                        .find(el => el.textContent.trim() === 'Sign In' || 
                                   el.value === 'Sign In' ||
                                   el.innerText.includes('Sign In'));
                """)
                if sign_in_btn:
                    driver.execute_script("arguments[0].click();", sign_in_btn)
                    print("   Clicked button via JS search")
                else:
                    # Last resort: press Enter
                    print("   Fallback: pressing Enter")
                    password_field.send_keys(Keys.RETURN)
            except Exception as e:
                print(f"   Button click failed: {e}, pressing Enter")
                password_field.send_keys(Keys.RETURN)
        
        time.sleep(3)  # Wait for form submission and redirect
        
        # Wait for redirect (the page will try to redirect to com.myqops://android?code=...)
        print("⏳ Waiting for OAuth redirect...")
        
        # The redirect to com.myqops:// will fail in browser, but we can catch it
        # by monitoring network activity or page content
        max_wait = 45
        auth_code = None
        last_url = driver.current_url
        
        for i in range(max_wait):
            current_url = driver.current_url
            
            # Log URL changes
            if current_url != last_url:
                print(f"   URL changed: {current_url[:80]}...")
                last_url = current_url
            
            # Check browser console logs for the redirect URL
            # The browser logs an error when it can't handle com.myqops:// scheme
            try:
                logs = driver.get_log('browser')
                for log in logs:
                    msg = log.get('message', '')
                    # Match both iOS and Android redirects
                    if 'com.myqops://' in msg and 'code=' in msg:
                        import re
                        # Extract the full URL from the error message
                        match = re.search(r"'(com\.myqops://(?:ios|android)\?[^']+)'", msg)
                        if match:
                            redirect_url = match.group(1)
                            from urllib.parse import unquote
                            redirect_url = unquote(redirect_url)
                            parsed = urlparse(redirect_url)
                            query_params = parse_qs(parsed.query)
                            auth_code = query_params.get('code', [''])[0]
                            if auth_code:
                                print(f"✅ Got authorization code from browser console!")
                                break
                if auth_code:
                    break
            except Exception:
                pass  # Some browsers don't support get_log
            
            # Check if we got redirected to the app URL (will show error page)
            if "com.myqops://" in current_url or "code=" in current_url:
                # Parse the auth code from URL
                parsed = urlparse(current_url)
                query_params = parse_qs(parsed.query)
                auth_code = query_params.get('code', [''])[0]
                if auth_code:
                    print(f"✅ Got authorization code from URL!")
                    break
            
            # Check page source for redirect URL
            try:
                page_source = driver.page_source
                import re
                # Look for the code in various places
                for pattern in [
                    r"com\.myqops://android\?code=([A-Za-z0-9_-]+)",
                    r"'com\.myqops://[^']*code=([A-Za-z0-9_-]+)[^']*'",
                ]:
                    match = re.search(pattern, page_source)
                    if match:
                        auth_code = match.group(1)
                        print(f"✅ Got authorization code from page!")
                        break
                if auth_code:
                    break
            except Exception:
                pass
            
            # Check for blocked/challenge page
            page_lower = driver.page_source.lower()
            if "verification required" in page_lower or "unusual activity" in page_lower:
                driver.save_screenshot(str(Path(__file__).parent / "login_blocked.png"))
                print("❌ Account verification or challenge required")
                return False
            
            # If we're still on login page after button click, form may not have submitted
            if i == 5 and "Account/Login" in current_url:
                # Try clicking the button again with JavaScript
                print("   Retrying form submission...")
                try:
                    driver.execute_script("""
                        var buttons = document.querySelectorAll('button');
                        for (var b of buttons) {
                            if (b.textContent.toLowerCase().includes('sign in')) {
                                b.click();
                                break;
                            }
                        }
                    """)
                except Exception:
                    pass
            
            # Check for verification success page (Cloudflare interstitial)
            if "verification successful" in page_lower:
                print("   ✓ Verification successful, waiting for redirect...")
                driver.save_screenshot(str(Path(__file__).parent / "login_success.png"))
                time.sleep(2)  # Give it more time to redirect
                continue
            
            # If we're on the login page for too long, credentials may be wrong
            if i > 10 and "Account/Login" in current_url:
                driver.save_screenshot(str(Path(__file__).parent / "login_stuck.png"))
                print("📸 Saved stuck screenshot")
                # Check for error messages
                try:
                    error_elem = driver.find_element(By.CSS_SELECTOR, ".validation-summary-errors, .field-validation-error, .alert-danger")
                    print(f"❌ Login error: {error_elem.text}")
                except Exception:
                    print(f"⚠️  Still on login page after {i} seconds...")
            
            time.sleep(1)
        
        if not auth_code:
            print("❌ Failed to get authorization code")
            print(f"   Current URL: {driver.current_url[:100]}")
            return False
        
        # Exchange code for tokens using the browser (same session)
        print(f"🔄 Exchanging code for tokens...")
        print(f"   Auth code: {auth_code[:30]}...")
        print(f"   Code verifier: {code_verifier[:20]}...")
        
        # Try using fetch() in the browser first (maintains session cookies)
        token_js = f"""
        return fetch('https://partner-identity.myq-cloud.com/connect/token', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/x-www-form-urlencoded',
            }},
            body: 'client_id={OAUTH_CLIENT_ID}&code={auth_code}&code_verifier={code_verifier}&grant_type=authorization_code&redirect_uri={OAUTH_REDIRECT_URI}&scope=MyQ_Residential%20offline_access'
        }}).then(r => r.json()).catch(e => ({{error: e.message}}));
        """
        
        try:
            token_result = driver.execute_async_script(f"""
                var callback = arguments[arguments.length - 1];
                fetch('https://partner-identity.myq-cloud.com/connect/token', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/x-www-form-urlencoded',
                    }},
                    body: 'client_id={OAUTH_CLIENT_ID}&code={auth_code}&code_verifier={code_verifier}&grant_type=authorization_code&redirect_uri={redirect_uri_encoded}&scope=MyQ_Residential%20offline_access'
                }}).then(r => r.json()).then(data => callback(data)).catch(e => callback({{error: e.message}}));
            """)
            print(f"   Browser token result: {str(token_result)[:100]}...")
            
            if token_result and 'access_token' in token_result:
                token_data = token_result
                access_token = token_data['access_token']
            elif token_result and 'error' in token_result:
                print(f"   Browser fetch error: {token_result.get('error')}")
                raise Exception("Browser token exchange failed, falling back to Python")
            else:
                raise Exception("No access_token in browser response")
        except Exception as e:
            print(f"   Browser token exchange failed: {e}, trying Python requests...")
            import requests
            
            token_data_req = {
                'client_id': OAUTH_CLIENT_ID,
                'code': auth_code,
                'code_verifier': code_verifier,
                'grant_type': 'authorization_code',
                'redirect_uri': OAUTH_REDIRECT_URI,
                'scope': 'MyQ_Residential offline_access',
            }
            
            token_headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept-Encoding': 'gzip',
                'App-Version': MYQ_APP_VERSION,
                'BrandId': '1',
                'MyQApplicationId': MYQ_APP_ID,
                'User-Agent': MYQ_API_USER_AGENT,
            }
            
            token_response = requests.post(
                "https://partner-identity.myq-cloud.com/connect/token",
                data=token_data_req,
                headers=token_headers,
            )
            
            if token_response.status_code != 200:
                print(f"❌ Token exchange failed: {token_response.status_code}")
                print(f"   {token_response.text}")
                return False
            
            token_data = token_response.json()
            access_token = token_data['access_token']
        
        # Get account info
        print("📋 Getting account info...")
        accounts_response = requests.get(
            "https://devices.myq-cloud.com/api/v6.2/Accounts",
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        account_id = ""
        if accounts_response.status_code == 200:
            accounts = accounts_response.json().get('items', [])
            if accounts:
                account_id = accounts[0].get('id', '')
        
        # Get device info
        device_serial = ""
        if account_id:
            devices_response = requests.get(
                f"https://devices.myq-cloud.com/api/v6.2/Accounts/{account_id}/Devices",
                headers={'Authorization': f'Bearer {access_token}'}
            )
            if devices_response.status_code == 200:
                for device in devices_response.json().get('items', []):
                    if device.get('device_family') == 'garagedoor':
                        device_serial = device.get('serial_number', '')
                        break
        
        # Save tokens
        existing = load_existing_tokens()
        tokens = {
            'access_token': access_token,
            'refresh_token': token_data.get('refresh_token', existing.get('refresh_token', '')),
            'expires_at': int(time.time()) + token_data.get('expires_in', 1800),
            'account_id': account_id or existing.get('account_id', ''),
            'device_serial': device_serial or existing.get('device_serial', ''),
            'expires_in': token_data.get('expires_in', 1800),
            'token_type': 'Bearer',
            'scope': 'MyQ_Residential offline_access',
            'cf_cookie': existing.get('cf_cookie', ''),
            'token_scope': token_data.get('scope', 'MyQ_Residential offline_access'),
        }
        
        save_tokens(tokens)
        print("🎉 Login successful!")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════╗
║             MyQ Browser Login                                    ║
╠══════════════════════════════════════════════════════════════════╣
║  Uses real Chrome browser to bypass Cloudflare protection        ║
║                                                                  ║
║  Set HEADLESS=1 to run without visible browser window            ║
╚══════════════════════════════════════════════════════════════════╝
    """)
    
    success = browser_login()
    exit(0 if success else 1)

