#!/usr/bin/env python3
"""
USB RFID Reader Service for Atlas Edge
Handles USB RFID card reading with exclusive device access
USB RFID readers act as HID keyboard devices - this module grabs exclusive access
to prevent keystrokes from being sent to other applications
"""

import json
import time
import logging
import sys
import os
from datetime import datetime
from pathlib import Path

# Try to import evdev for exclusive input grabbing
try:
    import evdev
    from evdev import InputDevice, categorize, ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False


class USBRFIDReader:
    """
    USB RFID Reader with exclusive device access.

    Uses evdev to grab exclusive access to the HID device, preventing
    keystrokes from being sent to other applications on the system.
    """

    # Key code to character mapping for evdev
    KEY_MAP = {
        evdev.ecodes.KEY_1: '1', evdev.ecodes.KEY_2: '2', evdev.ecodes.KEY_3: '3',
        evdev.ecodes.KEY_4: '4', evdev.ecodes.KEY_5: '5', evdev.ecodes.KEY_6: '6',
        evdev.ecodes.KEY_7: '7', evdev.ecodes.KEY_8: '8', evdev.ecodes.KEY_9: '9',
        evdev.ecodes.KEY_0: '0',
        evdev.ecodes.KEY_A: 'A', evdev.ecodes.KEY_B: 'B', evdev.ecodes.KEY_C: 'C',
        evdev.ecodes.KEY_D: 'D', evdev.ecodes.KEY_E: 'E', evdev.ecodes.KEY_F: 'F',
        evdev.ecodes.KEY_G: 'G', evdev.ecodes.KEY_H: 'H', evdev.ecodes.KEY_I: 'I',
        evdev.ecodes.KEY_J: 'J', evdev.ecodes.KEY_K: 'K', evdev.ecodes.KEY_L: 'L',
        evdev.ecodes.KEY_M: 'M', evdev.ecodes.KEY_N: 'N', evdev.ecodes.KEY_O: 'O',
        evdev.ecodes.KEY_P: 'P', evdev.ecodes.KEY_Q: 'Q', evdev.ecodes.KEY_R: 'R',
        evdev.ecodes.KEY_S: 'S', evdev.ecodes.KEY_T: 'T', evdev.ecodes.KEY_U: 'U',
        evdev.ecodes.KEY_V: 'V', evdev.ecodes.KEY_W: 'W', evdev.ecodes.KEY_X: 'X',
        evdev.ecodes.KEY_Y: 'Y', evdev.ecodes.KEY_Z: 'Z',
    } if EVDEV_AVAILABLE else {}

    def __init__(self, config_path='config/config.json', device_path=None, device_name=None):
        """
        Initialize the RFID reader.

        Args:
            config_path: Path to configuration JSON file
            device_path: Explicit path to input device (e.g., /dev/input/event0)
            device_name: Name pattern to search for (e.g., 'RFID', 'Reader', 'HID')
        """
        self.config = self._load_config(config_path)
        self.device_path = device_path or self.config.get('rfid', {}).get('device_path')
        self.device_name = device_name or self.config.get('rfid', {}).get('device_name', 'RFID')
        self.setup_logging()

        self.device = None
        self.last_card_id = None
        self.last_read_time = 0
        self.debounce_time = self.config.get('rfid', {}).get('debounce_time', 2)
        self.grabbed = False

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
        log_dir = Path('logs')
        log_dir.mkdir(exist_ok=True)

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(log_dir / 'rfid.log')
            ]
        )
        self.logger = logging.getLogger('USBRFIDReader')

    def find_rfid_device(self):
        """
        Auto-detect USB RFID reader device using evdev.

        Returns:
            InputDevice or None: The found device or None if not found
        """
        if not EVDEV_AVAILABLE:
            self.logger.error("evdev module not available. Install with: pip install evdev")
            return None

        # If explicit path is given, try that first
        if self.device_path and os.path.exists(self.device_path):
            try:
                device = InputDevice(self.device_path)
                self.logger.info(f"Using configured device: {device.name} at {self.device_path}")
                return device
            except Exception as e:
                self.logger.error(f"Failed to open configured device {self.device_path}: {e}")

        # Search for RFID reader by name
        self.logger.info("Searching for RFID reader device...")
        devices = [InputDevice(path) for path in evdev.list_devices()]

        # Keywords to identify RFID readers
        keywords = ['rfid', 'reader', 'hid', 'card', 'usb', 'keyboard']

        for device in devices:
            device_name_lower = device.name.lower()

            # Check if device name matches any keyword
            if any(kw in device_name_lower for kw in keywords):
                # Verify it has keyboard capabilities (sends key events)
                caps = device.capabilities()
                if evdev.ecodes.EV_KEY in caps:
                    self.logger.info(f"Found potential RFID device: {device.name}")
                    self.logger.info(f"  Path: {device.path}")
                    self.logger.info(f"  Phys: {device.phys}")
                    return device

        # If no match found by name, list all devices for manual selection
        self.logger.warning("No RFID device auto-detected. Available devices:")
        for device in devices:
            caps = device.capabilities()
            if evdev.ecodes.EV_KEY in caps:
                self.logger.info(f"  - {device.name} ({device.path})")

        return None

    def list_devices(self):
        """List all available input devices"""
        if not EVDEV_AVAILABLE:
            self.logger.error("evdev module not available")
            return []

        devices = []
        for path in evdev.list_devices():
            try:
                device = InputDevice(path)
                caps = device.capabilities()
                has_keys = evdev.ecodes.EV_KEY in caps
                devices.append({
                    'path': device.path,
                    'name': device.name,
                    'phys': device.phys,
                    'has_keyboard': has_keys
                })
            except Exception:
                pass

        return devices

    def grab_device(self):
        """
        Grab exclusive access to the RFID device.

        This prevents the device's keystrokes from being sent to other applications.
        """
        if not self.device:
            self.device = self.find_rfid_device()

        if not self.device:
            self.logger.error("No RFID device found!")
            return False

        try:
            self.device.grab()
            self.grabbed = True
            self.logger.info(f"Grabbed exclusive access to: {self.device.name}")
            return True
        except IOError as e:
            self.logger.error(f"Failed to grab device (need root/input group): {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error grabbing device: {e}")
            return False

    def ungrab_device(self):
        """Release exclusive access to the device"""
        if self.device and self.grabbed:
            try:
                self.device.ungrab()
                self.grabbed = False
                self.logger.info("Released device")
            except Exception as e:
                self.logger.error(f"Error releasing device: {e}")

    def read_card_evdev(self):
        """
        Read card ID from the grabbed input device using evdev.

        Returns:
            str or None: The card ID or None if no card was read
        """
        if not self.device:
            return None

        card_data = []
        timeout = 5  # seconds
        start_time = time.time()

        try:
            # Use select to wait for input with timeout
            import select

            while time.time() - start_time < timeout:
                # Wait for input (100ms timeout for each check)
                r, w, x = select.select([self.device.fd], [], [], 0.1)

                if not r:
                    continue

                # Read events
                for event in self.device.read():
                    if event.type == evdev.ecodes.EV_KEY:
                        key_event = categorize(event)

                        # Only process key down events
                        if key_event.keystate == 1:  # Key down
                            keycode = key_event.scancode

                            # Check for Enter key (end of card data)
                            if keycode == evdev.ecodes.KEY_ENTER:
                                if card_data:
                                    return ''.join(card_data)
                                card_data = []

                            # Map keycode to character
                            elif keycode in self.KEY_MAP:
                                card_data.append(self.KEY_MAP[keycode])

            return None

        except Exception as e:
            self.logger.error(f"Error reading card: {e}")
            return None

    def read_card_hidraw(self):
        """Fallback: Read card ID from HID raw device"""
        device_path = self.device_path or '/dev/hidraw0'

        try:
            with open(device_path, 'rb') as device:
                self.logger.info(f"Reading from {device_path}...")

                card_data = []
                start_time = time.time()
                timeout = 5

                while time.time() - start_time < timeout:
                    try:
                        data = device.read(8)
                        if data:
                            for byte in data:
                                if 4 <= byte <= 39:
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
            self.logger.error(f"Permission denied: {device_path}. Run with sudo or add user to input group.")
            return None
        except FileNotFoundError:
            self.logger.error(f"Device not found: {device_path}")
            return None
        except Exception as e:
            self.logger.error(f"Error reading card: {e}")
            return None

    def _hid_to_char(self, hid_code):
        """Convert HID keyboard code to character (fallback method)"""
        hid_map = {
            4: 'A', 5: 'B', 6: 'C', 7: 'D', 8: 'E', 9: 'F', 10: 'G',
            11: 'H', 12: 'I', 13: 'J', 14: 'K', 15: 'L', 16: 'M',
            17: 'N', 18: 'O', 19: 'P', 20: 'Q', 21: 'R', 22: 'S',
            23: 'T', 24: 'U', 25: 'V', 26: 'W', 27: 'X', 28: 'Y', 29: 'Z',
            30: '1', 31: '2', 32: '3', 33: '4', 34: '5',
            35: '6', 36: '7', 37: '8', 38: '9', 39: '0'
        }
        return hid_map.get(hid_code, '')

    def read_card(self):
        """
        Read RFID card and return card ID.

        Uses evdev with exclusive grab if available, falls back to hidraw.

        Returns:
            str or None: The card ID or None if no card was read
        """
        card_id = None

        # Try evdev first (with exclusive grab)
        if EVDEV_AVAILABLE and self.device:
            card_id = self.read_card_evdev()
        else:
            card_id = self.read_card_hidraw()

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
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'device_id': self.config['device']['id'],
            'device_name': self.config['device']['name'],
            'location': self.config['device']['location'],
            'synced': False
        }
        return record

    def start_reading(self, callback):
        """
        Start continuous card reading with callback.

        Args:
            callback: Function to call when a card is read, receives attendance record
        """
        self.logger.info("=" * 60)
        self.logger.info("Starting USB RFID Reader Service")
        self.logger.info(f"Device: {self.config['device']['name']}")
        self.logger.info(f"Location: {self.config['device']['location']}")

        # Try to grab exclusive access
        if EVDEV_AVAILABLE:
            if self.grab_device():
                self.logger.info(f"Exclusive access: {self.device.name}")
                self.logger.info(f"Device path: {self.device.path}")
            else:
                self.logger.warning("Could not grab exclusive access, using fallback mode")
        else:
            self.logger.warning("evdev not available, using hidraw fallback")
            self.logger.warning("Install evdev for exclusive access: pip install evdev")

        self.logger.info("Ready to scan cards...")
        self.logger.info("=" * 60)

        try:
            while True:
                card_id = self.read_card()
                if card_id:
                    record = self.create_attendance_record(card_id)
                    callback(record)
                time.sleep(0.05)  # Small delay to prevent CPU spinning

        except KeyboardInterrupt:
            self.logger.info("Stopping RFID reader service...")
        finally:
            self.cleanup()

    def cleanup(self):
        """Cleanup resources"""
        self.ungrab_device()
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
        self.logger.info("Cleanup completed")


# Backwards compatibility alias
RFIDReader = USBRFIDReader


def list_all_devices():
    """Utility function to list all input devices"""
    if not EVDEV_AVAILABLE:
        print("evdev module not available. Install with: pip install evdev")
        return

    print("\nAvailable Input Devices:")
    print("=" * 70)

    for path in evdev.list_devices():
        try:
            device = InputDevice(path)
            caps = device.capabilities()
            has_keys = evdev.ecodes.EV_KEY in caps

            print(f"\nDevice: {device.name}")
            print(f"  Path: {device.path}")
            print(f"  Phys: {device.phys}")
            print(f"  Has keyboard events: {has_keys}")

            if has_keys:
                print("  ** This could be an RFID reader **")

        except Exception as e:
            print(f"Error reading {path}: {e}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='USB RFID Reader')
    parser.add_argument('--device', '-d', type=str, help='Device path (e.g., /dev/input/event0)')
    parser.add_argument('--list', '-l', action='store_true', help='List all available input devices')
    parser.add_argument('--test', '-t', action='store_true', help='Run in test mode')
    args = parser.parse_args()

    if args.list:
        list_all_devices()
        sys.exit(0)

    reader = USBRFIDReader(device_path=args.device)

    def test_callback(record):
        print("\n" + "=" * 60)
        print("ATTENDANCE RECORDED:")
        print(json.dumps(record, indent=2))
        print("=" * 60 + "\n")

    try:
        if args.test:
            # Just test device detection
            print("\nTesting device detection...")
            devices = reader.list_devices()
            for d in devices:
                print(f"  {d['name']} ({d['path']}) - Keyboard: {d['has_keyboard']}")

            print("\nAttempting to find RFID reader...")
            device = reader.find_rfid_device()
            if device:
                print(f"Found: {device.name} at {device.path}")
            else:
                print("No RFID reader found")
        else:
            reader.start_reading(test_callback)
    except KeyboardInterrupt:
        print("\nExiting...")
    except PermissionError:
        print("\nPermission denied! Run with sudo or add user to 'input' group:")
        print("  sudo usermod -a -G input $USER")
        print("Then log out and back in.")
