"""Forgejo access for the development update channel, kept in a secure store.

The access token never enters ``config.json``, the update state or logs. On
Windows it lives in the Credential Manager of the user that runs FlowShift
(generic credential ``FlowShift:forgejo-update``, protected by DPAPI). Other
platforms have no secure store here; tests inject :class:`MemoryCredentialStore`.
"""
from __future__ import annotations

import ctypes
import sys
import threading


CREDENTIAL_TARGET = "FlowShift:forgejo-update"
CREDENTIAL_USER = "flowshift-update"
MIN_TOKEN_LENGTH = 8
MAX_TOKEN_LENGTH = 512

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class CredentialError(OSError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_token(value):
    """Accept one printable ASCII token without whitespace; return it unchanged."""
    if not isinstance(value, str):
        raise CredentialError("invalid_token", "Access token must be text")
    token = value.strip()
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        raise CredentialError("invalid_token", "Access token length is invalid")
    if any(not 33 <= ord(character) <= 126 for character in token):
        raise CredentialError("invalid_token", "Access token contains invalid characters")
    return token


class MemoryCredentialStore:
    """Process-local store for tests and platforms without a secure store."""

    supported = False

    def __init__(self, token=None):
        self._lock = threading.Lock()
        self._token = token

    def read(self):
        with self._lock:
            return self._token

    def write(self, token):
        token = validate_token(token)
        with self._lock:
            self._token = token

    def delete(self):
        with self._lock:
            existed = self._token is not None
            self._token = None
        return existed


class WindowsCredentialStore:
    """Generic Windows credential owned by the user running FlowShift."""

    supported = True

    def __init__(self, target=CREDENTIAL_TARGET):
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._target = target
        self._credential_type = CREDENTIALW
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self._read = advapi.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self._read.restype = wintypes.BOOL
        self._write = advapi.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._delete = advapi.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = advapi.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def read(self):
        pointer = ctypes.POINTER(self._credential_type)()
        if not self._read(self._target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise CredentialError("credential_store_error", f"CredReadW failed: {error}")
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            self._free(pointer)
        try:
            return validate_token(blob.decode("utf-8"))
        except (UnicodeDecodeError, CredentialError):
            return None

    def write(self, token):
        blob = validate_token(token).encode("utf-8")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self._credential_type()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target
        credential.Comment = "FlowShift development updates (Forgejo)"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = CREDENTIAL_USER
        if not self._write(ctypes.byref(credential), 0):
            raise CredentialError("credential_store_error",
                                  f"CredWriteW failed: {ctypes.get_last_error()}")

    def delete(self):
        if self._delete(self._target, _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise CredentialError("credential_store_error", f"CredDeleteW failed: {error}")


def default_store():
    if sys.platform == "win32":
        try:
            return WindowsCredentialStore()
        except (AttributeError, OSError):
            pass
    return MemoryCredentialStore()


def token_provider(store):
    """Return a callable that reads the token at request time and never raises."""
    def provide():
        try:
            return store.read()
        except (CredentialError, OSError):
            return None
    return provide


def access_status(store):
    """Report only whether a token exists; the token itself is never returned."""
    try:
        configured = store.read() is not None
        error = None
    except (CredentialError, OSError) as exc:
        configured = False
        error = getattr(exc, "code", "credential_store_error")
    return {
        "configured": configured,
        "secure_store": bool(getattr(store, "supported", False)),
        "error": error,
    }
