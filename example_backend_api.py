# Example Atlas Backend API Implementation
# This is a reference implementation showing the required endpoints

from flask import Flask, request, jsonify
from datetime import datetime
import json

app = Flask(__name__)

# In-memory storage (use a real database in production)
devices = {}
attendance_records = []

@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})

@app.route('/api/devices/register', methods=['POST'])
def register_device():
    """Register a new edge device"""
    data = request.json
    device_id = data.get('device_id')
    
    if not device_id:
        return jsonify({'error': 'device_id required'}), 400
    
    devices[device_id] = {
        'device_id': device_id,
        'device_name': data.get('device_name'),
        'location': data.get('location'),
        'registered_at': data.get('registered_at'),
        'last_seen': datetime.utcnow().isoformat()
    }
    
    return jsonify({'message': 'Device registered successfully', 'device': devices[device_id]}), 201

@app.route('/api/devices/<device_id>', methods=['GET'])
def get_device(device_id):
    """Get device information"""
    if device_id not in devices:
        return jsonify({'error': 'Device not found'}), 404
    
    return jsonify(devices[device_id])

@app.route('/api/devices/heartbeat', methods=['POST'])
def device_heartbeat():
    """Receive heartbeat from device"""
    data = request.json
    device_id = data.get('device_id')
    
    if device_id in devices:
        devices[device_id]['last_seen'] = datetime.utcnow().isoformat()
        return jsonify({'message': 'Heartbeat received'})
    
    return jsonify({'error': 'Device not registered'}), 404

@app.route('/api/attendance', methods=['POST'])
def create_attendance():
    """Create single attendance record"""
    data = request.json
    
    # Validate required fields
    required = ['card_id', 'timestamp', 'device_id']
    if not all(field in data for field in required):
        return jsonify({'error': 'Missing required fields'}), 400
    
    # Add to records
    record = {
        'id': len(attendance_records) + 1,
        'card_id': data['card_id'],
        'timestamp': data['timestamp'],
        'device_id': data['device_id'],
        'device_name': data.get('device_name'),
        'location': data.get('location'),
        'created_at': datetime.utcnow().isoformat()
    }
    
    attendance_records.append(record)
    
    return jsonify({'message': 'Attendance recorded', 'record': record}), 201

@app.route('/api/attendance/batch', methods=['POST'])
def create_attendance_batch():
    """Create multiple attendance records"""
    data = request.json
    records = data.get('records', [])
    
    if not records:
        return jsonify({'error': 'No records provided'}), 400
    
    created = []
    for record_data in records:
        record = {
            'id': len(attendance_records) + 1,
            'card_id': record_data['card_id'],
            'timestamp': record_data['timestamp'],
            'device_id': record_data['device_id'],
            'device_name': record_data.get('device_name'),
            'location': record_data.get('location'),
            'created_at': datetime.utcnow().isoformat()
        }
        attendance_records.append(record)
        created.append(record)
    
    return jsonify({
        'message': 'Batch created successfully',
        'count': len(created),
        'records': created
    }), 201

@app.route('/api/attendance', methods=['GET'])
def get_attendance():
    """Get attendance records"""
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 100, type=int)
    
    records = attendance_records
    
    if device_id:
        records = [r for r in records if r['device_id'] == device_id]
    
    # Sort by timestamp descending
    records = sorted(records, key=lambda x: x['timestamp'], reverse=True)
    
    return jsonify({
        'records': records[:limit],
        'total': len(records)
    })

@app.route('/api/devices', methods=['GET'])
def get_devices():
    """Get all registered devices"""
    return jsonify({
        'devices': list(devices.values()),
        'count': len(devices)
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
