# MyQ Garage Door Controller

A self-hosted REST API for controlling a MyQ garage door, designed for iOS Shortcuts automation and deployed to AWS Lambda.

**Why this exists:** Chamberlain shut off third-party access to MyQ and blocks unofficial API clients. This project reverse-engineers the MyQ Android app's OAuth flow and device API to give you back programmatic control of your own garage door.

```
┌──────────────┐    HTTPS     ┌─────────────┐   OAuth 2.0   ┌─────────────┐
│  iOS         │─────────────▶│ API Gateway │──────────────▶│  MyQ Cloud  │
│  Shortcuts   │  X-API-Key   │  + Lambda   │  Bearer token │     API     │
└──────────────┘              └──────┬──────┘               └─────────────┘
                                     │
                             ┌───────▼────────┐
                             │ SSM Parameter  │  rotating tokens,
                             │ Store (KMS)    │  encrypted at rest
                             └────────────────┘
```

## Features

- **Built for Shortcuts** — every command works over plain `GET`, authenticates via a query parameter, and can return plain text instead of JSON
- **Confirmed commands** — `?wait=true` polls until the door has actually finished moving, so an automation can report what happened rather than what was requested
- **Durable tokens** — MyQ access tokens expire every ~30 minutes; refreshed tokens are written back to SSM Parameter Store so they survive restarts and cold starts
- **Fails closed** — no API key configured means the API refuses requests rather than exposing an open garage door
- **~$0/month** — Lambda + API Gateway at garage-door request volumes sits inside the free tier
- **Idempotent** — closing an already-closed door is a no-op, because Shortcuts automations fire more than once

## Quick start

### 1. Install

```bash
git clone https://github.com/viraatdas/myq-reverse-engineer.git
cd myq-reverse-engineer
uv venv && uv pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
python -c "import secrets; print('API_KEY=' + secrets.token_urlsafe(32))" >> .env
```

Set `MYQ_EMAIL` and `MYQ_PASSWORD` in `.env` too — they are used only by the one-time login below, never by the deployed API.

### 3. Deploy to AWS

```bash
./deploy/deploy.sh
```

This is idempotent — re-run it any time to ship changes. It creates:

| Resource | Name | Purpose |
|---|---|---|
| Lambda function | `myq-api` | The API (Python 3.13, arm64) |
| IAM role | `myq-api-role` | Read/write exactly one SSM parameter |
| SSM parameter | `/myq/tokens` | Encrypted MyQ tokens |
| API Gateway | `myq-api` | Public HTTPS endpoint |

It prints your API URL at the end. Save it — you'll need it for Shortcuts.

### 4. Log in to MyQ

```bash
python -m myq.cli setup
```

This opens the MyQ sign-in page in your browser, waits for you to log in, then uploads the resulting tokens to AWS.

**This step needs a real browser and cannot be automated.** MyQ puts Cloudflare in front of its login page, which blocks headless browsers. You log in normally; the browser then fails to open a `com.myqops://android?code=...` link — that failure is expected, and you paste that URL back into the terminal.

You only do this once. After that the API refreshes its own tokens indefinitely.

### 5. Verify

```bash
curl "https://YOUR-API-URL/status?key=YOUR_API_KEY"
```

## API reference

Interactive docs are at `https://YOUR-API-URL/docs`.

| Endpoint | Methods | Description |
|---|---|---|
| `/health` | GET | Health check — **no auth required** |
| `/status` | GET | Full door status |
| `/state` | GET | Bare state as plain text: `open`, `closed`, `opening`, `closing` |
| `/open` | GET, POST | Open the door |
| `/close` | GET, POST | Close the door |
| `/toggle` | GET, POST | Open if closed, close if open |
| `/doors` | GET | All garage doors and their states |
| `/devices` | GET | All MyQ devices, raw |
| `/admin/refresh` | POST | Force a token refresh |
| `/admin/reset` | POST | Drop cached state |

### Authentication

Three interchangeable forms, so every client can use one:

```bash
curl -H "X-API-Key: KEY"            https://YOUR-API-URL/status   # preferred
curl -H "Authorization: Bearer KEY" https://YOUR-API-URL/status
curl "https://YOUR-API-URL/status?key=KEY"                        # for Shortcuts URLs
```

### Query parameters

| Parameter | Applies to | Description |
|---|---|---|
| `key` | all | API key, for clients that cannot set headers |
| `format` | most | `json` (default) or `text` |
| `wait` | commands | `true` polls until the door finishes moving |
| `timeout` | commands | Seconds to wait, default 25 |
| `serial` | most | Target a specific door on multi-door accounts |
| `force` | open/close | Send the command even if already in that state |

### Examples

```bash
# Is it open? -> "closed"
curl "https://YOUR-API-URL/state?key=KEY"

# Close it and wait for confirmation -> "Garage Door is closed"
curl "https://YOUR-API-URL/close?key=KEY&wait=true&format=text"

# Fire and forget
curl -X POST -H "X-API-Key: KEY" "https://YOUR-API-URL/open"
```

### Status codes

Errors are typed so a client can tell recoverable from unrecoverable:

| Code | Meaning |
|---|---|
| 401 | Missing or wrong API key |
| 404 | No such garage door |
| 409 | The opener is offline — the command would have vanished silently |
| 429 | Rate limited |
| 502 | MyQ is down or returned an error |
| 503 | No API key configured, or MyQ tokens need a re-login |
| 504 | Command sent, but the door did not reach the target state in time |

## iOS Shortcuts

See **[docs/SHORTCUTS.md](docs/SHORTCUTS.md)** for step-by-step setup, including Bluetooth and location-triggered automations.

The short version — one action, no JSON parsing:

> **Get Contents of URL** → `https://YOUR-API-URL/open?key=YOUR_KEY`

## Local development

```bash
python -m myq.cli login     # get tokens locally
python -m myq.cli serve     # run on http://localhost:8000
python -m myq.cli test      # print door state
python -m myq.cli open --wait
pytest                      # test suite, no network required
```

| Command | Description |
|---|---|
| `myq setup` | Log in and upload tokens to AWS (first run) |
| `myq login` | Browser login, saves tokens locally |
| `myq push-tokens` | Upload local tokens to SSM |
| `myq pull-tokens` | Download tokens from SSM |
| `myq status` | Show stored token state |
| `myq test` | Print door state from MyQ |
| `myq serve` | Run the API locally |
| `myq open` / `myq close` | Send a command directly |

## Project layout

```
myq/
  api.py             FastAPI app — routes, auth, rate limiting, error mapping
  client.py          Async MyQ client — OAuth refresh, device and command calls
  tokens.py          Token model + file/SSM storage backends
  login.py           One-time interactive OAuth login (local only)
  config.py          Settings
  errors.py          Typed errors mapped to HTTP status codes
  models.py          Response models with plain-text renderings
  cli.py             Command line interface
  lambda_handler.py  AWS Lambda entry point
deploy/deploy.sh     Idempotent AWS deployment
tools/               Optional mitmproxy token capture
tests/               Test suite
```

## Security notes

- **Set a strong `API_KEY`.** Anyone with your URL and key can open your garage. The deploy script refuses to run with a placeholder key.
- The API key is compared in constant time, and failed attempts are rate limited per IP.
- Tokens are stored as an SSM `SecureString`, KMS-encrypted at rest. The Lambda role can access exactly that one parameter.
- Error responses never include upstream detail or token material; that goes to CloudWatch only.
- `.env` and `myq_tokens.json` are gitignored. Do not commit them.
- API Gateway has no authorizer by design — auth is the app's API key, because iOS Shortcuts cannot do SigV4 request signing.

## Troubleshooting

**`503 MyQ authentication expired`** — the refresh token died (happens if you change your MyQ password or revoke sessions). Run `python -m myq.cli setup` again.

**`409 The garage door opener is offline`** — the opener has lost its Wi-Fi link to MyQ. Check the MyQ app; commands sent while offline do nothing.

**`504` on `?wait=true`** — the door is slower than the timeout. Pass a larger `timeout`, but note that API Gateway caps a request at 30 seconds.

**Logs:**
```bash
aws logs tail /aws/lambda/myq-api --since 15m --follow
```

## Self-hosting instead of AWS

A `Dockerfile` and `docker-compose.yml` are included for running on a Pi, NAS, or VPS. Put it behind a TLS-terminating reverse proxy before exposing it — Shortcuts should not send your API key over plain HTTP.

## Disclaimer

Not affiliated with MyQ or Chamberlain Group. The MyQ API is undocumented and may change or break without notice. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE).
