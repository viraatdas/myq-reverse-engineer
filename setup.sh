#!/bin/bash
# MyQ Garage Door Controller - Setup Script
# Run this after cloning the repository

set -e

echo "🚗 MyQ Garage Door Controller Setup"
echo "===================================="
echo ""

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first:"
    echo "   https://docs.docker.com/get-docker/"
    exit 1
fi

echo "✅ Docker found"

# Check for Docker Compose
if ! command -v docker compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

echo "✅ Docker Compose found"

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    echo ""
    echo "📝 Creating .env file..."
    cp .env.example .env
    
    # Generate a secure API key
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32)
    sed -i.bak "s/your-secure-api-key-here/$API_KEY/" .env && rm -f .env.bak
    
    echo "✅ Created .env with generated API key"
    echo "   API Key: $API_KEY"
else
    echo "✅ .env file already exists"
fi

# Create tokens file if it doesn't exist
if [ ! -f myq_tokens.json ]; then
    echo ""
    echo "📝 Creating myq_tokens.json placeholder..."
    cp myq_tokens.example.json myq_tokens.json
    echo "✅ Created myq_tokens.json"
    echo ""
    echo "⚠️  You need to capture your MyQ tokens!"
    echo "   See README.md for instructions on using Proxyman."
else
    echo "✅ myq_tokens.json already exists"
fi

echo ""
echo "===================================="
echo "🎉 Setup complete!"
echo ""
echo "Next steps:"
echo "1. Capture your MyQ tokens (see README.md)"
echo "2. Edit myq_tokens.json with your captured tokens"
echo "3. Run: docker compose up -d"
echo "4. Test: curl http://localhost:8000/status"
echo ""

