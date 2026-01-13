# Atlas Edge - RFID Attendance System for Raspberry Pi

Atlas Edge is a robust, offline-capable RFID attendance tracking system designed for Raspberry Pi. It logs attendance via RFID cards and syncs data to your Atlas backend API when online.

## Features

- 🎯 **RFID Card Reading**: Supports RC522 RFID readers
- 📡 **Offline Operation**: Continues logging when backend is unreachable
- 🔄 **Automatic Sync**: Periodically syncs offline data to backend
- 🌐 **Web Portal**: Monitor system status and records via web interface
- 🔒 **Reliable Storage**: Local JSON-based storage with file locking
- 📊 **Real-time Dashboard**: View attendance records and system status
- 🚀 **Systemd Integration**: Runs as system services with auto-restart

## Hardware Requirements

- Raspberry Pi (3/4/5 or Zero W recommended)
- RC522 RFID Reader Module
- MicroSD Card (8GB minimum, 16GB+ recommended)
- Power Supply (5V, 2.5A minimum)
- RFID Cards/Tags (13.56MHz)

## RFID Wiring (RC522 to Raspberry Pi)

```
RC522 Pin    →    Raspberry Pi Pin
SDA (SS)     →    GPIO 8 (Pin 24)
SCK          →    GPIO 11 (Pin 23)
MOSI         →    GPIO 10 (Pin 19)
MISO         →    GPIO 9 (Pin 21)
IRQ          →    Not connected
GND          →    Ground (Pin 6)
RST          →    GPIO 25 (Pin 22)
3.3V         →    3.3V (Pin 1)
```

## Software Architecture

```
┌─────────────────────────────────────────┐
│           Atlas Edge Device              │
│                                          │
│  ┌────────────┐      ┌───────────────┐ │
│  │   RFID     │─────▶│  Attendance   │ │
│  │   Reader   │      │   Service     │ │
│  └────────────┘      └───────┬───────┘ │
│                              │          │
│                      ┌───────▼───────┐  │
│                      │   Offline     │  │
│                      │   Storage     │  │
│                      └───────┬───────┘  │
│                              │          │
│                      ┌───────▼───────┐  │
│                      │   API Sync    │  │
│                      └───────┬───────┘  │
│                              │          │
│  ┌────────────┐              │          │
│  │    Web     │──────────────┘          │
│  │   Portal   │                         │
│  └────────────┘                         │
└──────────────┬──────────────────────────┘
               │
               │ HTTP/HTTPS
               │
       ┌───────▼────────┐
       │ Atlas Backend  │
       │      API       │
       └────────────────┘
```

## Installation

### 1. Prepare Raspberry Pi

```bash
# Update system
sudo apt-get update
sudo apt-get upgrade -y

# Enable SPI interface
sudo raspi-config
# Navigate to: Interface Options → SPI → Enable
```

### 2. Transfer Project Files

Transfer all project files to your Raspberry Pi:

```bash
# On your computer (using scp)
scp -r atlas-edge/ pi@<raspberry-pi-ip>:~/

# Or clone from repository if available
git clone <your-repo-url> /home/pi/atlas-edge
```

### 3. Run Installation Script

```bash
cd /home/pi/atlas-edge
chmod +x install.sh
./install.sh
```

The script will:
- Install dependencies
- Configure the system
- Set up systemd services
- Start the services

### 4. Manual Configuration (Alternative)

If you prefer manual setup:

```bash
# Install Python dependencies
pip3 install -r requirements.txt

# Edit configuration
nano config/config.json

# Run services manually
python3 attendance_service.py &
python3 web/app.py &
```

## Configuration

Edit `config/config.json`:

```json
{
  "device": {
    "name": "Atlas Edge",
    "id": "atlas-edge-001",
    "location": "Main Entrance"
  },
  "rfid": {
    "reader_type": "RC522",
    "spi_bus": 0,
    "spi_device": 0,
    "pin_rst": 25
  },
  "server": {
    "api_url": "https://your-atlas-backend.com/api",
    "api_key": "your-api-key-here",
    "sync_interval": 300,
    "timeout": 10
  },
  "storage": {
    "offline_log": "/home/pi/atlas-edge/data/offline_attendance.json",
    "max_offline_records": 10000
  },
  "web": {
    "port": 8080,
    "host": "0.0.0.0"
  }
}
```

### Configuration Options

- **device.id**: Unique identifier for this device
- **device.name**: Human-readable device name
- **device.location**: Physical location of the device
- **server.api_url**: Atlas backend API endpoint
- **server.api_key**: API authentication key
- **server.sync_interval**: Seconds between sync attempts (default: 300)
- **web.port**: Web portal port (default: 8080)

## Usage

### Access Web Portal

Open browser and navigate to:
```
http://<raspberry-pi-ip>:8080
```

### Service Management

```bash
# View logs
sudo journalctl -u atlas-edge-attendance.service -f
sudo journalctl -u atlas-edge-web.service -f

# Restart services
sudo systemctl restart atlas-edge-attendance.service
sudo systemctl restart atlas-edge-web.service

# Stop services
sudo systemctl stop atlas-edge-attendance.service
sudo systemctl stop atlas-edge-web.service

# Check status
sudo systemctl status atlas-edge-attendance.service
sudo systemctl status atlas-edge-web.service
```

### Manual Testing

Test individual components:

```bash
# Test RFID reader
cd /home/pi/atlas-edge
python3 services/rfid_reader.py

# Test API sync
python3 services/api_sync.py

# Test storage
python3 services/offline_storage.py
```

## API Endpoints

The web portal exposes these REST endpoints:

### Status
- `GET /api/status` - System status and stats
- `GET /api/health` - Health check

### Records
- `GET /api/records` - Get all attendance records
- `GET /api/records/unsynced` - Get unsynced records

### Sync
- `POST /api/sync/trigger` - Manually trigger sync

### Configuration
- `GET /api/config` - Get configuration (sensitive data hidden)
- `POST /api/config` - Update configuration

### Storage
- `POST /api/storage/clear` - Clear synced records

### Device
- `POST /api/device/register` - Register device with backend

## Atlas Backend API Requirements

Your backend API should implement these endpoints:

### Required Endpoints

```
POST /api/attendance
POST /api/attendance/batch
POST /api/devices/register
POST /api/devices/heartbeat
GET  /api/devices/{device_id}
GET  /api/health
```

### Attendance Record Format

```json
{
  "card_id": "123456789",
  "timestamp": "2024-12-31T10:30:00Z",
  "device_id": "atlas-edge-001",
  "device_name": "Atlas Edge",
  "location": "Main Entrance",
  "synced": false
}
```

### Batch Sync Format

```json
{
  "records": [
    {
      "card_id": "123456789",
      "timestamp": "2024-12-31T10:30:00Z",
      ...
    }
  ]
}
```

## Troubleshooting

### RFID Reader Not Working

1. Check wiring connections
2. Verify SPI is enabled: `lsmod | grep spi`
3. Test with simple read script
4. Check logs: `sudo journalctl -u atlas-edge-attendance.service -f`

### Backend Connection Issues

1. Verify API URL and key in config
2. Test network connectivity: `ping your-backend.com`
3. Check API endpoint: `curl https://your-backend.com/api/health`
4. Review sync logs

### Web Portal Not Accessible

1. Check service status: `sudo systemctl status atlas-edge-web.service`
2. Verify port 8080 is not blocked by firewall
3. Check if service is bound: `netstat -tlnp | grep 8080`

### Storage Issues

1. Check disk space: `df -h`
2. Verify write permissions: `ls -la /home/pi/atlas-edge/data/`
3. Check log file: `cat logs/storage.log`

## Remote Access (SSH Setup)

### Enable SSH on Raspberry Pi

```bash
sudo systemctl enable ssh
sudo systemctl start ssh
```

### Connect from Remote Machine

```bash
ssh pi@<raspberry-pi-ip>
```

### Setup SSH Keys (Recommended)

```bash
# On your computer
ssh-keygen -t rsa -b 4096
ssh-copy-id pi@<raspberry-pi-ip>
```

### Port Forwarding for Remote Web Access

Forward local port to Pi's web portal:

```bash
ssh -L 8080:localhost:8080 pi@<raspberry-pi-ip>
```

Then access: `http://localhost:8080`

## Security Considerations

1. **Change Default Password**: Always change the default Pi password
2. **API Key Security**: Keep your API key secure, never commit to version control
3. **Firewall**: Consider using UFW to restrict access
4. **HTTPS**: Use HTTPS for backend API communication
5. **SSH Keys**: Use SSH keys instead of passwords
6. **Regular Updates**: Keep system and packages updated

## File Structure

```
atlas-edge/
├── config/
│   └── config.json              # Configuration file
├── services/
│   ├── rfid_reader.py          # RFID reading service
│   ├── offline_storage.py      # Local storage manager
│   └── api_sync.py             # Backend sync service
├── web/
│   ├── app.py                  # Flask web application
│   ├── templates/
│   │   └── dashboard.html      # Web dashboard
│   └── static/                 # Static assets
├── logs/                       # Log files
├── data/
│   └── offline_attendance.json # Offline storage
├── attendance_service.py       # Main service orchestrator
├── requirements.txt            # Python dependencies
├── install.sh                  # Installation script
├── atlas-edge-attendance.service  # Systemd service
├── atlas-edge-web.service      # Systemd service
└── README.md                   # This file
```

## Performance

- **RFID Read Rate**: ~1 read per second
- **Storage Capacity**: 10,000 records (configurable)
- **Sync Batch Size**: 50 records per batch
- **Memory Usage**: ~50-100MB
- **CPU Usage**: <5% idle, <20% during sync

## Future Enhancements

- [ ] LCD display for local feedback
- [ ] Buzzer for audio feedback
- [ ] Multiple RFID reader support
- [ ] Real-time WebSocket updates
- [ ] Offline web portal mode
- [ ] Database backend (SQLite)
- [ ] Encrypted storage
- [ ] VPN integration for secure remote access

## License

[Your License Here]

## Support

For issues and questions:
- Check logs: `/home/pi/atlas-edge/logs/`
- Review documentation
- Contact support: [your-support-email]

## Contributing

Contributions welcome! Please submit pull requests or open issues.

---

**Atlas Edge** - Reliable Edge Computing for Attendance Tracking
