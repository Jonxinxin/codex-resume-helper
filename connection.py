"""Receive Pebrel's explicit environment handoff; never scan processes or ports."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


CONNECTION_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexResumeHelper" / "connection.json"


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def unprotect(ciphertext: str) -> str:
    """Decrypt only in the Windows account that registered the connection."""
    raw = base64.b64decode(ciphertext, validate=True)
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("无法解密当前用户的 Pebrel 连接登记")
    try:
        return ctypes.string_at(result.data, result.size).decode("utf-8")
    finally:
        kernel.LocalFree(result.data)


def runtime_environment(cli: str) -> dict[str, str]:
    env = dict(os.environ)
    if env.get("PEBREL_RUNTIME_ENDPOINT"):
        return env
    try:
        record = json.loads(CONNECTION_FILE.read_text(encoding="utf-8-sig"))
        if record.get("version") != 1:
            return env
        if os.path.normcase(os.path.abspath(record.get("cli", ""))) != os.path.normcase(os.path.abspath(cli)):
            return env
        endpoint = unprotect(record["protected_endpoint"])
        if endpoint and "\n" not in endpoint and "\r" not in endpoint:
            env["PEBREL_RUNTIME_ENDPOINT"] = endpoint
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # A missing/expired registration is reported by the normal CLI probe.
        pass
    return env
