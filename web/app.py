from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_cors import CORS
import json
import logging
from datetime import datetime
from pathlib import Path
import sys
import os

# Add parent directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from services.offline_storage import OfflineStorage
from services.api_sync import APISync

app = Flask(__name__, static_folder='static',static_url_path='/static')
CORS(app)

# Initialize services
config_path = 'config/config.json'
storage = OfflineStorage(config_path)
api_sync = APISync(config_path)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('WebPortal')

def load_config():
    """Load configuration"""
    with open(config_path, 'r') as f:
        return json.load(f)

@app.route('/')
def index():
    """Home page"""
    return render_template('dashboard.html')

@app.route('/api/status')
def get_status():
    """Get system status"""
    config = load_config()
    stats = storage.get_stats()
    is_online = api_sync.check_connection()
    
    return jsonify({
        'device': config['device'],
        'backend_online': is_online,
        'last_check': api_sync.last_check.isoformat() if api_sync.last_check else None,
        'storage': stats,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/api/records')
def get_records():
    """Get attendance records"""
    limit = request.args.get('limit', 100, type=int)
    records = storage.get_all_records()
    
    # Sort by timestamp descending
    records.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    return jsonify({
        'records': records[:limit],
        'total': len(records)
    })

@app.route('/api/records/unsynced')
def get_unsynced_records():
    """Get unsynced records"""
    records = storage.get_unsynced_records()
    return jsonify({
        'records': records,
        'count': len(records)
    })

@app.route('/api/sync/trigger', methods=['POST'])
def trigger_sync():
    """Manually trigger sync"""
    if not api_sync.check_connection():
        return jsonify({
            'success': False,
            'message': 'Backend is offline'
        }), 503
    
    unsynced = storage.get_unsynced_records()
    if not unsynced:
        return jsonify({
            'success': True,
            'message': 'No records to sync',
            'synced': 0
        })
    
    result = api_sync.send_batch_attendance(unsynced)
    
    if result['synced_ids']:
        storage.mark_as_synced(result['synced_ids'])
    
    return jsonify({
        'success': result['success'] > 0,
        'synced': result['success'],
        'failed': result['failed']
    })

@app.route('/api/config')
def get_config():
    """Get configuration (excluding sensitive data)"""
    config = load_config()
    
    # Remove sensitive information
    safe_config = config.copy()
    if 'server' in safe_config and 'api_key' in safe_config['server']:
        safe_config['server']['api_key'] = '***HIDDEN***'
    
    return jsonify(safe_config)

@app.route('/api/config', methods=['POST'])
def update_config():
    """Update configuration"""
    try:
        new_config = request.json
        
        # Validate required fields
        required_fields = ['device', 'server', 'storage']
        for field in required_fields:
            if field not in new_config:
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400
        
        # Save configuration
        with open(config_path, 'w') as f:
            json.dump(new_config, f, indent=2)
        
        return jsonify({
            'success': True,
            'message': 'Configuration updated successfully'
        })
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@app.route('/api/storage/clear', methods=['POST'])
def clear_synced_storage():
    """Clear synced records from storage"""
    try:
        removed = storage.clear_synced_records()
        return jsonify({
            'success': True,
            'removed': removed,
            'message': f'Cleared {removed} synced records'
        })
    except Exception as e:
        logger.error(f"Error clearing storage: {e}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@app.route('/api/device/register', methods=['POST'])
def register_device():
    """Register device with backend"""
    if not api_sync.check_connection():
        return jsonify({
            'success': False,
            'message': 'Backend is offline'
        }), 503
    
    success = api_sync.register_device()
    
    return jsonify({
        'success': success,
        'message': 'Device registered successfully' if success else 'Registration failed'
    })

@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat()
    })

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    config = load_config()
    port = config['web']['port']
    host = config['web']['host']
    
    logger.info(f"Starting Atlas Edge Web Portal on {host}:{port}")
    app.run(host=host, port=port, debug=False)
