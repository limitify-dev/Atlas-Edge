# Atlas Edge - Deployment Guide

## Production Deployment Checklist

### Pre-Deployment

- [ ] Raspberry Pi imaged with latest Raspberry Pi OS
- [ ] RFID reader properly wired and tested
- [ ] Network connectivity configured (WiFi/Ethernet)
- [ ] Static IP or DHCP reservation configured
- [ ] Atlas backend API running and accessible
- [ ] API credentials generated

### Hardware Setup

1. **Raspberry Pi Configuration**
   ```bash
   sudo raspi-config
   ```
   - Set hostname (e.g., `atlas-edge-001`)
   - Enable SPI interface
   - Set timezone
   - Configure WiFi (if using wireless)
   - Expand filesystem
   - Change default password

2. **RFID Reader Connection**
   - Verify wiring with multimeter
   - Test continuity of connections
   - Ensure proper voltage (3.3V, not 5V!)

### Software Deployment

1. **Transfer Files**
   ```bash
   # From development machine
   rsync -avz --exclude='*.pyc' --exclude='__pycache__' \
     atlas-edge/ pi@<pi-ip>:/home/pi/atlas-edge/
   ```

2. **Run Installation**
   ```bash
   ssh pi@<pi-ip>
   cd /home/pi/atlas-edge
   ./install.sh
   ```

3. **Configure Backend Connection**
   ```bash
   nano /home/pi/atlas-edge/config/config.json
   ```
   Update:
   - `server.api_url`
   - `server.api_key`
   - `device.id`
   - `device.name`
   - `device.location`

4. **Test Services**
   ```bash
   # Test RFID reader
   python3 /home/pi/atlas-edge/services/rfid_reader.py
   
   # Test API connection
   python3 /home/pi/atlas-edge/services/api_sync.py
   
   # Test storage
   python3 /home/pi/atlas-edge/services/offline_storage.py
   ```

5. **Start Services**
   ```bash
   sudo systemctl start atlas-edge-attendance.service
   sudo systemctl start atlas-edge-web.service
   
   # Verify they're running
   sudo systemctl status atlas-edge-*
   ```

### Security Hardening

1. **Change Default Credentials**
   ```bash
   passwd  # Change pi user password
   ```

2. **Setup SSH Keys**
   ```bash
   # On your computer
   ssh-copy-id pi@<pi-ip>
   
   # On Raspberry Pi, disable password auth
   sudo nano /etc/ssh/sshd_config
   # Set: PasswordAuthentication no
   sudo systemctl restart ssh
   ```

3. **Configure Firewall**
   ```bash
   sudo apt-get install ufw
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow ssh
   sudo ufw allow 8080/tcp  # Web portal
   sudo ufw enable
   ```

4. **Secure API Key**
   ```bash
   # Restrict config file permissions
   chmod 600 /home/pi/atlas-edge/config/config.json
   ```

### Monitoring Setup

1. **Enable Boot Logging**
   ```bash
   sudo journalctl --vacuum-size=100M
   ```

2. **Setup Log Rotation**
   ```bash
   sudo nano /etc/logrotate.d/atlas-edge
   ```
   
   Add:
   ```
   /home/pi/atlas-edge/logs/*.log {
       daily
       rotate 7
       compress
       delaycompress
       missingok
       notifempty
   }
   ```

3. **Monitor Service Health**
   Create `/home/pi/atlas-edge/monitor.sh`:
   ```bash
   #!/bin/bash
   if ! systemctl is-active --quiet atlas-edge-attendance.service; then
       echo "Attendance service down! Restarting..."
       sudo systemctl restart atlas-edge-attendance.service
   fi
   
   if ! systemctl is-active --quiet atlas-edge-web.service; then
       echo "Web service down! Restarting..."
       sudo systemctl restart atlas-edge-web.service
   fi
   ```
   
   Add to crontab:
   ```bash
   crontab -e
   # Add: */5 * * * * /home/pi/atlas-edge/monitor.sh
   ```

### Network Configuration

1. **Static IP (Recommended for Production)**
   ```bash
   sudo nano /etc/dhcpcd.conf
   ```
   
   Add:
   ```
   interface eth0
   static ip_address=192.168.1.100/24
   static routers=192.168.1.1
   static domain_name_servers=8.8.8.8 8.8.4.4
   ```

2. **Configure Local DNS**
   ```bash
   sudo nano /etc/hosts
   # Add: 192.168.1.100  atlas-edge-001
   ```

### Remote Access Setup

1. **Port Forwarding (if needed)**
   - Forward external port to Pi's SSH (22)
   - Forward external port to web portal (8080)
   - Use non-standard external ports for security

2. **VPN Access (Recommended)**
   - Setup WireGuard or OpenVPN
   - Access Pi through secure tunnel
   - No public port exposure needed

3. **Cloudflare Tunnel (Alternative)**
   ```bash
   # Install cloudflared
   wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
   sudo dpkg -i cloudflared-linux-arm64.deb
   
   # Setup tunnel
   cloudflared tunnel login
   cloudflared tunnel create atlas-edge
   ```

### Backup Strategy

1. **Config Backup**
   ```bash
   # Create backup script
   cat > /home/pi/backup-config.sh << 'EOF'
   #!/bin/bash
   DATE=$(date +%Y%m%d_%H%M%S)
   tar -czf /home/pi/backups/config-$DATE.tar.gz \
     /home/pi/atlas-edge/config/ \
     /home/pi/atlas-edge/data/
   
   # Keep only last 7 backups
   ls -t /home/pi/backups/config-*.tar.gz | tail -n +8 | xargs rm -f
   EOF
   
   chmod +x /home/pi/backup-config.sh
   
   # Run daily
   crontab -e
   # Add: 0 2 * * * /home/pi/backup-config.sh
   ```

2. **Full SD Card Image (Monthly)**
   ```bash
   # On Linux machine with SD card reader
   sudo dd if=/dev/sdX of=atlas-edge-backup.img bs=4M status=progress
   gzip atlas-edge-backup.img
   ```

### Performance Tuning

1. **Disable Unnecessary Services**
   ```bash
   sudo systemctl disable bluetooth
   sudo systemctl disable avahi-daemon
   ```

2. **Optimize Memory**
   ```bash
   # Edit /boot/config.txt
   sudo nano /boot/config.txt
   # Add: gpu_mem=16
   ```

3. **Enable Watchdog**
   ```bash
   sudo modprobe bcm2835_wdt
   echo "bcm2835_wdt" | sudo tee -a /etc/modules
   
   sudo apt-get install watchdog
   sudo systemctl enable watchdog
   ```

### Testing & Validation

1. **Functional Tests**
   - [ ] RFID card detected and logged
   - [ ] Attendance synced to backend
   - [ ] Offline mode works (disconnect network)
   - [ ] Data preserved after reboot
   - [ ] Web portal accessible
   - [ ] All API endpoints responding

2. **Stress Tests**
   ```bash
   # Simulate 100 card reads
   for i in {1..100}; do
     python3 -c "from services.offline_storage import OfflineStorage; \
                 s = OfflineStorage(); \
                 s.add_record({'card_id': '$i', 'timestamp': 'test'})"
     sleep 0.1
   done
   ```

3. **Recovery Tests**
   - [ ] Service auto-restarts on failure
   - [ ] Data preserved after crash
   - [ ] Sync resumes after network recovery

### Production Monitoring

1. **System Metrics**
   ```bash
   # Install monitoring tools
   sudo apt-get install htop iotop
   
   # Check system resources
   htop
   df -h
   free -m
   ```

2. **Service Logs**
   ```bash
   # Real-time logs
   sudo journalctl -u atlas-edge-attendance.service -f
   
   # Last 100 lines
   sudo journalctl -u atlas-edge-attendance.service -n 100
   ```

3. **Web Dashboard**
   - Monitor unsynced count
   - Check backend connectivity status
   - Review recent attendance records

### Maintenance Schedule

**Daily**
- Check service status
- Monitor unsynced records count
- Review error logs

**Weekly**
- Clear old log files
- Verify backend connectivity
- Check disk space usage

**Monthly**
- Update system packages
- Review and clear synced records
- Create full backup image
- Test disaster recovery

**Quarterly**
- Security audit
- Performance review
- Hardware inspection
- Software updates

### Troubleshooting Common Issues

**Issue: High unsynced count**
```bash
# Check network
ping backend-api.com

# Check API credentials
curl -H "Authorization: Bearer YOUR_API_KEY" https://backend-api.com/api/health

# Manually trigger sync
curl -X POST http://localhost:8080/api/sync/trigger
```

**Issue: RFID not reading**
```bash
# Check SPI
lsmod | grep spi

# Test GPIO
gpio readall

# Check wiring
python3 services/rfid_reader.py
```

**Issue: Service crashes**
```bash
# Check logs
sudo journalctl -u atlas-edge-attendance.service --since "1 hour ago"

# Check system resources
free -m
df -h

# Restart service
sudo systemctl restart atlas-edge-attendance.service
```

### Rollback Procedure

If deployment fails:

1. **Stop Services**
   ```bash
   sudo systemctl stop atlas-edge-*
   ```

2. **Restore Previous Version**
   ```bash
   cd /home/pi
   mv atlas-edge atlas-edge-broken
   tar -xzf backups/config-YYYYMMDD.tar.gz
   ```

3. **Restart Services**
   ```bash
   sudo systemctl start atlas-edge-*
   ```

### Post-Deployment

- [ ] Document device IP and location
- [ ] Add device to inventory system
- [ ] Configure monitoring alerts
- [ ] Update network documentation
- [ ] Train operators on web portal
- [ ] Schedule first maintenance check

### Support Contacts

- Technical Support: [email]
- Emergency Contact: [phone]
- Documentation: http://docs.your-domain.com

---

**Remember**: Always test in a staging environment before production deployment!
