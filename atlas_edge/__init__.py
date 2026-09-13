"""Atlas-Edge — Raspberry Pi gateway between a ZKTeco F18 terminal and Atlas.

Two independent processes share one SQLite store:

* ``atlas_edge.listener`` — owns the F18 connection: streams card taps, buffers
  and forwards them to Atlas, runs the hourly onboard-log reconciliation, and
  executes device-write commands (single enroll / bulk sync) queued by the web
  UI. It is the *only* process that talks to the device.
* ``atlas_edge.web`` — a small FastAPI site for a technician on the school LAN:
  log in (against Atlas), watch device/queue status, and queue enrollment work.
"""

__version__ = "0.1.0"
