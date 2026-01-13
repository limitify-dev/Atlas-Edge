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

        # Sync configuration (from sync section, fallback to server section for backwards compat)
        sync_config = self.config.get('sync', {})
        self.sync_interval = sync_config.get('sync_interval', self.config.get('server', {}).get('sync_interval', 300))
        self.batch_size = sync_config.get('batch_size', 50)
        self.min_records_for_sync = sync_config.get('min_records_for_sync', 1)
        self.immediate_sync = sync_config.get('immediate_sync', False)
        self.cleanup_after_sync = self.config.get('storage', {}).get('cleanup_after_sync', True)
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
        """
        Handle new attendance record from RFID reader.

        Batch-first approach:
        1. Always save to local storage first (offline-first)
        2. If immediate_sync is enabled and backend is online, sync right away
        3. Otherwise, records will be synced in batches at the next sync interval
        """
        self.logger.info("=" * 60)
        self.logger.info(f"ATTENDANCE LOGGED")
        self.logger.info(f"Card ID: {record['card_id']}")
        self.logger.info(f"Time: {datetime.fromisoformat(record['timestamp']).strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Location: {record['location']}")
        self.logger.info("=" * 60)

        # Always save to offline storage first (batch-first approach)
        self.storage.add_record(record)
        self.logger.info("Saved to local storage")

        # Check if immediate sync is enabled
        if self.immediate_sync:
            # Try to send to backend immediately if online
            if self.api_sync.check_connection():
                success = self.api_sync.send_attendance(record)
                if success:
                    self.storage.mark_as_synced([record['timestamp']])
                    self.logger.info("Synced to backend immediately")
                else:
                    self.logger.warning("Failed to sync immediately, will retry in batch")
            else:
                self.logger.info("Backend offline, will sync in batch later")
        else:
            # Batch mode: show pending count
            stats = self.storage.get_stats()
            self.logger.info(f"Pending sync: {stats['unsynced_records']} records (next sync in ~{self.sync_interval}s)")

        self.logger.info("")  # Empty line for readability
    
    def sync_offline_records(self):
        """
        Sync pending offline records to backend in chunks.

        This method:
        1. Checks if backend is online
        2. Gets all unsynced records
        3. Sends them in configurable chunk sizes
        4. Marks successful records as synced
        5. Optionally cleans up synced records
        """
        if not self.api_sync.check_connection():
            self.logger.debug("Backend offline, skipping sync")
            return {'synced': 0, 'failed': 0, 'skipped': True}

        unsynced = self.storage.get_unsynced_records()
        if not unsynced:
            self.logger.debug("No unsynced records to sync")
            return {'synced': 0, 'failed': 0, 'skipped': False}

        # Check minimum records threshold
        if len(unsynced) < self.min_records_for_sync:
            self.logger.debug(f"Only {len(unsynced)} records, minimum is {self.min_records_for_sync}")
            return {'synced': 0, 'failed': 0, 'skipped': True}

        self.logger.info(f"Syncing {len(unsynced)} offline records in chunks of {self.batch_size}...")

        # Use chunk-based sync from api_sync
        result = self.api_sync.sync_records_in_chunks(unsynced, self.batch_size)

        # Mark successful records as synced
        if result['synced_ids']:
            self.storage.mark_as_synced(result['synced_ids'])
            self.logger.info(f"Marked {len(result['synced_ids'])} records as synced")

        # Log any errors
        if result['errors']:
            for error in result['errors'][:5]:  # Show first 5 errors
                self.logger.warning(f"Sync error for {error.get('card_id', 'unknown')}: {error.get('error', 'Unknown')}")
            if len(result['errors']) > 5:
                self.logger.warning(f"...and {len(result['errors']) - 5} more errors")

        # Update last sync time
        self.last_sync = datetime.now()

        # Clean up synced records if enabled
        if self.cleanup_after_sync and result['success'] > 0:
            removed = self.storage.clear_synced_records()
            if removed > 0:
                self.logger.info(f"Cleared {removed} synced records from local storage")

        self.logger.info(f"Sync complete: {result['success']} synced, {result['failed']} failed")

        return {
            'synced': result['success'],
            'failed': result['failed'],
            'skipped': False,
            'errors': result.get('errors', [])
        }
    
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
        self.logger.info("Periodic sync thread started")
        self.logger.info(f"   Mode: {'Immediate + Batch' if self.immediate_sync else 'Batch only'}")
        self.logger.info(f"   Sync interval: {self.sync_interval} seconds")
        self.logger.info(f"   Batch size: {self.batch_size} records per chunk")
        self.logger.info(f"   Min records to sync: {self.min_records_for_sync}")
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
        """Get service status including sync configuration"""
        stats = self.storage.get_stats()
        return {
            'device': self.config['device'],
            'backend_online': self.api_sync.is_online,
            'last_backend_check': self.api_sync.last_check.isoformat() if self.api_sync.last_check else None,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None,
            'storage': stats,
            'sync_config': {
                'mode': 'immediate' if self.immediate_sync else 'batch',
                'batch_size': self.batch_size,
                'sync_interval': self.sync_interval,
                'min_records_for_sync': self.min_records_for_sync,
                'cleanup_after_sync': self.cleanup_after_sync
            },
            'service_running': self.running
        }

    def force_sync(self):
        """Force an immediate sync of all pending records"""
        self.logger.info("Force sync requested...")
        return self.sync_offline_records()

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