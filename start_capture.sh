#!/bin/bash
#
# MyQ Token Auto-Capture
# 
# This script starts the proxy that automatically captures MyQ tokens
# when you use the myQ app on your phone.
#

cd "$(dirname "$0")"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}Starting MyQ Token Capture Proxy...${NC}"
echo ""

# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Run the proxy
python auto_capture_proxy.py

