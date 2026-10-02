from __future__ import annotations

import os


# API tests must not launch real network probes in the application startup hook.
os.environ.setdefault("SUBTRANS_STARTUP_PROBE_ENABLED", "0")
