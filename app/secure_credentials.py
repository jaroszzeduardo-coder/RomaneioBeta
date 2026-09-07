"""Credential overlay compatible with legacy CONFIG.toml files.

This module stores sensitive provider fields in Windows Credential Manager when
available. It never removes values from CONFIG.toml; migration only copies
plaintext values into the secure store so older configs keep working.
"""
from __future__ import annotations

import copy
import ctypes
import logging
import os
import re
from ctypes import wintypes
from typing import Any, Iterator


_SERVICE_PREFIX = "Fretio"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_SENSITIVE_FIELDS = ("senha", "password", "token")
_CONFIG_CONTAINER_SECTIONS = {"fretio", "fretebot", "romaneio", "transportadoras"}
_logger = logging.getLogger(__name__)
_memory_fallback: dict[str, str] = {}


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def _warn(message: str, *args: Any) -> None:
    try:
        _logger.warning(message, *args)
    except Exception:
        pass


def _normalize_component(value: Any) -> str:
    text = str(value or "default").strip().lower() or "default"
    return re.sub(r"[^a-z0-9_.-]+", "_", text)


def _target_name(empresa: Any, transportadora: Any, campo: Any) -> str:
    return ":".join(
        [
            _SERVICE_PREFIX,
            _normalize_component(empresa),
            _normalize_component(transportadora),
            _normalize_component(campo),
        ]
    )


def _redact_target(target: str) -> str:
    """Mascara o segmento da empresa antes de logar (CWE-532).

    O formato é ``Fretio:<empresa>:<transportadora>:<campo>``. Transportadora e
    campo são nomes públicos normalizados; só a empresa pode revelar o cliente.
    """
    parts = str(target).split(":", 3)
    if len(parts) == 4:
        parts[1] = "***"
    return ":".join(parts)


def _read_windows_credential(target: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.windll.advapi32
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [wintypes.LPVOID]
        advapi32.CredFree.restype = None
        cred_ptr = PCREDENTIALW()
        if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
            return None
        try:
            blob_size = int(cred_ptr.contents.CredentialBlobSize or 0)
            if blob_size <= 0:
                return ""
            raw = ctypes.string_at(cred_ptr.contents.CredentialBlob, blob_size)
            return raw.decode("utf-16-le")
        finally:
            advapi32.CredFree(cred_ptr)
    except Exception as exc:
        _warn("Falha ao ler credencial segura %s: %s", _redact_target(target), exc)
        return None


def _write_windows_credential(target: str, value: str) -> bool:
    if os.name != "nt":
        return False
    try:
        advapi32 = ctypes.windll.advapi32
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        blob = str(value or "").encode("utf-16-le")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_byte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "Fretio"
        ok = bool(advapi32.CredWriteW(ctypes.byref(credential), 0))
        if not ok:
            _warn("Credential Manager recusou gravação para %s", _redact_target(target))
        return ok
    except Exception as exc:
        _warn("Falha ao gravar credencial segura %s: %s", _redact_target(target), exc)
        return False


def get_credential(empresa: str, transportadora: str, campo: str) -> str | None:
    """Return a credential from secure storage, or None if unavailable."""
    target = _target_name(empresa, transportadora, campo)
    value = _read_windows_credential(target)
    if value is not None:
        return value
    return _memory_fallback.get(target)


def set_credential(empresa: str, transportadora: str, campo: str, valor: str) -> bool:
    """Store a credential. Falls back to process memory if the OS store fails."""
    target = _target_name(empresa, transportadora, campo)
    value = str(valor or "")
    if not value:
        return False
    if _write_windows_credential(target, value):
        return True
    _memory_fallback[target] = value
    _warn("Usando fallback em memória para credencial %s", _redact_target(target))
    return False


def _is_sensitive_field(campo: Any) -> bool:
    return str(campo or "").strip().lower() in _SENSITIVE_FIELDS


def _iter_provider_sections(config: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    transportadoras = config.get("transportadoras", {})
    if isinstance(transportadoras, dict):
        for nome, section in transportadoras.items():
            if isinstance(section, dict):
                yield str(nome), section

    for nome, section in config.items():
        if str(nome).strip().lower() in _CONFIG_CONTAINER_SECTIONS:
            continue
        if isinstance(section, dict):
            yield str(nome), section


def migrate_plaintext_credentials(config: dict[str, Any], empresa: str) -> dict[str, Any]:
    """Copy plaintext sensitive fields to secure storage without mutating config."""
    if not isinstance(config, dict):
        return config
    for transportadora, section in _iter_provider_sections(config):
        for campo, valor in section.items():
            if not _is_sensitive_field(campo):
                continue
            valor_txt = str(valor or "").strip()
            if valor_txt:
                set_credential(empresa, transportadora, str(campo), valor_txt)
    return config


def overlay_secure_credentials(config: dict[str, Any], empresa: str) -> dict[str, Any]:
    """Return a copy of config with secure credentials overlaid into provider sections."""
    if not isinstance(config, dict):
        return config
    merged = copy.deepcopy(config)
    for transportadora, section in _iter_provider_sections(merged):
        for campo in _SENSITIVE_FIELDS:
            valor = get_credential(empresa, transportadora, campo)
            if valor is not None and str(valor) != "":
                section[campo] = valor
    return merged
