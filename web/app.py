from flask import Flask, render_template, jsonify, request, send_file, Response
from flask_cors import CORS
import json
import logging
import io
import csv
from datetime import datetime, timedelta
from pathlib import Path
import sys
import os
import subprocess
import platform

# Add parent directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from services.offline_storage import OfflineStorage
from services.api_sync import APISync

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

# Initialize services
config_path = os.path.join(parent_dir, 'config/config.json')
storage = OfflineStorage(config_path)
api_sync = APISync(config_path)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('WebPortal')


def load_config():
    """Load configuration"""
    with open(config_path, 'r') as f:
        return json.load(f)


def get_system_info():
    """Get Raspberry Pi system information"""
    info = {
        'hostname': platform.node(),
        'platform': platform.system(),
        'architecture': platform.machine(),
        'python_version': platform.python_version(),
        'cpu_temp': None,
        'cpu_usage': None,
        'memory_usage': None,
        'disk_usage': None,
        'uptime': None,
        'ip_address': None
    }

    try:
        # CPU Temperature (Raspberry Pi specific)
        if os.path.exists('/sys/class/thermal/thermal_zone0/temp'):
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = int(f.read().strip()) / 1000
                info['cpu_temp'] = round(temp, 1)

        # CPU Usage
        if os.path.exists('/proc/stat'):
            with open('/proc/stat', 'r') as f:
                cpu_line = f.readline()
                cpu_times = list(map(int, cpu_line.split()[1:]))
                idle = cpu_times[3]
                total = sum(cpu_times)
                info['cpu_usage'] = round(100 * (1 - idle / total), 1)

        # Memory Usage
        if os.path.exists('/proc/meminfo'):
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
                mem_total = int(lines[0].split()[1])
                mem_available = int(lines[2].split()[1])
                info['memory_usage'] = {
                    'total_mb': round(mem_total / 1024, 1),
                    'used_mb': round((mem_total - mem_available) / 1024, 1),
                    'percent': round(100 * (1 - mem_available / mem_total), 1)
                }

        # Disk Usage
        if os.path.exists('/'):
            statvfs = os.statvfs('/')
            total = statvfs.f_frsize * statvfs.f_blocks
            free = statvfs.f_frsize * statvfs.f_bavail
            used = total - free
            info['disk_usage'] = {
                'total_gb': round(total / (1024 ** 3), 1),
                'used_gb': round(used / (1024 ** 3), 1),
                'percent': round(100 * used / total, 1)
            }

        # Uptime
        if os.path.exists('/proc/uptime'):
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                days = int(uptime_seconds // 86400)
                hours = int((uptime_seconds % 86400) // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                info['uptime'] = f"{days}d {hours}h {minutes}m"
                info['uptime_seconds'] = int(uptime_seconds)

        # IP Address
        try:
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                info['ip_address'] = ips[0] if ips else None
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error getting system info: {e}")

    return info


def read_log_file(log_path, lines=100):
    """Read last N lines from a log file"""
    try:
        if not os.path.exists(log_path):
            return []

        with open(log_path, 'r') as f:
            all_lines = f.readlines()
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
    except Exception as e:
        logger.error(f"Error reading log file {log_path}: {e}")
        return []


@app.route('/')
def index():
    """Home page"""
    return render_template('dashboard.html')


@app.route('/api/status')
def get_status():
    """Get comprehensive system status"""
    config = load_config()
    stats = storage.get_stats()
    is_online = api_sync.check_connection()
    sync_config = api_sync.get_sync_config()
    system_info = get_system_info()

    return jsonify({
        'device': config['device'],
        'backend_online': is_online,
        'last_check': api_sync.last_check.isoformat() if api_sync.last_check else None,
        'last_sync': api_sync.last_sync.isoformat() if api_sync.last_sync else None,
        'storage': stats,
        'sync_config': sync_config,
        'system': system_info,
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/api/records')
def get_records():
    """Get attendance records with pagination and filtering"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    synced_filter = request.args.get('synced', None)
    search = request.args.get('search', '')

    records = storage.get_all_records()

    # Filter by synced status
    if synced_filter is not None:
        synced_bool = synced_filter.lower() == 'true'
        records = [r for r in records if r.get('synced', False) == synced_bool]

    # Search by card_id
    if search:
        records = [r for r in records if search.lower() in r.get('card_id', '').lower()]

    # Sort by timestamp descending
    records.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    total = len(records)
    paginated_records = records[offset:offset + limit]

    return jsonify({
        'records': paginated_records,
        'total': total,
        'limit': limit,
        'offset': offset,
        'has_more': offset + limit < total
    })


@app.route('/api/records/unsynced')
def get_unsynced_records():
    """Get unsynced records"""
    records = storage.get_unsynced_records()
    return jsonify({
        'records': records,
        'count': len(records)
    })


@app.route('/api/records/stats')
def get_records_stats():
    """Get detailed records statistics"""
    records = storage.get_all_records()

    # Calculate stats
    today = datetime.utcnow().date()
    today_count = 0
    last_hour_count = 0
    hourly_distribution = {str(i).zfill(2): 0 for i in range(24)}

    now = datetime.utcnow()
    one_hour_ago = now - timedelta(hours=1)

    for record in records:
        try:
            ts = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
            ts_naive = ts.replace(tzinfo=None)

            if ts_naive.date() == today:
                today_count += 1
                hourly_distribution[str(ts_naive.hour).zfill(2)] += 1

            if ts_naive >= one_hour_ago:
                last_hour_count += 1
        except Exception:
            pass

    stats = storage.get_stats()

    return jsonify({
        'total_records': stats['total_records'],
        'synced_records': stats['synced_records'],
        'unsynced_records': stats['unsynced_records'],
        'today_count': today_count,
        'last_hour_count': last_hour_count,
        'hourly_distribution': hourly_distribution,
        'storage_capacity': stats['max_capacity'],
        'storage_usage_percent': round(100 * stats['total_records'] / stats['max_capacity'], 1) if stats['max_capacity'] > 0 else 0
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

    # Use chunk-based sync
    result = api_sync.sync_records_in_chunks(unsynced)

    if result['synced_ids']:
        storage.mark_as_synced(result['synced_ids'])

    return jsonify({
        'success': result['success'] > 0,
        'synced': result['success'],
        'failed': result['failed'],
        'errors': result.get('errors', [])[:5]  # Return first 5 errors
    })


@app.route('/api/sync/config')
def get_sync_config():
    """Get sync configuration"""
    return jsonify(api_sync.get_sync_config())


@app.route('/api/config')
def get_config():
    """Get configuration (excluding sensitive data)"""
    config = load_config()

    # Remove sensitive information
    safe_config = config.copy()
    if 'server' in safe_config and 'api_key' in safe_config['server']:
        api_key = safe_config['server']['api_key']
        safe_config['server']['api_key'] = f"***{api_key[-8:]}" if len(api_key) > 8 else '***HIDDEN***'

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


@app.route('/api/logs')
def get_logs():
    """Get application logs"""
    log_type = request.args.get('type', 'api_sync')
    lines = request.args.get('lines', 100, type=int)

    log_files = {
        'api_sync': os.path.join(parent_dir, 'logs/api_sync.log'),
        'storage': os.path.join(parent_dir, 'logs/storage.log'),
        'attendance': os.path.join(parent_dir, 'logs/attendance.log'),
        'rfid': os.path.join(parent_dir, 'logs/rfid.log')
    }

    log_path = log_files.get(log_type)
    if not log_path:
        return jsonify({'error': 'Invalid log type'}), 400

    log_lines = read_log_file(log_path, lines)

    # Parse log lines into structured format
    parsed_logs = []
    for line in log_lines:
        line = line.strip()
        if not line:
            continue

        # Try to parse standard log format: 2024-01-15 08:30:00,123 - Logger - LEVEL - Message
        try:
            parts = line.split(' - ', 3)
            if len(parts) >= 4:
                parsed_logs.append({
                    'timestamp': parts[0],
                    'logger': parts[1],
                    'level': parts[2],
                    'message': parts[3]
                })
            else:
                parsed_logs.append({
                    'timestamp': None,
                    'logger': None,
                    'level': 'INFO',
                    'message': line
                })
        except Exception:
            parsed_logs.append({
                'timestamp': None,
                'logger': None,
                'level': 'INFO',
                'message': line
            })

    return jsonify({
        'logs': parsed_logs,
        'count': len(parsed_logs),
        'log_type': log_type,
        'file_path': log_path
    })


@app.route('/api/logs/available')
def get_available_logs():
    """Get list of available log files"""
    logs_dir = os.path.join(parent_dir, 'logs')
    available = []

    if os.path.exists(logs_dir):
        for filename in os.listdir(logs_dir):
            if filename.endswith('.log'):
                filepath = os.path.join(logs_dir, filename)
                stat = os.stat(filepath)
                available.append({
                    'name': filename.replace('.log', ''),
                    'filename': filename,
                    'size_kb': round(stat.st_size / 1024, 1),
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                })

    return jsonify({'logs': available})


@app.route('/api/export/records')
def export_records():
    """Export records as CSV or JSON"""
    format_type = request.args.get('format', 'csv')
    synced_filter = request.args.get('synced', None)

    records = storage.get_all_records()

    # Filter by synced status
    if synced_filter is not None:
        synced_bool = synced_filter.lower() == 'true'
        records = [r for r in records if r.get('synced', False) == synced_bool]

    # Sort by timestamp
    records.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

    if format_type == 'csv':
        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(['Card ID', 'Timestamp', 'Location', 'Device ID', 'Device Name', 'Synced'])

        # Data
        for record in records:
            writer.writerow([
                record.get('card_id', ''),
                record.get('timestamp', ''),
                record.get('location', ''),
                record.get('device_id', ''),
                record.get('device_name', ''),
                'Yes' if record.get('synced', False) else 'No'
            ])

        output.seek(0)
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f'atlas_edge_records_{timestamp}.csv'

        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    elif format_type == 'json':
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        filename = f'atlas_edge_records_{timestamp}.json'

        return Response(
            json.dumps({'records': records, 'exported_at': datetime.utcnow().isoformat()}, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    else:
        return jsonify({'error': 'Invalid format. Use csv or json'}), 400


@app.route('/api/export/logs')
def export_logs():
    """Export logs as text file"""
    log_type = request.args.get('type', 'api_sync')

    log_files = {
        'api_sync': os.path.join(parent_dir, 'logs/api_sync.log'),
        'storage': os.path.join(parent_dir, 'logs/storage.log'),
        'attendance': os.path.join(parent_dir, 'logs/attendance.log'),
        'rfid': os.path.join(parent_dir, 'logs/rfid.log')
    }

    log_path = log_files.get(log_type)
    if not log_path or not os.path.exists(log_path):
        return jsonify({'error': 'Log file not found'}), 404

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'atlas_edge_{log_type}_{timestamp}.log'

    return send_file(
        log_path,
        mimetype='text/plain',
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/system')
def get_system_status():
    """Get detailed system status"""
    return jsonify(get_system_info())


@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '2.0.0'
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    config = load_config()
    port = config.get('web', {}).get('port', 8080)
    host = config.get('web', {}).get('host', '0.0.0.0')

    logger.info(f"Starting Atlas Edge Web Portal v2.0 on {host}:{port}")
    app.run(host=host, port=port, debug=False)
