#!/usr/bin/env python3
"""
USB RFID Reader Service for Atlas Edge
Handles USB RFID card reading and attendance logging
USB RFID readers act as HID keyboard devices
"""

import json
import time
import logging
import sys
from datetime import datetime
from pathlib import Path
import select

class USBRFIDReader:
    def __init__(self, config_path='config/config.json', device_path=None):
        self.config = self._load_config(config_path)
        self.device_path = device_path or self.config.get('rfid', {}).get('device_path', '/dev/hidraw0')
        self.setup_logging()
        self.last_card_id = None
        self.last_read_time = 0
        self.debounce_time = 2  # Seconds to prevent duplicate reads
        
    def _load_config(self, config_path):
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            return {'device': {'id': 'unknown', 'name': 'Unknown', 'location': 'Unknown'}}
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger('USBRFIDReader')
    
    def find_rfid_device(self):
        """Auto-detect USB RFID reader device"""
        import os
        import glob
        
        # Try common paths
        possible_paths = [
            '/dev/hidraw0',
            '/dev/hidraw1',
            '/dev/hidraw2',
            '/dev/input/by-id/*RFID*',
            '/dev/input/by-id/*Reader*',
        ]
        
        for pattern in possible_paths:
            devices = glob.glob(pattern)
            if devices:
                self.logger.info(f"Found potential RFID device: {devices[0]}")
                return devices[0]
        
        self.logger.warning("No RFID device auto-detected, using stdin")
        return None
    
    def read_card_stdin(self):
        """Read card ID from standard input (keyboard emulation)"""
        try:
            self.logger.info("Waiting for RFID card scan...")
            
            # Check if data is available (with timeout)
            if select.select([sys.stdin], [], [], 0.1)[0]:
                card_id = sys.stdin.readline().strip()
                if card_id:
                    return card_id
            return None
            
        except Exception as e:
            self.logger.error(f"Error reading from stdin: {e}")
            return None
    
    def read_card_hidraw(self):
        """Read card ID from HID raw device"""
        try:
            with open(self.device_path, 'rb') as device:
                self.logger.info(f"Reading from {self.device_path}...")
                
                card_data = []
                start_time = time.time()
                timeout = 5
                
                while time.time() - start_time < timeout:
                    try:
                        # Read raw HID data
                        data = device.read(8)
                        if data:
                            # Parse HID data (this depends on your specific reader)
                            # Most readers send ASCII codes
                            for byte in data:
                                if 4 <= byte <= 39:  # HID keyboard codes for numbers/letters
                                    char = self._hid_to_char(byte)
                                    if char:
                                        card_data.append(char)
                                elif byte == 40:  # Enter key
                                    if card_data:
                                        return ''.join(card_data)
                                    card_data = []
                    except IOError:
                        time.sleep(0.01)
                
                return None
                
        except PermissionError:
            self.logger.error(f"Permission denied: {self.device_path}. Run with sudo or add user to input group.")
            return None
        except FileNotFoundError:
            self.logger.error(f"Device not found: {self.device_path}")
            return None
        except Exception as e:
            self.logger.error(f"Error reading card: {e}")
            return None
    
    def _hid_to_char(self, hid_code):
        """Convert HID keyboard code to character"""
        # HID keyboard codes to characters mapping
        hid_map = {
            4: 'a', 5: 'b', 6: 'c', 7: 'd', 8: 'e', 9: 'f', 10: 'g',
            11: 'h', 12: 'i', 13: 'j', 14: 'k', 15: 'l', 16: 'm',
            17: 'n', 18: 'o', 19: 'p', 20: 'q', 21: 'r', 22: 's',
            23: 't', 24: 'u', 25: 'v', 26: 'w', 27: 'x', 28: 'y', 29: 'z',
            30: '1', 31: '2', 32: '3', 33: '4', 34: '5',
            35: '6', 36: '7', 37: '8', 38: '9', 39: '0'
        }
        return hid_map.get(hid_code, '')
    
    def read_card(self):
        """Read RFID card and return card ID"""
        card_id = None
        
        # Try reading from device or stdin
        if self.device_path and self.device_path.startswith('/dev/'):
            card_id = self.read_card_hidraw()
        else:
            card_id = self.read_card_stdin()
        
        # Debounce - ignore duplicate reads
        if card_id:
            current_time = time.time()
            if card_id == self.last_card_id and (current_time - self.last_read_time) < self.debounce_time:
                self.logger.debug(f"Ignoring duplicate read: {card_id}")
                return None
            
            self.last_card_id = card_id
            self.last_read_time = current_time
            self.logger.info(f"Card detected: {card_id}")
            
        return card_id
    
    def create_attendance_record(self, card_id):
        """Create attendance record from card ID"""
        record = {
            'card_id': str(card_id),
            'timestamp': datetime.utcnow().isoformat(),
            'device_id': self.config['device']['id'],
            'device_name': self.config['device']['name'],
            'location': self.config['device']['location'],
            'synced': False
        }
        return record
    
    def start_reading(self, callback):
        """Start continuous card reading with callback"""
        self.logger.info("=" * 60)
        self.logger.info("Starting USB RFID Reader Service")
        self.logger.info(f"Device: {self.config['device']['name']}")
        self.logger.info(f"Reading from: {self.device_path or 'stdin'}")
        self.logger.info("Ready to scan cards...")
        self.logger.info("=" * 60)
        
        try:
            while True:
                card_id = self.read_card()
                if card_id:
                    record = self.create_attendance_record(card_id)
                    callback(record)
                time.sleep(0.1)  # Small delay to prevent CPU spinning
                
        except KeyboardInterrupt:
            self.logger.info("Stopping RFID reader service...")
            self.cleanup()
    
    def cleanup(self):
        """Cleanup resources"""
        self.logger.info("Cleanup completed")

# Backwards compatibility alias
RFIDReader = USBRFIDReader

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='USB RFID Reader Test')
    parser.add_argument('--device', type=str, help='Device path (e.g., /dev/hidraw0)', default=None)
    args = parser.parse_args()
    
    reader = USBRFIDReader(device_path=args.device)
    
    def test_callback(record):
        print("\n" + "=" * 60)
        print("ATTENDANCE RECORDED:")
        print(json.dumps(record, indent=2))
        print("=" * 60 + "\n")
    
    try:
        reader.start_reading(test_callback)
    except KeyboardInterrupt:
        print("\nExiting...")