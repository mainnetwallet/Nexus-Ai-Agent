"""
Encrypted local keystore for the hot-signer private key.

Replaces writing HOT_SIGNER_PRIVATE_KEY as plaintext into .env
(backend/wallet/hot_signer.py used to do this via persist_hot_signer_secret).
Same tradeoff and same scope as before -- this is still only appropriate for
a burner/bot wallet, never one holding real value -- but the secret now sits
on disk encrypted, not in cleartext.

Scheme: PBKDF2-HMAC-SHA256(passphrase, random 16-byte salt, 390_000 iters)
-> Fernet key -> Fernet.encrypt(private_key). File on disk holds only
`salt + token`; without the passphrase it's unrecoverable ciphertext.

The passphrase itself is NOT the secret -- it's the thing that unlocks the
secret. It still needs to come from somewhere at process start (env var
KEYSTORE_PASSPHRASE, or an interactive prompt); storing it separately from
the keystore file is what makes the split useful (e.g. keystore file can
safely be backed up/synced, the passphrase should not be).

The decrypted key only ever lives in memory for the lifetime of the process
(loaded once into settings.hot_signer_private_key, same as before) -- this
module does not change that part of the trust model, only how the secret
rests on disk between runs.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("nexus.wallet.keystore")

SALT_LEN = 16
KDF_ITERATIONS = 390_000


class KeystoreError(Exception):
    pass


class KeystoreLocked(KeystoreError):
    """Wrong passphrase, corrupted file, or file missing."""


def _derive_fernet(passphrase: str, salt: bytes) -> Fernet:
    if not passphrase:
        raise KeystoreError("Passphrase must not be empty.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    derived = kdf.derive(passphrase.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


@dataclass
class Keystore:
    """One encrypted secret per file. Call save() to write, load() to read."""

    path: Path

    def save(self, private_key: str, passphrase: str) -> None:
        """
        Encrypts `private_key` under `passphrase` and writes salt+token to
        self.path, overwriting any existing file. Sets file permissions to
        owner-read/write-only where the OS supports it.
        """
        salt = os.urandom(SALT_LEN)
        fernet = _derive_fernet(passphrase, salt)
        token = fernet.encrypt(private_key.encode("utf-8"))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(salt + token)
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass  # best-effort on filesystems that don't support chmod
            tmp_path.replace(self.path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        logger.info("Hot signer key encrypted and saved to %s (plaintext never written to disk)", self.path)

    def load(self, passphrase: str) -> str:
        """
        Decrypts and returns the private key. Raises KeystoreLocked if the
        file is missing, the passphrase is wrong, or the file is corrupted
        -- callers should treat all three the same way (refuse to proceed),
        not report which one it was, so a wrong-passphrase guess can't be
        distinguished from a missing file by an attacker probing the CLI.
        """
        if not self.path.exists():
            raise KeystoreLocked(f"No keystore file at {self.path}.")

        data = self.path.read_bytes()
        if len(data) <= SALT_LEN:
            raise KeystoreLocked("Keystore file is corrupted or empty.")

        salt, token = data[:SALT_LEN], data[SALT_LEN:]
        fernet = _derive_fernet(passphrase, salt)
        try:
            plaintext = fernet.decrypt(token)
        except InvalidToken as exc:
            raise KeystoreLocked("Wrong passphrase or corrupted keystore file.") from exc
        return plaintext.decode("utf-8")

    def exists(self) -> bool:
        return self.path.exists()

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()
            logger.info("Deleted keystore file at %s", self.path)


def get_passphrase_noninteractive() -> str:
    """
    Reads the passphrase from KEYSTORE_PASSPHRASE only. Never prompts.

    Use this from any code path that might run inside a request handler
    (REST route, chat engine turn, etc.) -- prompting on stdin from a
    server process either hangs the event loop forever (no TTY) or, worse,
    blocks every other request behind it (if there is one). Raises
    KeystoreError if the env var isn't set, so the caller can turn that
    into a clear 4xx/chat error instead of hanging.
    """
    env_val = os.environ.get("KEYSTORE_PASSPHRASE")
    if not env_val:
        raise KeystoreError(
            "KEYSTORE_PASSPHRASE is not set. Set it in the environment before "
            "starting the server -- API/chat requests never prompt interactively."
        )
    return env_val


def get_passphrase_interactive(prompt: str = "Keystore passphrase: ") -> str:
    """
    CLI-only helper: reads KEYSTORE_PASSPHRASE if set, otherwise prompts
    interactively without echoing input. Only call this from a script you
    are running yourself in a terminal (e.g. a one-off setup command) --
    never from an API route or the chat engine.
    """
    env_val = os.environ.get("KEYSTORE_PASSPHRASE")
    if env_val:
        return env_val
    import getpass
    return getpass.getpass(prompt)
