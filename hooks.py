"""Plugin lifecycle hooks — a0 calls install()/uninstall() on load/unload.

install() deliberately does NOT configure the engine: a0's plugin loader
calls hooks before the runtime config layer is guaranteed ready, so the
sync `startup_migration` extension owns configure(). uninstall() stops the
auto-sync thread and clears backend registrations. Both bodies are
guarded — a hook raise would abort the host's install/uninstall path.
"""

from __future__ import annotations

import logging


def install() -> None:
    try:
        logging.getLogger("a0.device_sync").info(
            "device-sync plugin installed — engine configures at startup_migration"
        )
    except Exception:
        pass


def uninstall() -> None:
    """Stop the auto-sync thread; drop backend registrations."""
    try:
        from usr.plugins.device_sync.helpers import memory_backend, runtime

        runtime._reset()
        memory_backend.reset_backends()
    except Exception:
        logging.getLogger("a0.device_sync").exception(
            "device-sync uninstall cleanup failed"
        )
