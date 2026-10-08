"""startup_migration extension — configure the device-sync engine.

Sync `execute` (startup_migration runs under call_extensions_sync — an
awaitable return would raise). Engine construction is synchronous and the
auto-sync loop lives on its own thread (blocking urllib work) so no event
loop is needed here.

The `usr.plugins.device_sync` import stays inside execute() — upstream's
`import_module` sweep is unguarded, so a module-level plugin import that
ever fails would abort the whole extension sweep (and a0 boot for
startup_migration). `_70_` intentionally lands late so sibling plugins
(e.g. a Kurultai memory store) can register memory backends first via
`runtime.register_memory_backend`.
"""

from helpers.extension import Extension


class DeviceSyncInit(Extension):
    def execute(self, **kwargs) -> None:
        del kwargs  # a0 passes no payload at this extension point
        try:
            from usr.plugins.device_sync.helpers import runtime

            runtime.configure(None)
        except Exception:
            pass
