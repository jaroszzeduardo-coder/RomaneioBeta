"""Limite de Chromes do Fretio conforme a RAM do computador.

A cotação continua nos portais. Este módulo só decide quantos navegadores
podem ficar abertos ao mesmo tempo para o Windows não passar a usar disco.
"""

from __future__ import annotations

import sys
from typing import Any

# Máquinas de 2 GB reportam 2048 MB, às vezes um pouco menos por hardware reservado.
_RAM_UM_CHROME_MB = 3072
_RAM_DOIS_CHROMES_MB = 6144
_RAM_LIVRE_UM_CHROME_MB = 600

LOW_MEMORY_USER_MESSAGE = (
    "Computador com pouca memória: cotando uma transportadora por vez para não travar."
)

_LEAN_CHROME_ARGS = (
    "--renderer-process-limit=1",
    "--disk-cache-size=1",
    "--media-cache-size=1",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
)


def _memory_status() -> Any | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status
    except Exception:
        return None


def physical_ram_mb() -> int | None:
    status = _memory_status()
    if status is None:
        return None
    try:
        return int(status.ullTotalPhys // (1024 * 1024))
    except Exception:
        return None


def available_ram_mb() -> int | None:
    status = _memory_status()
    if status is None:
        return None
    try:
        return int(status.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None


def single_chrome_mode() -> bool:
    """Um Chrome do Fretio por vez: PC com até 3 GB, ou com menos de 600 MB livres."""
    total = physical_ram_mb()
    if total is not None and total <= _RAM_UM_CHROME_MB:
        return True
    free = available_ram_mb()
    return free is not None and free < _RAM_LIVRE_UM_CHROME_MB


def chrome_concurrency_limit(configured: int) -> int:
    """Teto de Chromes sem passar do max_paralelo já configurado.

    Falha ao ler a RAM mantém o valor configurado, para não reduzir um PC grande.
    """
    try:
        configured_n = int(configured or 1)
    except (TypeError, ValueError):
        configured_n = 1
    configured_n = max(1, configured_n)
    if single_chrome_mode():
        return 1
    total = physical_ram_mb()
    if total is not None and total <= _RAM_DOIS_CHROMES_MB:
        return min(configured_n, 2)
    return configured_n


def lean_chrome_args() -> list[str]:
    if not single_chrome_mode():
        return []
    return list(_LEAN_CHROME_ARGS)


def apply_low_memory_chrome_args(launch_args: list[str]) -> list[str]:
    """Troca o limite de renderers e acrescenta flags leves, sem duplicar."""
    extra = lean_chrome_args()
    if not extra:
        return launch_args
    args = [item for item in launch_args if not str(item).startswith("--renderer-process-limit=")]
    present = {str(item).split("=", 1)[0] for item in args if str(item).startswith("--")}
    missing = [item for item in extra if item.split("=", 1)[0] not in present]
    if not missing:
        return args
    # O Chrome trata o que vem depois da URL como outro endereço.
    if args and not str(args[-1]).startswith("-"):
        return [*args[:-1], *missing, args[-1]]
    return [*args, *missing]
