"""
Encryption for the fields the brief marks sensitive, and the placeholder layer
that keeps them away from the language model.

The schema marks two fields sensitive: `account_number` and `utr_number`. It
marks `transaction_reference_id` explicitly plaintext and leaves `description`
unmarked. We follow that classification rather than inventing our own.

  utr_number      arrives ALREADY ENCRYPTED (base64, 42-48 bytes decoded). We
                  hold no key, so there is nothing to encrypt and no way to
                  decrypt. Stored as received, shown masked.

  account_number  STORED IN PLAINTEXT, encrypted only at the boundary where rows
                  are handed to the language model, and decrypted again for the
                  final render. The model never sees a real account number.

                  The tradeoff this accepts: at rest the value is readable, so a
                  database dump, a replica or a logged SELECT * exposes it. The
                  protection here is against the MODEL, not against the disk.

WHY AES-SIV
-----------
AES-256-SIV (RFC 5297) is a DETERMINISTIC authenticated cipher: the same
plaintext always produces the same ciphertext under the same key. That is what
makes the column searchable - `WHERE account_number = encrypt(value)` works,
which a randomized cipher cannot do. The brief warns about exactly this:
"an encrypted column can't be searched with a plain WHERE =".

The trade is that equal plaintexts are visibly equal in the ciphertext. For an
account number, whose purpose is to identify an account, that is not a
meaningful loss. It would be for a low-cardinality column like a status, which
is why this is applied per-column and not as a blanket policy.

SIV is also misuse-resistant: there is no nonce to reuse, which is the failure
mode visible in the upstream utr_number export, where every value shares an
identical 16-byte ciphertext prefix.

THE BOUNDARY LAYER
------------------
Rows handed to the model carry CIPHERTEXT in place of the account number; real
values are substituted back only at the final render, after verification.

The ciphertext doubles as a stable pseudonym because AES-SIV is deterministic:
the same account is the same token every time, so the model can still say "this
account appears in three rows" correctly without ever seeing the number.

Ciphertext contains digits, unlike a synthetic `[[ACCT_A]]` marker, so
narrate.py strips these tokens from the text before running the numeric
provenance check - otherwise base64 digits would be mistaken for figures.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV

from api.config import settings

log = logging.getLogger("tbx.crypto")

# Columns whose RAW value must never reach the model, a log, or a stored
# message. The user may see account_number; the model may not.
SENSITIVE_COLUMNS = frozenset({"account_number", "utr_number"})

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
# A token is base64 ciphertext: >=24 chars of the base64 alphabet. Long enough
# not to collide with reference ids ("HDFCH01078329532") or ordinary words.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}")


class MissingKey(RuntimeError):
    pass


def _siv() -> AESSIV:
    """AES-256-SIV needs a 512-bit key; SENSITIVE_KEY is stretched to it.

    Derivation is a fixed SHA-512 over a labelled input, so the same env value
    always yields the same cipher key - required, because the ciphertext in the
    database has to stay readable across restarts.
    """
    key = (settings.sensitive_key or "").strip()
    if not key:
        raise MissingKey(
            "SENSITIVE_KEY is not set. Generate one with:\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"\n'
            "and put it in .env. It is never stored in the database."
        )
    if len(key) < 32:
        raise MissingKey("SENSITIVE_KEY must be at least 32 characters")
    return AESSIV(hashlib.sha512(b"tbx-account-number-v1|" + key.encode()).digest())


def normalize(value: str) -> str:
    """Strip formatting so the same account written two ways encrypts alike.

    Without this, "5020 0013 7290 69" and "50200013729069" would produce
    different ciphertext and an exact-match lookup would silently miss.
    """
    return _NON_ALNUM.sub("", value or "").upper()


def encrypt(value: str | None) -> str | None:
    """Plaintext -> base64 ciphertext. Deterministic, so it is searchable."""
    if not value:
        return None
    ct = _siv().encrypt(normalize(value).encode(), None)
    return base64.b64encode(ct).decode()


def decrypt(value: str | None) -> str | None:
    """Ciphertext -> plaintext. Returns None if it cannot be authenticated.

    A wrong key fails here rather than returning garbage, because SIV
    authenticates. That is the difference between a visible outage and silently
    showing the wrong customer's account number.
    """
    if not value:
        return None
    try:
        return _siv().decrypt(base64.b64decode(value), None).decode()
    except (InvalidTag, ValueError, TypeError) as exc:
        log.warning("could not decrypt an account_number: %s", type(exc).__name__)
        return None


def looks_encrypted(value: str | None) -> bool:
    """Cheap guard so a re-run of the migration cannot double-encrypt."""
    if not value:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) >= 16
    except Exception:  # noqa: BLE001
        return False


def mask(value: str | None) -> str:
    """Display form for anywhere the full value should not appear."""
    plain = normalize(value or "")
    return f"XXXXXX{plain[-4:]}" if len(plain) >= 4 else "—"


def mask_utr(value: str | None) -> str:
    """utr_number is already ciphertext; show only enough to distinguish rows."""
    return f"UTR-{value[-4:]}" if value else "—"


def key_fingerprint() -> str:
    """Safe to log or display. Confirms two environments share a key without
    revealing it - a mismatch otherwise shows up as unreadable ciphertext."""
    try:
        key = (settings.sensitive_key or "").strip()
        return hashlib.sha256(b"fingerprint|" + key.encode()).hexdigest()[:8] if key else "unset"
    except Exception:  # noqa: BLE001
        return "unset"


# --------------------------------------------------------------------------
# Placeholder layer
# --------------------------------------------------------------------------


@dataclass
class PlaceholderMap:
    """Per-request substitution table: ciphertext -> plaintext.

    Lives in memory only. Built fresh per request from the plaintext read out of
    the database, so nothing is cached across users.
    """

    to_plain: dict[str, str] = field(default_factory=dict)
    _seen: dict[str, str] = field(default_factory=dict)

    def placeholder_for(self, plaintext: str) -> str:
        """The token IS the ciphertext. Deterministic, so the same account gets
        the same token every time and repeated rows stay linkable."""
        if plaintext in self._seen:
            return self._seen[plaintext]
        token = encrypt(plaintext) or mask(plaintext)
        self._seen[plaintext] = token
        self.to_plain[token] = plaintext
        return token

    def __bool__(self) -> bool:
        return bool(self.to_plain)

    def resolve(self, text: str) -> str:
        """Substitute real values back in. Final render only."""
        for token in self.tokens():
            text = text.replace(token, self.to_plain[token])
        return text

    def tokens(self) -> list[str]:
        """Issued tokens, longest first so substitution cannot cut one in half."""
        return sorted(self.to_plain, key=len, reverse=True)

    def strip(self, text: str) -> str:
        """Remove issued tokens so the numeric provenance check does not read
        base64 digits as figures."""
        for token in self.tokens():
            text = text.replace(token, " ")
        return text

    def unknown_in(self, text: str) -> list[str]:
        """Account-shaped strings the model produced that were never issued.

        A token it invented is the same class of failure as an invented number.
        Anything base64-ish and long enough to be a token, that we did not hand
        over, is flagged.
        """
        issued = set(self.to_plain)
        return [m.group(0) for m in _TOKEN_RE.finditer(text)
                if m.group(0) not in issued]


def placeholderise(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> tuple[list[list[Any]], PlaceholderMap]:
    """Decrypt sensitive columns, then swap the plaintext for a placeholder.

    Returns rows safe to hand the model, plus the map needed to restore them.
    """
    pmap = PlaceholderMap()
    idx = [i for i, c in enumerate(columns) if c in SENSITIVE_COLUMNS]
    if not idx:
        return [list(r) for r in rows], pmap

    out: list[list[Any]] = []
    for row in rows:
        new = list(row)
        for i in idx:
            if new[i] is None:
                continue
            if columns[i] == "account_number":
                # Stored plaintext; encrypted here, on the way to the model.
                plain = str(new[i])
            else:
                plain = mask_utr(str(new[i]))
            new[i] = pmap.placeholder_for(plain)
        out.append(new)
    return out, pmap


def resolve_rows(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], pmap: PlaceholderMap
) -> list[list[Any]]:
    """Final render: placeholders back to real values, for the USER only."""
    if not pmap:
        return [list(r) for r in rows]
    idx = [i for i, c in enumerate(columns) if c in SENSITIVE_COLUMNS]
    out = []
    for row in rows:
        new = list(row)
        for i in idx:
            if isinstance(new[i], str):
                new[i] = pmap.resolve(new[i])
        out.append(new)
    return out


def assert_no_plaintext(
    rows: Iterable[Sequence[Any]], pmap: PlaceholderMap, where: str
) -> None:
    """Fail loudly if a decrypted value is about to reach somewhere it must not.

    Checked before the narration prompt and before anything is persisted. A mask
    that silently stops working looks completely normal until someone reads the
    output, which is precisely why this raises instead of logging.
    """
    if not pmap:
        return
    plaintexts = set(pmap.to_plain.values())
    for row in rows:
        for cell in row:
            if isinstance(cell, str) and cell in plaintexts:
                raise RuntimeError(
                    f"a decrypted account number reached {where}. "
                    f"Only placeholders may leave the request boundary."
                )
