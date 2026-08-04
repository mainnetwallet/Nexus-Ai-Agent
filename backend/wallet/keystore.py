"""
Encrypted local keystore for hot-signer private keys.

Replaces writing HOT_SIGNER_PRIVATE_KEY as plaintext into .env
(backend/wallet/hot_signer.py used to do this via persist_hot_signer_secret).
Same tradeoff and same scope as before -- this is still only appropriate for
burner/bot wallets, never one holding real value -- but secrets now sit on
disk encrypted, not in cleartext.

Scheme: PBKDF2-HMAC-SHA256(passphrase, random 16-byte salt, 390_000 iters)
-> Fernet key -> Fernet.encrypt(json-encoded entries). File on disk holds
only `salt + token`; without the passphrase it's unrecoverable ciphertext.

MULTI-KEY: the file holds a dict of entries keyed by an id (the hot
signer's checksum address, in practice) -> {"private_key": "0x...",
"label": "..."}. All entries share one salt/passphrase and are
encrypted/decrypted together as a single JSON blob -- adding or removing
one entry re-encrypts the whole (small) blob, it does not touch the
others' bytes on disk, but it does mean every entry needs the same
passphrase. That's an acceptable tradeoff for the burner-wallet use case
this module exists for.

LEGACY FORMAT: keystores written before multi-key support hold a single
raw private-key string as the entire decrypted plaintext (no JSON
envelope). load_keys() detects this (JSON parsing fails) and surfaces it
under the sentinel id LEGACY_ENTRY_ID so callers (hot_signer.py's
unlock_hot_signer) can migrate it to a real address-keyed entry the first
time it's unlocked.

The passphrase itself is NOT the secret -- it's the thing that unlocks the
secret. It still needs to come from somewhere at process start (env var
KEYSTORE_PASSPHRASE, or an interactive prompt); storing it separately from
the keystore file is what makes the split useful (e.g. keystore file can
safely be backed up/synced, the passphrase should not be).

Decrypted keys only ever live in memory for the lifetime of the process
(loaded once into settings.hot_signer_keys / settings.hot_signer_private_key)
-- this module does not change that part of the trust model, only how
secrets rest on disk between runs.
"""
from __future__ import annotations

import base64
import json
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

# Sentinel id used for a decrypted blob that turned out to be the old
# single-secret raw-string format rather than the current JSON dict of
# entries. Never a real address, so it can't collide with one.
LEGACY_ENTRY_ID = "__legacy__"


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
    """
    One encrypted file holding a dict of entries: {entry_id: {"private_key":
    "0x...", "label": "..."}}. entry_id is the hot signer's checksum
    address in practice, but this module treats it as an opaque string.
    """

    path: Path

    def _write_entries(self, entries: dict, passphrase: str) -> None:
        """Encrypts `entries` (a dict) under `passphrase` and writes
        salt+token to self.path, overwriting any existing file. Sets file
        permissions to owner-read/write-only where the OS supports it."""
        salt = os.urandom(SALT_LEN)
        fernet = _derive_fernet(passphrase, salt)
        token = fernet.encrypt(json.dumps(entries).encode("utf-8"))

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

        logger.info(
            "Hot signer keystore written to %s (%d key(s), plaintext never written to disk)",
            self.path, len(entries),
        )

    def load_keys(self, passphrase: str) -> dict:
        """
        Decrypts and returns all entries as {entry_id: {"private_key": ...,
        "label": ...}}. Raises KeystoreLocked if the file is missing, the
        passphrase is wrong, or the file is corrupted -- callers should
        treat all three the same way (refuse to proceed), not report which
        one it was, so a wrong-passphrase guess can't be distinguished from
        a missing file by an attacker probing the CLI.

        A pre-multi-key file (single raw secret, no JSON envelope) decrypts
        fine but fails JSON parsing -- that's surfaced as one entry under
        LEGACY_ENTRY_ID rather than raised as an error, so callers can
        migrate it instead of treating it as corruption.
        """
        if not self.path.exists():
            raise KeystoreLocked(f"No keystore file at {self.path}.")

        data = self.path.read_bytes()
        if len(data) <= SALT_LEN:
            raise KeystoreLocked("Keystore file is corrupted or empty.")

        salt, token = data[:SALT_LEN], data[SALT_LEN:]
        fernet = _derive_fernet(passphrase, salt)
        try:
            plaintext = fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise KeystoreLocked("Wrong passphrase or corrupted keystore file.") from exc

        try:
            parsed = json.loads(plaintext)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            return parsed

        # Legacy single-secret file: the whole decrypted plaintext IS the key.
        return {LEGACY_ENTRY_ID: {"private_key": plaintext, "label": "legacy"}}

    def add_key(self, entry_id: str, private_key: str, passphrase: str, label: Optional[str] = None) -> None:
        """
        Adds or overwrites the single entry `entry_id`, leaving every other
        entry already in the keystore untouched. This is the fix for the
        old single-secret behavior where saving a second key silently
        discarded the first -- multiple hot signers can now coexist in one
        file.
        """
        try:
            entries = self.load_keys(passphrase)
        except KeystoreLocked:
            if self.path.exists():
                raise  # wrong passphrase / corrupted -- don't silently clobber it
            entries = {}
        entries.pop(LEGACY_ENTRY_ID, None)  # migrating in a real entry supersedes the legacy blob
        entries[entry_id] = {"private_key": private_key, "label": label}
        self._write_entries(entries, passphrase)

    def remove_key(self, entry_id: str, passphrase: str) -> bool:
        """Removes one entry by id. Returns False if it wasn't present."""
        entries = self.load_keys(passphrase)
        if entry_id not in entries:
            return False
        del entries[entry_id]
        self._write_entries(entries, passphrase)
        return True

    def replace_all(self, entries: dict, passphrase: str) -> None:
        """Rewrites the whole file with exactly `entries`. Used by the
        legacy -> multi-key migration in hot_signer.unlock_hot_signer, which
        needs to move LEGACY_ENTRY_ID's content under a real address in one
        atomic write."""
        self._write_entries(entries, passphrase)

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
