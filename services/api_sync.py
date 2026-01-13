"""
API Sync Service for Atlas Edge
Handles communication with Atlas backend API
"""

import json
import logging
import requests
import time
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

class APISync:
    def __init__(self, config_path='config/config.json'):
        self.config = self._load_config(config_path)
        self.api_url = self.config['server']['api_url']
        self.api_key = self.config['server']['api_key']
        self.timeout = self.config['server']['timeout']
        self.is_online = False
        self.last_check = None
        self.setup_logging()
    
    def _load_config(self, config_path):
        """Load configuration from JSON file"""
        with open(config_path, 'r') as f:
            return json.load(f)
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('logs/api_sync.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger('APISync')
    
    def _get_headers(self):
        """Get API request headers"""
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'X-Device-Id': self.config['device']['id']
        }
    
    def check_connection(self) -> bool:
        """Check if backend API is reachable"""
        try:
            response = requests.get(
                f"{self.api_url}/",
                headers=self._get_headers(),
                timeout=self.timeout
            )
            self.is_online = response.status_code == 200
            self.last_check = datetime.utcnow()
            
            if self.is_online:
                self.logger.info("Backend API is reachable")
            else:
                self.logger.warning(f"Backend API returned status {response.status_code}")
            
            return self.is_online
        except requests.exceptions.RequestException as e:
            self.is_online = False
            self.last_check = datetime.utcnow()
            self.logger.warning(f"Backend API unreachable: {e}")
            return False
    
    def send_attendance(self, record: Dict) -> bool:
        """Send single attendance record to backend"""
        try:
            response = requests.post(
                f"{self.api_url}/attendance",
                json=record,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            
            if response.status_code in [200, 201]:
                self.logger.info(f"Attendance synced successfully: {record['card_id']}")
                return True
            else:
                self.logger.error(f"Failed to sync attendance. Status: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error syncing attendance: {e}")
            return False
    
    def send_batch_attendance(self, records: List[Dict]) -> Dict:
        """Send multiple attendance records to backend"""
        if not records:
            return {'success': 0, 'failed': 0, 'synced_ids': []}
        
        try:
            response = requests.post(
                f"{self.api_url}/attendance/batch",
                json={'records': records},
                headers=self._get_headers(),
                timeout=self.timeout * 2  # Longer timeout for batch
            )
            
            if response.status_code in [200, 201]:
                result = response.json()
                synced_ids = [r['timestamp'] for r in records]
                self.logger.info(f"Batch sync successful: {len(records)} records")
                return {
                    'success': len(records),
                    'failed': 0,
                    'synced_ids': synced_ids
                }
            else:
                self.logger.error(f"Batch sync failed. Status: {response.status_code}")
                return {'success': 0, 'failed': len(records), 'synced_ids': []}
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error in batch sync: {e}")
            return {'success': 0, 'failed': len(records), 'synced_ids': []}
    
    def register_device(self) -> bool:
        """Register this edge device with the backend"""
        device_info = {
            'device_id': self.config['device']['id'],
            'device_name': self.config['device']['name'],
            'location': self.config['device']['location'],
            'registered_at': datetime.utcnow().isoformat()
        }
        
        try:
            response = requests.post(
                f"{self.api_url}/devices/register",
                json=device_info,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            
            if response.status_code in [200, 201]:
                self.logger.info("Device registered successfully")
                return True
            else:
                self.logger.error(f"Device registration failed. Status: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error registering device: {e}")
            return False
    
    def get_device_info(self) -> Optional[Dict]:
        """Get device information from backend"""
        try:
            response = requests.get(
                f"{self.api_url}/devices/{self.config['device']['id']}",
                headers=self._get_headers(),
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"Failed to get device info. Status: {response.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error getting device info: {e}")
            return None
    
    def heartbeat(self) -> bool:
        """Send heartbeat to backend"""
        heartbeat_data = {
            'device_id': self.config['device']['id'],
            'timestamp': datetime.utcnow().isoformat(),
            'status': 'online'
        }
        
        try:
            response = requests.post(
                f"{self.api_url}/devices/heartbeat",
                json=heartbeat_data,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

if __name__ == "__main__":
    # Test the API sync
    api = APISync()
    
    if api.check_connection():
        print("✓ Backend is reachable")
        
        # Test attendance record
        test_record = {
            'card_id': '123456789',
            'timestamp': datetime.utcnow().isoformat(),
            'device_id': api.config['device']['id']
        }
        
        if api.send_attendance(test_record):
            print("✓ Test attendance sent successfully")
    else:
        print("✗ Backend is unreachable")
