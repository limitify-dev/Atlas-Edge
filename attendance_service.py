#!#!/usr/bin/env python3
"""
Main Attendance Service for Atlas Edge
Orchestrates RFID reading, storage, and API synchronization
"""

import json
import logging
import time
import threading
from datetime import datetime
from pathlib import Path
import signal
import sys
import os

# Import our custom services
from services.rfid_reader import USBRFIDReader
from services.offline_storage import OfflineStorage
from services.api_sync import APISync

class AttendanceService:
    def __init__(self, config_path='config/config.json'):
        self.config = self._load_config(config_path)
        self.running = False
        self.setup_logging()
        
        # Initialize services
        self.logger.info("Initializing Atlas Edge services...")
        
        # Get RFID device path from config
        rfid_config = self.config.get('rfid', {})
        device_path = rfid_config.get('device_path', None)
        
        self.rfid_reader = USBRFIDReader(config_path, device_path=device_path)
        self.storage = OfflineStorage(config_path)
        self.api_sync = APISync(config_path)
        
        # Sync configuration
        self.sync_interval = self.config['server']['sync_interval']
        self.last_sync = None
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _load_config(self, config_path):
        """Load configuration from JSON file"""
        with open(config_path, 'r') as f:
            return json.load(f)
    
    def setup_logging(self):
        """Setup logging configuration"""
        Path('logs').mkdir(exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger('AttendanceService')
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)
    
    def handle_attendance(self, record):
        """Handle new attendance record from RFID reader"""
        self.logger.info("=" * 60)
        self.logger.info(f"📋 ATTENDANCE LOGGED")
        self.logger.info(f"Card ID: {record['card_id']}")
        self.logger.info(f"Time: {datetime.fromisoformat(record['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Location: {record['location']}")
        self.logger.info("=" * 60)
        
        # Always save to offline storage first
        self.storage.add_record(record)
        
        # Try to send to backend if online
        if self.api_sync.check_connection():
            success = self.api_sync.send_attendance(record)
            if success:
                # Mark as synced
                self.storage.mark_as_synced([record['timestamp']])
                self.logger.info("✓ Synced to backend immediately")
            else:
                self.logger.warning("⚠ Failed to sync immediately, will retry later")
        else:
            self.logger.info("💾 Backend offline, saved locally")
        
        self.logger.info("")  # Empty line for readability
    
    def sync_offline_records(self):
        """Sync any pending offline records to backend"""
        if not self.api_sync.check_connection():
            self.logger.debug("Backend offline, skipping sync")
            return
        
        unsynced = self.storage.get_unsynced_records()
        if not unsynced:
            self.logger.debug("No unsynced records to sync")
            return
        
        self.logger.info(f"🔄 Syncing {len(unsynced)} offline records...")
        
        # Send in batches of 50
        batch_size = 50
        total_synced = 0
        
        for i in range(0, len(unsynced), batch_size):
            batch = unsynced[i:i+batch_size]
            result = self.api_sync.send_batch_attendance(batch)
            
            if result['synced_ids']:
                self.storage.mark_as_synced(result['synced_ids'])
                total_synced += result['success']
                self.logger.info(f"✓ Synced batch of {result['success']} records")
            
            time.sleep(1)  # Small delay between batches
        
        if total_synced > 0:
            self.logger.info(f"✓ Total synced: {total_synced} records")
            # Clean up old synced records
            removed = self.storage.clear_synced_records()
            if removed > 0:
                self.logger.info(f"🗑️  Cleared {removed} old synced records")
    
    def periodic_sync(self):
        """Periodically sync offline records"""
        while self.running:
            try:
                time.sleep(self.sync_interval)
                self.logger.info("Running periodic sync...")
                self.sync_offline_records()
                
                # Send heartbeat
                if self.api_sync.check_connection():
                    self.api_sync.heartbeat()
                    
            except Exception as e:
                self.logger.error(f"Error in periodic sync: {e}")
    
    def start(self):
        """Start the attendance service"""
        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("🚀 ATLAS EDGE ATTENDANCE SERVICE")
        self.logger.info("=" * 60)
        self.logger.info(f"Device ID: {self.config['device']['id']}")
        self.logger.info(f"Device Name: {self.config['device']['name']}")
        self.logger.info(f"Location: {self.config['device']['location']}")
        
        # Display RFID reader info
        rfid_config = self.config.get('rfid', {})
        reader_type = rfid_config.get('reader_type', 'USB')
        device_path = rfid_config.get('device_path', 'stdin')
        self.logger.info(f"Reader Type: {reader_type}")
        self.logger.info(f"Reader Device: {device_path}")
        
        self.logger.info("=" * 60)
        self.logger.info("")
        
        self.running = True
        
        # Check backend connection on startup
        self.logger.info("🔍 Checking backend connection...")
        if self.api_sync.check_connection():
            self.logger.info("✓ Backend connection established")
            self.logger.info("📡 Registering device...")
            self.api_sync.register_device()
        else:
            self.logger.warning("⚠ Backend unreachable, operating in OFFLINE mode")
        
        # Display storage stats
        stats = self.storage.get_stats()
        self.logger.info("")
        self.logger.info("📊 Storage Status:")
        self.logger.info(f"   Total Records: {stats['total_records']}")
        self.logger.info(f"   Synced: {stats['synced_records']}")
        self.logger.info(f"   Pending: {stats['unsynced_records']}")
        self.logger.info("")
        
        # Start periodic sync in background thread
        sync_thread = threading.Thread(target=self.periodic_sync, daemon=True)
        sync_thread.start()
        self.logger.info("✓ Periodic sync thread started")
        self.logger.info(f"   Sync interval: {self.sync_interval} seconds")
        self.logger.info("")
        
        # Start RFID reading (blocking call)
        self.logger.info("=" * 60)
        self.logger.info("👉 READY TO SCAN CARDS")
        self.logger.info("=" * 60)
        self.logger.info("")
        
        try:
            self.rfid_reader.start_reading(self.handle_attendance)
        except Exception as e:
            self.logger.error(f"❌ Error in RFID reader: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            self.stop()
    
    def stop(self):
        """Stop the attendance service"""
        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("🛑 Stopping Atlas Edge Attendance Service...")
        self.logger.info("=" * 60)
        self.running = False
        
        # Final sync before shutdown
        try:
            self.logger.info("📤 Performing final sync...")
            self.sync_offline_records()
        except Exception as e:
            self.logger.error(f"Error during final sync: {e}")
        
        # Cleanup
        self.rfid_reader.cleanup()
        self.logger.info("✓ Service stopped successfully")
        self.logger.info("")
    
    def get_status(self):
        """Get service status"""
        stats = self.storage.get_stats()
        return {
            'device': self.config['device'],
            'backend_online': self.api_sync.is_online,
            'last_backend_check': self.api_sync.last_check.isoformat() if self.api_sync.last_check else None,
            'storage': stats,
            'service_running': self.running
        }

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Atlas Edge Attendance Service')
    parser.add_argument('--config', type=str, default='config/config.json', 
                       help='Path to configuration file')
    parser.add_argument('--device', type=str, help='Override RFID device path')
    args = parser.parse_args()
    
    # Override device path if specified
    if args.device:
        import json
        with open(args.config, 'r') as f:
            config = json.load(f)
        if 'rfid' not in config:
            config['rfid'] = {}
        config['rfid']['device_path'] = args.device
        with open(args.config, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"Updated device path to: {args.device}")
    
    service = AttendanceService(args.config)
    service.start()