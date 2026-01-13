"""
Offline Storage Manager for Atlas Edge
Handles local storage when server is unreachable
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import fcntl

class OfflineStorage:
    def __init__(self, config_path='config/config.json'):
        self.config = self._load_config(config_path)
        self.storage_file = Path(self.config['storage']['offline_log'])
        self.max_records = self.config['storage']['max_offline_records']
        self.setup_logging()
        self._ensure_storage_file()
    
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
                logging.FileHandler('logs/storage.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger('OfflineStorage')
    
    def _ensure_storage_file(self):
        """Ensure storage file exists"""
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_file.exists():
            self._write_records([])
    
    def _read_records(self) -> List[Dict]:
        """Read all records from storage file with file locking"""
        try:
            with open(self.storage_file, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return data
        except json.JSONDecodeError:
            self.logger.warning("Corrupted storage file, creating new one")
            return []
        except Exception as e:
            self.logger.error(f"Error reading storage: {e}")
            return []
    
    def _write_records(self, records: List[Dict]):
        """Write records to storage file with file locking"""
        try:
            with open(self.storage_file, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump(records, f, indent=2)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            self.logger.error(f"Error writing storage: {e}")
    
    def add_record(self, record: Dict):
        """Add a new attendance record to offline storage"""
        records = self._read_records()
        
        # Add synced flag if not present
        if 'synced' not in record:
            record['synced'] = False
        
        records.append(record)
        
        # Trim if exceeds max records
        if len(records) > self.max_records:
            self.logger.warning(f"Max records exceeded, removing oldest entries")
            records = records[-self.max_records:]
        
        self._write_records(records)
        self.logger.info(f"Record added to offline storage. Total records: {len(records)}")
    
    def get_unsynced_records(self) -> List[Dict]:
        """Get all records that haven't been synced to server"""
        records = self._read_records()
        unsynced = [r for r in records if not r.get('synced', False)]
        self.logger.info(f"Found {len(unsynced)} unsynced records")
        return unsynced
    
    def mark_as_synced(self, record_ids: List[str]):
        """Mark records as synced by their timestamps"""
        records = self._read_records()
        synced_count = 0
        
        for record in records:
            if record.get('timestamp') in record_ids:
                record['synced'] = True
                synced_count += 1
        
        self._write_records(records)
        self.logger.info(f"Marked {synced_count} records as synced")
    
    def get_all_records(self) -> List[Dict]:
        """Get all records"""
        return self._read_records()
    
    def get_stats(self) -> Dict:
        """Get storage statistics"""
        records = self._read_records()
        synced = sum(1 for r in records if r.get('synced', False))
        unsynced = len(records) - synced
        
        return {
            'total_records': len(records),
            'synced_records': synced,
            'unsynced_records': unsynced,
            'storage_file': str(self.storage_file),
            'max_capacity': self.max_records
        }
    
    def clear_synced_records(self):
        """Remove all synced records to free up space"""
        records = self._read_records()
        unsynced = [r for r in records if not r.get('synced', False)]
        self._write_records(unsynced)
        removed = len(records) - len(unsynced)
        self.logger.info(f"Cleared {removed} synced records")
        return removed

if __name__ == "__main__":
    # Test the storage
    storage = OfflineStorage()
    
    # Add test record
    test_record = {
        'card_id': '123456789',
        'timestamp': datetime.utcnow().isoformat(),
        'device_id': 'test-device',
        'synced': False
    }
    
    storage.add_record(test_record)
    print(f"Stats: {json.dumps(storage.get_stats(), indent=2)}")
