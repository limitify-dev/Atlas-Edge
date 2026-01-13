# Atlas Edge - Quick Start Guide

## 5-Minute Setup

### 1. Hardware Setup (2 minutes)

Connect RC522 RFID reader to Raspberry Pi:

```
RC522    →    Raspberry Pi
SDA      →    GPIO 8 (Pin 24)
SCK      →    GPIO 11 (Pin 23)
MOSI     →    GPIO 10 (Pin 19)
MISO     →    GPIO 9 (Pin 21)
GND      →    Ground (Pin 6)
RST      →    GPIO 25 (Pin 22)
3.3V     →    3.3V (Pin 1)
```

### 2. Software Installation (3 minutes)

```bash
# 1. Copy files to Raspberry Pi
scp -r atlas-edge/ pi@<your-pi-ip>:~/

# 2. SSH into Raspberry Pi
ssh pi@<your-pi-ip>

# 3. Run installation
cd ~/atlas-edge
chmod +x install.sh
./install.sh
```

During installation, provide:
- Backend API URL (e.g., `https://api.example.com/api`)
- API Key
- Device ID (default: `atlas-edge-001`)

### 3. Verify Installation

```bash
# Check services are running
sudo systemctl status atlas-edge-attendance.service
sudo systemctl status atlas-edge-web.service

# Get your Pi's IP address
hostname -I
```

### 4. Access Web Portal

Open browser: `http://<pi-ip-address>:8080`

## Quick Commands

```bash
# View live logs
sudo journalctl -u atlas-edge-attendance.service -f

# Restart services
sudo systemctl restart atlas-edge-*

# Stop all services
sudo systemctl stop atlas-edge-*

# Check system status
systemctl list-units atlas-edge-*
```

## Test RFID Reader

```bash
cd ~/atlas-edge
python3 services/rfid_reader.py
# Hold RFID card near reader
# Press Ctrl+C to stop
```

## Troubleshooting

**RFID not reading?**
```bash
# Enable SPI
sudo raspi-config
# Interface Options → SPI → Enable → Reboot
```

**Can't access web portal?**
```bash
# Check if web service is running
sudo systemctl status atlas-edge-web.service

# Check port
sudo netstat -tlnp | grep 8080
```

**Backend not connecting?**
```bash
# Test API connectivity
curl https://your-backend.com/api/health

# Check config
cat ~/atlas-edge/config/config.json
```

## Next Steps

1. ✅ Verify RFID reader works
2. ✅ Confirm backend connectivity
3. ✅ Test attendance logging
4. ✅ Monitor web dashboard
5. ✅ Setup remote SSH access

## Need Help?

- Check logs: `~/atlas-edge/logs/`
- Read full docs: `README.md`
- View service status: `sudo systemctl status atlas-edge-*`

---

You're ready! Hold an RFID card near the reader to log attendance.
