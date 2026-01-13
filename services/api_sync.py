"""
API Sync Service for Atlas Edge
Handles communication with Atlas backend API

Updated to work with Atlas backend endpoints:
- /device-api/health - Health check with device auth
- /device-api/heartbeat - Device heartbeat
- /device-api/register - Device self-registration
- /device-api/info - Get device info
- /attendance/auto-checkin - Single attendance record
- /attendance/batch - Batch attendance records

Sync modes:
- batch: Records are stored locally and synced in chunks at intervals (default)
- immediate: Each record is sent immediately (fallback to batch if offline)
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

        # Sync configuration
        sync_config = self.config.get('sync', {})
        self.sync_mode = sync_config.get('mode', 'batch')
        self.batch_size = sync_config.get('batch_size', 50)
        self.sync_interval = sync_config.get('sync_interval', 300)
        self.min_records_for_sync = sync_config.get('min_records_for_sync', 1)
        self.retry_failed_after = sync_config.get('retry_failed_after', 60)
        self.max_retries = sync_config.get('max_retries', 3)
        self.immediate_sync = sync_config.get('immediate_sync', False)

        # State tracking
        self.is_online = False
        self.last_check = None
        self.last_sync = None
        self.failed_records = []
        self.retry_counts = {}

        self.setup_logging()

    def _load_config(self, config_path):
        """Load configuration from JSON file"""
        with open(config_path, 'r') as f:
            return json.load(f)

    def setup_logging(self):
        """Setup logging configuration"""
        # Create logs directory if it doesn't exist
        Path('logs').mkdir(exist_ok=True)

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
        """
        Check if backend API is reachable using device-api health endpoint.
        This endpoint authenticates the device and confirms connectivity.
        """
        try:
            response = requests.get(
                f"{self.api_url}/device-api/health",
                headers=self._get_headers(),
                timeout=self.timeout
            )
            self.is_online = response.status_code == 200
            self.last_check = datetime.utcnow()

            if self.is_online:
                result = response.json()
                self.logger.info(f"Backend API is reachable. Device status: {result.get('device', {}).get('status', 'unknown')}")
            else:
                self.logger.warning(f"Backend API returned status {response.status_code}")

            return self.is_online
        except requests.exceptions.RequestException as e:
            self.is_online = False
            self.last_check = datetime.utcnow()
            self.logger.warning(f"Backend API unreachable: {e}")
            return False

    def send_attendance(self, record: Dict) -> bool:
        """
        Send single attendance record to backend.
        Maps Edge format (card_id, timestamp) to backend format (cardNumber, date).
        """
        try:
            # Map Edge format to backend format
            backend_record = {
                'cardNumber': record['card_id'],
                'date': record['timestamp'],
                'location': record.get('location', self.config['device']['location'])
            }

            response = requests.post(
                f"{self.api_url}/attendance/auto-checkin",
                json=backend_record,
                headers=self._get_headers(),
                timeout=self.timeout
            )

            if response.status_code in [200, 201]:
                result = response.json()
                self.logger.info(f"Attendance synced successfully: {record['card_id']} - {result.get('status', 'unknown')}")
                return True
            else:
                error_msg = "Unknown error"
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', str(response.status_code))
                except:
                    error_msg = f"Status {response.status_code}"
                self.logger.error(f"Failed to sync attendance: {error_msg}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error syncing attendance: {e}")
            return False

    def send_batch_attendance(self, records: List[Dict]) -> Dict:
        """
        Send multiple attendance records to backend.
        Uses the /attendance/batch endpoint for efficient bulk sync.
        """
        if not records:
            return {'success': 0, 'failed': 0, 'synced_ids': [], 'errors': []}

        try:
            response = requests.post(
                f"{self.api_url}/attendance/batch",
                json={'records': records},
                headers=self._get_headers(),
                timeout=self.timeout * 2  # Longer timeout for batch
            )

            if response.status_code in [200, 201]:
                result = response.json()
                # Extract synced timestamps from results
                synced_ids = []
                errors = []

                if 'results' in result:
                    # Get successful records
                    for r in result['results'].get('records', []):
                        if r.get('success', False):
                            synced_ids.append(r.get('timestamp') or r.get('card_id'))

                    # Get failed records with errors
                    for r in result['results'].get('errors', []):
                        errors.append({
                            'card_id': r.get('card_id'),
                            'timestamp': r.get('timestamp'),
                            'error': r.get('error', 'Unknown error')
                        })
                else:
                    # Fallback: assume all synced if successful response
                    synced_ids = [r['timestamp'] for r in records]

                successful = result.get('results', {}).get('successful', len(records))
                failed = result.get('results', {}).get('failed', 0)

                self.logger.info(f"Batch sync: {successful} successful, {failed} failed")
                self.last_sync = datetime.utcnow()

                return {
                    'success': successful,
                    'failed': failed,
                    'synced_ids': synced_ids,
                    'errors': errors
                }
            else:
                error_msg = f"Status {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', error_msg)
                except:
                    pass
                self.logger.error(f"Batch sync failed: {error_msg}")
                return {'success': 0, 'failed': len(records), 'synced_ids': [], 'errors': []}
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error in batch sync: {e}")
            return {'success': 0, 'failed': len(records), 'synced_ids': [], 'errors': []}

    def sync_records_in_chunks(self, records: List[Dict], chunk_size: Optional[int] = None) -> Dict:
        """
        Sync records in chunks to avoid overwhelming the API.
        Returns aggregate results from all chunks.
        """
        if not records:
            return {'success': 0, 'failed': 0, 'synced_ids': [], 'errors': []}

        chunk_size = chunk_size or self.batch_size
        total_success = 0
        total_failed = 0
        all_synced_ids = []
        all_errors = []

        # Process records in chunks
        for i in range(0, len(records), chunk_size):
            chunk = records[i:i + chunk_size]
            chunk_num = (i // chunk_size) + 1
            total_chunks = (len(records) + chunk_size - 1) // chunk_size

            self.logger.info(f"Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} records)")

            result = self.send_batch_attendance(chunk)

            total_success += result['success']
            total_failed += result['failed']
            all_synced_ids.extend(result['synced_ids'])
            all_errors.extend(result.get('errors', []))

            # Small delay between chunks to avoid overwhelming the API
            if i + chunk_size < len(records):
                time.sleep(0.5)

        self.logger.info(f"Chunk sync complete: {total_success} successful, {total_failed} failed")

        return {
            'success': total_success,
            'failed': total_failed,
            'synced_ids': all_synced_ids,
            'errors': all_errors
        }

    def get_sync_config(self) -> Dict:
        """Get current sync configuration"""
        return {
            'mode': self.sync_mode,
            'batch_size': self.batch_size,
            'sync_interval': self.sync_interval,
            'min_records_for_sync': self.min_records_for_sync,
            'immediate_sync': self.immediate_sync,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None,
            'is_online': self.is_online
        }

    def register_device(self) -> bool:
        """
        Register/confirm this edge device with the backend.
        Uses the /device-api/register endpoint with API key auth.
        """
        device_info = {
            'device_id': self.config['device']['id'],
            'device_name': self.config['device']['name'],
            'location': self.config['device']['location'],
            'metadata': {
                'registered_at': datetime.utcnow().isoformat(),
                'software_version': '1.0.0',
                'device_type': 'ATLAS_EDGE'
            }
        }

        try:
            response = requests.post(
                f"{self.api_url}/device-api/register",
                json=device_info,
                headers=self._get_headers(),
                timeout=self.timeout
            )

            if response.status_code in [200, 201]:
                result = response.json()
                self.logger.info(f"Device registered successfully: {result.get('device', {}).get('name', 'unknown')}")
                return True
            else:
                error_msg = f"Status {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', error_msg)
                except:
                    pass
                self.logger.error(f"Device registration failed: {error_msg}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error registering device: {e}")
            return False

    def get_device_info(self) -> Optional[Dict]:
        """Get device information from backend"""
        try:
            response = requests.get(
                f"{self.api_url}/device-api/info",
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
        """
        Send heartbeat to backend to indicate device is online.
        Uses the /device-api/heartbeat endpoint.
        """
        heartbeat_data = {
            'status': 'online',
            'metadata': {
                'timestamp': datetime.utcnow().isoformat(),
                'uptime': self._get_uptime()
            }
        }

        try:
            response = requests.post(
                f"{self.api_url}/device-api/heartbeat",
                json=heartbeat_data,
                headers=self._get_headers(),
                timeout=self.timeout
            )

            if response.status_code == 200:
                self.logger.debug("Heartbeat sent successfully")
                return True
            else:
                self.logger.warning(f"Heartbeat failed. Status: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Heartbeat error: {e}")
            return False

    def _get_uptime(self) -> str:
        """Get system uptime (Linux only)"""
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                return f"{hours}h {minutes}m"
        except:
            return "unknown"

if __name__ == "__main__":
    # Test the API sync
    print("=" * 50)
    print("Atlas Edge API Sync Test")
    print("=" * 50)

    api = APISync()

    print(f"\nAPI URL: {api.api_url}")
    print(f"Device ID: {api.config['device']['id']}")
    print(f"Device Name: {api.config['device']['name']}")

    print("\n--- Testing Connection ---")
    if api.check_connection():
        print("✓ Backend is reachable and device is authenticated")

        print("\n--- Testing Device Registration ---")
        if api.register_device():
            print("✓ Device registration confirmed")
        else:
            print("✗ Device registration failed")

        print("\n--- Testing Device Info ---")
        info = api.get_device_info()
        if info:
            print(f"✓ Device info retrieved: {info.get('device', {}).get('name', 'unknown')}")
        else:
            print("✗ Failed to get device info")

        print("\n--- Testing Heartbeat ---")
        if api.heartbeat():
            print("✓ Heartbeat sent successfully")
        else:
            print("✗ Heartbeat failed")

        print("\n--- Testing Single Attendance ---")
        test_record = {
            'card_id': 'TEST-123456789',
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'device_id': api.config['device']['id'],
            'location': api.config['device']['location']
        }
        print(f"  Sending test record: {test_record['card_id']}")
        if api.send_attendance(test_record):
            print("✓ Test attendance sent successfully")
        else:
            print("✗ Test attendance failed (card may not exist)")

        print("\n--- Testing Batch Attendance ---")
        test_records = [
            {
                'card_id': f'TEST-BATCH-{i}',
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'device_id': api.config['device']['id'],
                'location': api.config['device']['location']
            }
            for i in range(3)
        ]
        print(f"  Sending {len(test_records)} test records")
        result = api.send_batch_attendance(test_records)
        print(f"  Result: {result['success']} successful, {result['failed']} failed")

    else:
        print("✗ Backend is unreachable")
        print("  Check that:")
        print("  1. The backend server is running")
        print("  2. The API URL in config.json is correct")
        print("  3. The API key is valid")
        print("  4. Network connectivity is available")

    print("\n" + "=" * 50)
    print("Test Complete")
    print("=" * 50)
