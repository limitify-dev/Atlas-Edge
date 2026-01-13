#!/usr/bin/env python3
"""
USB RFID/IC Reader Service for Atlas Edge
Handles USB IC Card reading with exclusive device access
USB IC Readers act as HID keyboard devices - this module grabs exclusive access
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
    USB IC/RFID Reader with exclusive device access.

    Uses evdev to grab exclusive access to the HID device, preventing
    keystrokes from being sent to other applications on the system.
    """

    def __init__(self, config_path='config/config.json', device_path=None, device_name=None):
        """
        Initialize the IC Reader.

        Args:
            config_path: Path to configuration JSON file
            device_path: Explicit path to input device (e.g., /dev/input/event0)
            device_name: Exact name of the device to find (e.g., 'IC Reader')
        """
        self.config = self._load_config(config_path)
        self.device_path = device_path or self.config.get('rfid', {}).get('device_path')
        self.device_name = device_name or self.config.get('rfid', {}).get('device_name', 'IC Reader')
        self.setup_logging()

        self.device = None
        self.last_card_id = None
        self.last_read_time = 0
        self.debounce_time = self.config.get('rfid', {}).get('debounce_time', 2)
        self.grabbed = False

        # Build key map only if evdev is available
        self.KEY_MAP = self._build_key_map() if EVDEV_AVAILABLE else {}

    def _build_key_map(self):
        """Build the key code to character mapping"""
        return {
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
        }

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
        self.logger = logging.getLogger('ICReader')

    def find_ic_reader(self):
        """
        Find the IC Reader device by exact name match.

        Returns:
            InputDevice or None: The found device or None if not found
        """
        if not EVDEV_AVAILABLE:
            self.logger.error("evdev module not available. Install with: pip install evdev")
            return None

        # If explicit path is given and exists, use it
        if self.device_path and os.path.exists(self.device_path):
            try:
                device = InputDevice(self.device_path)
                self.logger.info(f"Using configured device: {device.name} at {self.device_path}")
                return device
            except Exception as e:
                self.logger.error(f"Failed to open configured device {self.device_path}: {e}")

        # Search for IC Reader by exact name
        self.logger.info(f"Searching for device: '{self.device_name}'...")

        for path in evdev.list_devices():
            try:
                device = InputDevice(path)

                # Exact name match (case-insensitive)
                if device.name.lower() == self.device_name.lower():
                    self.logger.info(f"Found IC Reader: {device.name}")
                    self.logger.info(f"  Path: {device.path}")
                    self.logger.info(f"  Phys: {device.phys}")
                    return device

            except Exception as e:
                self.logger.debug(f"Error checking device {path}: {e}")
                continue

        # If exact match not found, try partial match
        self.logger.warning(f"Exact match not found for '{self.device_name}', trying partial match...")

        for path in evdev.list_devices():
            try:
                device = InputDevice(path)

                # Partial name match
                if self.device_name.lower() in device.name.lower():
                    self.logger.info(f"Found device (partial match): {device.name}")
                    self.logger.info(f"  Path: {device.path}")
                    return device

            except Exception:
                continue

        # List all devices if nothing found
        self.logger.error(f"Device '{self.device_name}' not found!")
        self.logger.info("Available input devices:")
        for path in evdev.list_devices():
            try:
                device = InputDevice(path)
                caps = device.capabilities()
                if evdev.ecodes.EV_KEY in caps:
                    self.logger.info(f"  - '{device.name}' ({device.path})")
            except Exception:
                pass

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
        Grab exclusive access to the IC Reader device.

        This prevents the device's keystrokes from being sent to other applications.
        """
        if not self.device:
            self.device = self.find_ic_reader()

        if not self.device:
            self.logger.error("No IC Reader device found!")
            return False

        try:
            self.device.grab()
            self.grabbed = True
            self.logger.info(f"EXCLUSIVE ACCESS GRANTED: {self.device.name}")
            self.logger.info("Keystrokes from this device will NOT go to other applications")
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
                self.logger.info("Released exclusive access to device")
            except Exception as e:
                self.logger.error(f"Error releasing device: {e}")

    def read_card(self):
        """
        Read card ID from the IC Reader.

        Returns:
            str or None: The card ID or None if no card was read
        """
        if not self.device:
            return None

        card_data = []

        try:
            import select

            # Wait for input (100ms timeout)
            r, w, x = select.select([self.device.fd], [], [], 0.1)

            if not r:
                return None

            # Read all available events
            for event in self.device.read():
                if event.type == evdev.ecodes.EV_KEY:
                    key_event = categorize(event)

                    # Only process key down events (value=1)
                    if key_event.keystate == 1:
                        keycode = key_event.scancode

                        # Enter key = end of card data
                        if keycode == evdev.ecodes.KEY_ENTER:
                            if card_data:
                                card_id = ''.join(card_data)

                                # Debounce check
                                current_time = time.time()
                                if card_id == self.last_card_id and \
                                   (current_time - self.last_read_time) < self.debounce_time:
                                    self.logger.debug(f"Ignoring duplicate: {card_id}")
                                    card_data = []
                                    return None

                                self.last_card_id = card_id
                                self.last_read_time = current_time
                                self.logger.info(f"Card scanned: {card_id}")
                                return card_id
                            card_data = []

                        # Map keycode to character
                        elif keycode in self.KEY_MAP:
                            card_data.append(self.KEY_MAP[keycode])

            return None

        except Exception as e:
            self.logger.error(f"Error reading card: {e}")
            return None

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
        self.logger.info("Starting IC Reader Service")
        self.logger.info(f"Device Name: {self.config['device']['name']}")
        self.logger.info(f"Location: {self.config['device']['location']}")
        self.logger.info(f"Looking for: '{self.device_name}'")
        self.logger.info("=" * 60)

        if not EVDEV_AVAILABLE:
            self.logger.error("evdev not available! Install with: pip install evdev")
            return

        # Grab exclusive access
        if not self.grab_device():
            self.logger.error("Failed to get exclusive access to IC Reader!")
            self.logger.error("Make sure:")
            self.logger.error("  1. The IC Reader is connected")
            self.logger.error("  2. You have permissions (run with sudo or add user to 'input' group)")
            self.logger.error("  3. The device name in config matches your reader")
            return

        self.logger.info("=" * 60)
        self.logger.info("Ready to scan cards...")
        self.logger.info("Press Ctrl+C to stop")
        self.logger.info("=" * 60)

        try:
            while True:
                card_id = self.read_card()
                if card_id:
                    record = self.create_attendance_record(card_id)
                    callback(record)
                time.sleep(0.01)  # Small delay

        except KeyboardInterrupt:
            self.logger.info("\nStopping IC Reader service...")
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


# Backwards compatibility
RFIDReader = USBRFIDReader


def list_all_devices():
    """Utility function to list all input devices"""
    if not EVDEV_AVAILABLE:
        print("evdev module not available. Install with: pip install evdev")
        return

    print("\n" + "=" * 70)
    print("AVAILABLE INPUT DEVICES")
    print("=" * 70)

    for path in evdev.list_devices():
        try:
            device = InputDevice(path)
            caps = device.capabilities()
            has_keys = evdev.ecodes.EV_KEY in caps

            print(f"\nName: '{device.name}'")
            print(f"  Path: {device.path}")
            print(f"  Phys: {device.phys}")
            print(f"  Has keyboard events: {has_keys}")

            if has_keys and ('reader' in device.name.lower() or 'ic' in device.name.lower()):
                print("  >>> THIS IS LIKELY YOUR IC READER <<<")

        except Exception as e:
            print(f"Error reading {path}: {e}")

    print("\n" + "=" * 70)
    print("To use a specific device, set 'device_name' in config.json")
    print("Example: \"device_name\": \"IC Reader\"")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='USB IC/RFID Reader')
    parser.add_argument('--device', '-d', type=str, help='Device path (e.g., /dev/input/event0)')
    parser.add_argument('--name', '-n', type=str, help='Device name to search for (e.g., "IC Reader")')
    parser.add_argument('--list', '-l', action='store_true', help='List all available input devices')
    parser.add_argument('--test', '-t', action='store_true', help='Run in test mode')
    args = parser.parse_args()

    if args.list:
        list_all_devices()
        sys.exit(0)

    reader = USBRFIDReader(device_path=args.device, device_name=args.name)

    def test_callback(record):
        print("\n" + "=" * 60)
        print("CARD SCANNED!")
        print(json.dumps(record, indent=2))
        print("=" * 60 + "\n")

    try:
        if args.test:
            print("\nTesting device detection...")
            devices = reader.list_devices()
            print("\nAll devices with keyboard capability:")
            for d in devices:
                if d['has_keyboard']:
                    print(f"  '{d['name']}' ({d['path']})")

            print(f"\nSearching for: '{reader.device_name}'")
            device = reader.find_ic_reader()
            if device:
                print(f"FOUND: '{device.name}' at {device.path}")
            else:
                print("NOT FOUND")
        else:
            reader.start_reading(test_callback)
    except KeyboardInterrupt:
        print("\nExiting...")
    except PermissionError:
        print("\n" + "=" * 60)
        print("PERMISSION DENIED!")
        print("=" * 60)
        print("Run with sudo OR add your user to the 'input' group:")
        print("  sudo usermod -a -G input $USER")
        print("Then log out and back in.")
        print("=" * 60)
