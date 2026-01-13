#!/bin/bash
# Atlas Edge Installation Script
# Run this on your Raspberry Pi to set up the system

set -e

echo "=========================================="
echo "Atlas Edge Installation"
echo "=========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running on Raspberry Pi
if [ ! -f /proc/device-tree/model ]; then
    echo -e "${YELLOW}Warning: This doesn't appear to be a Raspberry Pi${NC}"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Update system
echo -e "${GREEN}Updating system packages...${NC}"
sudo apt-get update
sudo apt-get upgrade -y

# Install Python and pip
echo -e "${GREEN}Installing Python dependencies...${NC}"
sudo apt-get install -y python3 python3-pip python3-dev

# Install SPI tools for RFID reader
echo -e "${GREEN}Installing SPI tools...${NC}"
sudo apt-get install -y python3-spidev

# Enable SPI
echo -e "${GREEN}Enabling SPI interface...${NC}"
sudo raspi-config nonint do_spi 0

# Create project directory
PROJECT_DIR="/home/pi/atlas-edge"
echo -e "${GREEN}Creating project directory: $PROJECT_DIR${NC}"
mkdir -p $PROJECT_DIR
mkdir -p $PROJECT_DIR/logs
mkdir -p $PROJECT_DIR/data

# Copy project files
echo -e "${GREEN}Setting up project files...${NC}"
# Note: You should have already copied your files to the Pi
# This script assumes files are in the current directory

if [ -f "requirements.txt" ]; then
    cp -r * $PROJECT_DIR/
else
    echo -e "${RED}Error: Project files not found in current directory${NC}"
    echo "Please ensure you're running this script from the project directory"
    exit 1
fi

# Install Python packages
echo -e "${GREEN}Installing Python packages...${NC}"
cd $PROJECT_DIR
pip3 install -r requirements.txt

# Set up configuration
echo -e "${GREEN}Setting up configuration...${NC}"
if [ ! -f "$PROJECT_DIR/config/config.json" ]; then
    echo -e "${RED}Error: config.json not found${NC}"
    exit 1
fi

# Prompt for API configuration
echo -e "${YELLOW}Please configure your Atlas backend API:${NC}"
read -p "Backend API URL: " API_URL
read -p "API Key: " API_KEY
read -p "Device ID [atlas-edge-001]: " DEVICE_ID
DEVICE_ID=${DEVICE_ID:-atlas-edge-001}

# Update configuration
python3 << EOF
import json
with open('$PROJECT_DIR/config/config.json', 'r') as f:
    config = json.load(f)
config['server']['api_url'] = '$API_URL'
config['server']['api_key'] = '$API_KEY'
config['device']['id'] = '$DEVICE_ID'
with open('$PROJECT_DIR/config/config.json', 'w') as f:
    json.dump(config, f, indent=2)
EOF

# Set permissions
echo -e "${GREEN}Setting permissions...${NC}"
sudo chown -R pi:pi $PROJECT_DIR
chmod +x $PROJECT_DIR/attendance_service.py
chmod +x $PROJECT_DIR/web/app.py

# Install systemd services
echo -e "${GREEN}Installing systemd services...${NC}"
sudo cp $PROJECT_DIR/atlas-edge-attendance.service /etc/systemd/system/
sudo cp $PROJECT_DIR/atlas-edge-web.service /etc/systemd/system/
sudo systemctl daemon-reload

# Enable services
echo -e "${GREEN}Enabling services...${NC}"
sudo systemctl enable atlas-edge-attendance.service
sudo systemctl enable atlas-edge-web.service

# Start services
echo -e "${GREEN}Starting services...${NC}"
sudo systemctl start atlas-edge-attendance.service
sudo systemctl start atlas-edge-web.service

# Check service status
sleep 2
echo ""
echo -e "${GREEN}Checking service status...${NC}"
sudo systemctl status atlas-edge-attendance.service --no-pager -l
echo ""
sudo systemctl status atlas-edge-web.service --no-pager -l

# Get IP address
IP_ADDR=$(hostname -I | awk '{print $1}')

echo ""
echo "=========================================="
echo -e "${GREEN}Installation Complete!${NC}"
echo "=========================================="
echo ""
echo "Atlas Edge is now running on your Raspberry Pi"
echo ""
echo -e "Web Portal: ${GREEN}http://$IP_ADDR:8080${NC}"
echo ""
echo "Useful commands:"
echo "  - View attendance logs:  sudo journalctl -u atlas-edge-attendance.service -f"
echo "  - View web logs:         sudo journalctl -u atlas-edge-web.service -f"
echo "  - Restart attendance:    sudo systemctl restart atlas-edge-attendance.service"
echo "  - Restart web:           sudo systemctl restart atlas-edge-web.service"
echo "  - Stop all:              sudo systemctl stop atlas-edge-*"
echo ""
echo -e "${YELLOW}Note: Make sure your RFID reader is properly connected to the Pi${NC}"
echo ""
