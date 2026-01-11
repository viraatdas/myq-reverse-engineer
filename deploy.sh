#!/bin/bash
#
# Deploy MyQ Controller to a VPS
# 
# Usage:
#   ./deploy.sh user@your-server-ip
#
# Prerequisites:
#   - SSH access to the server
#   - Docker installed on the server
#

set -e

SERVER=$1

if [ -z "$SERVER" ]; then
    echo "Usage: ./deploy.sh user@server-ip"
    echo "Example: ./deploy.sh root@167.71.123.45"
    exit 1
fi

echo "🚀 Deploying MyQ Controller to $SERVER"
echo ""

# Files to deploy
FILES="
    server.py
    myq_api.py
    auto_capture_proxy.py
    myq_tokens.json
    .env
    Dockerfile
    docker-compose.yml
    requirements.txt
"

# Create remote directory
echo "📁 Creating remote directory..."
ssh $SERVER "mkdir -p ~/myq-controller"

# Copy files
echo "📦 Copying files..."
for file in $FILES; do
    if [ -f "$file" ]; then
        scp "$file" "$SERVER:~/myq-controller/" 2>/dev/null || echo "  Skipped: $file (not found)"
    fi
done

# Deploy on server
echo "🐳 Starting Docker containers..."
ssh $SERVER << 'EOF'
cd ~/myq-controller

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi

# Install docker compose plugin if not present
if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# Build and start
docker compose down 2>/dev/null || true
docker compose build
docker compose up -d

echo ""
echo "✅ Deployment complete!"
docker compose ps
EOF

echo ""
echo "🎉 Done! Your MyQ controller is running at:"
echo "   API: http://$SERVER:8000"
echo ""
echo "To start token capture proxy:"
echo "   ssh $SERVER 'cd ~/myq-controller && docker compose --profile capture up -d'"

