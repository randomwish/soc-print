"""Password storage in the operating system's keyring.

On Linux this is the Secret Service (GNOME Keyring / KWallet), on macOS the
Keychain, on Windows the Credential Manager. The password is encrypted at rest
by the OS and unlocked with the desktop login; it is never written to a file by
this app.
"""

from __future__ import annotations

import os

import keyring
import keyring.backend
import keyring.errors
from keyring.backends import chainer, fail, null

SERVICE = "soc-print"

FRIENDLY = {
    "SecretService": "GNOME Keyring / KWallet",
    "libsecret": "GNOME Keyring / KWallet",
    "kwallet": "KWallet",
    "macOS": "macOS Keychain",
    "Windows": "Windows Credential Manager",
}


def _pick_backend():
    """The operating system's own keyring, or None.

    keyring's default "chainer" would quietly fall back to any other installed
    backend, including plaintext-file ones, so the choice is made explicitly.
    """
    if os.environ.get("PYTHON_KEYRING_BACKEND"):
        return keyring.get_keyring()  # an explicit override, used by the tests
    placeholders = (chainer.ChainerBackend, fail.Keyring, null.Keyring)
    native = [
        backend for backend in keyring.backend.get_all_keyring()
        if type(backend).__module__.startswith("keyring.backends.")
        and not isinstance(backend, placeholders) and backend.priority > 0
    ]
    return max(native, key=lambda backend: backend.priority, default=None)


class PasswordStore:
    def __init__(self) -> None:
        try:
            self._backend = _pick_backend()
        except Exception:  # a broken backend must not stop the app from starting
            self._backend = None
        self.available = self._backend is not None
        self.name = ""
        if self.available:
            module = type(self._backend).__module__.rsplit(".", 1)[-1]
            self.name = FRIENDLY.get(module, getattr(self._backend, "name", module))

    def get(self, user: str) -> str | None:
        if not self.available or not user:
            return None
        try:
            return self._backend.get_password(SERVICE, user)
        except keyring.errors.KeyringError:
            return None

    def set(self, user: str, password: str) -> bool:
        if not self.available:
            return False
        try:
            self._backend.set_password(SERVICE, user, password)
            return True
        except keyring.errors.KeyringError:
            return False

    def delete(self, user: str) -> None:
        if not self.available or not user:
            return
        try:
            self._backend.delete_password(SERVICE, user)
        except keyring.errors.KeyringError:
            pass  # nothing stored, or the keyring is locked
