# MyQ Garage Door Controller

A self-hosted REST API to control your MyQ garage door, with support for iOS Shortcuts automation.

**Why this exists:** MyQ removed their official API and blocks third-party integrations. This project reverse-engineers the MyQ mobile app's API to provide reliable garage door control.

## Features

- 🚗 **REST API** - Simple endpoints to open, close, and check door status
- 🔄 **Auto Token Refresh** - Tokens automatically refresh, no manual intervention needed
- 📱 **iOS Shortcuts Support** - Automate with Bluetooth triggers (e.g., Tesla connection)
- 🐳 **Docker Ready** - One-command deployment
- 🔒 **Secure** - Optional API key protection

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A MyQ account with a compatible garage door opener
- iPhone with Proxyman app (for initial token capture)

### 1. Clone and Configure

```bash
git clone https://github.com/yourusername/myq-controller.git
cd myq-controller

# Create configuration files
cp .env.example .env
cp myq_tokens.example.json myq_tokens.json

# Edit .env and set your API_KEY
nano .env
```

### 2. Capture Tokens (One-Time Setup)

Since MyQ blocks automated logins with Cloudflare, you'll need to capture tokens from the official app:

1. **Install [Proxyman](https://apps.apple.com/app/proxyman/id1551292695)** on your iPhone (free)
2. Open Proxyman → Enable SSL Proxying for `*.myq-cloud.com`
3. Open the **MyQ app** → Log out → Log back in
4. In Proxyman, find the request to `partner-identity.myq-cloud.com/connect/token`
5. Copy the response JSON into `myq_tokens.json`

The response will look like:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "ABC123...",
  "expires_in": 1800,
  "token_type": "Bearer",
  "scope": "MyQ_Residential offline_access"
}
```

You'll also need to add:
- `account_id` - Found in requests to `accounts.myq-cloud.com`
- `device_serial` - Your garage door opener's serial number
- `cf_cookie` - The `__cf_bm` cookie from request headers

### 3. Run with Docker

```bash
# Start the API server
docker compose up -d

# Check logs
docker logs myq-api -f

# Test it
curl http://localhost:8000/status
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API info and available endpoints |
| `/status` | GET | Get current door state |
| `/open` | POST | Open the garage door |
| `/close` | POST | Close the garage door |
| `/toggle` | POST | Toggle door state |
| `/devices` | GET | List all MyQ devices |
| `/health` | GET | Health check |

### Example Usage

```bash
# Get door status
curl http://your-server:8000/status

# Open the door
curl -X POST http://your-server:8000/open

# Close the door
curl -X POST http://your-server:8000/close

# With API key (if configured)
curl -H "X-API-Key: your-api-key" http://your-server:8000/status
```

## Deployment Options

### DigitalOcean (Recommended)

1. Create a Droplet (512MB RAM is sufficient, ~$4/month)
2. SSH into the server:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Clone the repo
git clone https://github.com/yourusername/myq-controller.git
cd myq-controller

# Configure
cp .env.example .env
nano .env  # Set your API_KEY

# Add your tokens
nano myq_tokens.json  # Paste captured tokens

# Start
docker compose up -d
```

3. Configure firewall to allow port 8000

### Other Platforms

Works on any platform that supports Docker:
- AWS EC2 / Lightsail
- Google Cloud Run
- Azure Container Instances
- Raspberry Pi
- Home server / NAS

## iOS Shortcuts Setup

Create automations to control your garage based on location and Bluetooth:

### Open Garage When Arriving Home

1. **Shortcuts app** → **Automation** tab → **+**
2. Select **Bluetooth** → Choose your car's Bluetooth → **Is Connected**
3. Add action: **Get Contents of URL**
   - URL: `http://your-server:8000/open`
   - Method: POST
4. Add **If** condition to check location (optional):
   - Get Distance from Current Location to Home
   - If Distance < 0.1 miles → Run the URL action
5. Turn OFF "Ask Before Running"

### Close Garage When Leaving

1. Create automation triggered by **Leave** location (your home)
2. Add action: **Get Contents of URL**
   - URL: `http://your-server:8000/close`
   - Method: POST

## Token Auto-Capture (Optional)

For a more automated token capture experience, you can run the capture proxy:

```bash
# Start the capture proxy
docker compose --profile capture up -d

# Configure your phone to use the proxy:
# Server: your-server-ip
# Port: 8888

# Visit http://your-server-ip:8889 for setup instructions
```

## Troubleshooting

### "Token refresh failed" Error

Your refresh token has expired. You'll need to capture new tokens:
1. Open MyQ app while Proxyman is capturing
2. Log out and log back in
3. Update `myq_tokens.json` with the new tokens

### "Cloudflare challenge" Errors

MyQ uses Cloudflare protection. Make sure:
- The `cf_cookie` in your tokens file is recent
- You're using the official app (not automated login)

### Door Not Responding

1. Check the MyQ app works directly
2. Verify your `device_serial` is correct
3. Check API logs: `docker logs myq-api`

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   iPhone    │────▶│  Your API   │────▶│  MyQ Cloud  │
│  Shortcuts  │     │   Server    │     │    API      │
└─────────────┘     └─────────────┘     └─────────────┘
                           │
                    ┌──────┴──────┐
                    │ Token Store │
                    │   (JSON)    │
                    └─────────────┘
```

## Security Considerations

- **API Key**: Always set a strong `API_KEY` in production
- **HTTPS**: Use a reverse proxy (nginx, Caddy) with SSL in production
- **Firewall**: Restrict access to trusted IPs if possible
- **Tokens**: Never commit `myq_tokens.json` to version control

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## Disclaimer

This project is not affiliated with MyQ or Chamberlain Group. Use at your own risk. The MyQ API is undocumented and may change at any time.

## License

MIT License - See [LICENSE](LICENSE) for details.
