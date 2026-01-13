# Atlas Edge - Project Summary

## Overview

Atlas Edge is a complete RFID-based attendance tracking system designed for Raspberry Pi with offline capabilities and cloud synchronization.

## Key Features

✅ **Offline-First Architecture**
- Continues logging when backend is unreachable
- Automatic sync when connection restored
- Local storage with 10,000+ record capacity

✅ **Real-Time Monitoring**
- Web-based dashboard
- Live status updates
- Attendance record viewing

✅ **Robust & Reliable**
- Systemd service integration
- Auto-restart on failure
- File-locked storage
- Comprehensive error handling

✅ **Production Ready**
- Easy installation script
- Systemd service files
- Logging and monitoring
- Security considerations

## Project Structure

```
atlas-edge/
│
├── 📋 Documentation
│   ├── README.md              # Complete documentation
│   ├── QUICKSTART.md          # 5-minute setup guide
│   └── DEPLOYMENT.md          # Production deployment guide
│
├── ⚙️ Configuration
│   └── config/
│       └── config.json        # System configuration
│
├── 🔧 Core Services
│   ├── attendance_service.py  # Main orchestrator
│   └── services/
│       ├── rfid_reader.py     # RFID card reading
│       ├── offline_storage.py # Local data persistence
│       └── api_sync.py        # Backend synchronization
│
├── 🌐 Web Interface
│   └── web/
│       ├── app.py             # Flask API server
│       └── templates/
│           └── dashboard.html # Web dashboard
│
├── 🚀 Deployment
│   ├── install.sh             # Automated installer
│   ├── atlas-edge-attendance.service
│   └── atlas-edge-web.service
│
├── 📦 Dependencies
│   └── requirements.txt       # Python packages
│
├── 📝 Example
│   └── example_backend_api.py # Reference backend API
│
└── 💾 Data
    ├── logs/                  # Service logs
    └── data/                  # Offline storage
```

## Technology Stack

- **Hardware**: Raspberry Pi 3/4/5, RC522 RFID Reader
- **Language**: Python 3.7+
- **Framework**: Flask (web interface)
- **Storage**: JSON-based file storage
- **Services**: Systemd
- **Communication**: REST API (requests library)

## Components Explained

### 1. RFID Reader Service (`services/rfid_reader.py`)
- Interfaces with RC522 RFID module via SPI
- Continuously scans for RFID cards
- Generates attendance records with timestamps
- Provides callback mechanism for handling reads

### 2. Offline Storage (`services/offline_storage.py`)
- JSON-based local storage with file locking
- Tracks sync status for each record
- Configurable capacity (default: 10,000 records)
- Provides statistics and management functions

### 3. API Sync Service (`services/api_sync.py`)
- Handles communication with Atlas backend
- Connection health checking
- Single and batch record synchronization
- Device registration and heartbeat

### 4. Main Attendance Service (`attendance_service.py`)
- Orchestrates all components
- Manages service lifecycle
- Periodic sync scheduling
- Signal handling for graceful shutdown

### 5. Web Portal (`web/app.py`)
- RESTful API endpoints
- Real-time status monitoring
- Record viewing and management
- Manual sync triggering

### 6. Dashboard (`web/templates/dashboard.html`)
- Responsive web interface
- Real-time status cards
- Attendance record table
- Control buttons for management

## Installation Methods

### Method 1: Automated Installation (Recommended)
```bash
./install.sh
```

### Method 2: Manual Setup
```bash
pip3 install -r requirements.txt
python3 attendance_service.py &
python3 web/app.py &
```

### Method 3: Systemd Services
```bash
sudo systemctl start atlas-edge-attendance.service
sudo systemctl start atlas-edge-web.service
```

## Configuration

All settings in `config/config.json`:

- **Device Settings**: ID, name, location
- **RFID Settings**: Reader type, GPIO pins
- **Server Settings**: API URL, key, sync interval
- **Storage Settings**: File path, capacity
- **Web Settings**: Port, host binding

## API Endpoints

### Status & Health
- `GET /api/status` - System status
- `GET /api/health` - Health check

### Records Management
- `GET /api/records` - View records
- `GET /api/records/unsynced` - Unsynced records
- `POST /api/sync/trigger` - Manual sync

### Configuration
- `GET /api/config` - View config
- `POST /api/config` - Update config

### Storage
- `POST /api/storage/clear` - Clear synced records

## Backend Integration

Your Atlas backend must implement:

1. **Health Check**: `GET /api/health`
2. **Attendance Logging**: `POST /api/attendance`
3. **Batch Sync**: `POST /api/attendance/batch`
4. **Device Registration**: `POST /api/devices/register`
5. **Device Info**: `GET /api/devices/{id}`
6. **Heartbeat**: `POST /api/devices/heartbeat`

See `example_backend_api.py` for reference implementation.

## Workflow

```
1. RFID Card Detected
   ↓
2. Generate Attendance Record
   ↓
3. Save to Local Storage
   ↓
4. Check Backend Connection
   ↓
   ├─ Online  → Sync Immediately → Mark as Synced
   │
   └─ Offline → Keep in Queue → Retry on Periodic Sync
```

## Security Features

- API key authentication
- Configurable file permissions
- Service isolation
- SSH key support
- Optional firewall configuration

## Monitoring & Maintenance

**Logs Location**: `/home/pi/atlas-edge/logs/`
- `rfid_reader.log`
- `storage.log`
- `api_sync.log`
- `attendance_service.log`

**Service Logs**: 
```bash
sudo journalctl -u atlas-edge-attendance.service -f
```

**Web Dashboard**: `http://<pi-ip>:8080`

## Performance Characteristics

- **Memory Usage**: ~50-100 MB
- **CPU Usage**: <5% idle, <20% during sync
- **Read Rate**: ~1 card/second
- **Storage**: ~10,000 records (configurable)
- **Sync Rate**: Configurable (default: 5 minutes)

## Use Cases

✅ Office attendance tracking
✅ School/university attendance
✅ Event check-ins
✅ Access control logging
✅ Time tracking
✅ Visitor management

## Limitations

- Single RFID reader per device (can be extended)
- JSON storage (consider SQLite for >50k records)
- No real-time WebSocket updates (polling only)
- No built-in user management (handled by backend)

## Future Enhancements

Potential improvements:
- [ ] SQLite database backend
- [ ] Multiple RFID reader support
- [ ] LCD display integration
- [ ] Audio feedback (buzzer)
- [ ] WebSocket real-time updates
- [ ] Encrypted local storage
- [ ] Built-in VPN client
- [ ] Mobile app companion

## Testing

```bash
# Test components individually
python3 services/rfid_reader.py
python3 services/api_sync.py
python3 services/offline_storage.py

# Test full system
python3 attendance_service.py
```

## Support & Troubleshooting

1. **Check Logs**: Always start with logs
2. **Verify Wiring**: Use `gpio readall`
3. **Test Connectivity**: Ping backend server
4. **Check Services**: `systemctl status atlas-edge-*`

## Development

To extend functionality:

1. Add new services in `services/`
2. Update `attendance_service.py` orchestration
3. Add web endpoints in `web/app.py`
4. Update configuration schema
5. Test thoroughly before deployment

## License

[Your License Here]

## Credits

Developed for reliable edge computing in attendance tracking systems.

---

**Atlas Edge** - Where Edge Computing Meets Reliable Attendance Tracking

For detailed setup instructions, see:
- Quick start: `QUICKSTART.md`
- Full documentation: `README.md`
- Production deployment: `DEPLOYMENT.md`
