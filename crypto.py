"""
crypto.py — Fernet-encryptie voor Odoo API-sleutels
====================================================
De encryptie-sleutel (SECRET_KEY) leeft UITSLUITEND als omgevingsvariabele,
nooit in de database of code.

Lokale ontwikkeling: als SECRET_KEY ontbreekt, wordt een sleutel aangemaakt
in .secret_key (niet in Git). Dit bestand is automatisch toegevoegd aan .gitignore.

Productie (Render.com): stel SECRET_KEY in als environment variable.
"""

import os
import base64

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILE = os.path.join(os.path.dirname(__file__), ".secret_key")
_fernet: Fernet | None = None


def _is_production() -> bool:
    # Fail-closed: productie tenzij expliciet als dev gemarkeerd (zie config_loader).
    from config_loader import IS_PRODUCTION
    return IS_PRODUCTION


def _load_or_create_key() -> bytes:
    """Laad SECRET_KEY uit env var. Lokale fallback: .secret_key bestand."""
    raw = os.environ.get("SECRET_KEY", "").strip()
    if raw:
        try:
            key = raw.encode()
            Fernet(key)   # validate
            return key
        except Exception as exc:
            # Een gezette-maar-ongeldige SECRET_KEY is een misconfiguratie. In
            # productie NOOIT stil terugvallen op een gegenereerde sleutel: dat
            # maakt alle bestaande versleutelde tokens/TOTP-geheimen onleesbaar.
            if _is_production():
                raise RuntimeError(
                    "SECRET_KEY is gezet maar is geen geldige Fernet-sleutel. "
                    "Genereer er een met crypto.generate_new_key() en stel die in."
                ) from exc
            # Lokaal mag het wel doorvallen naar de .secret_key-fallback.

    if _is_production():
        # In productie verplicht een geldige SECRET_KEY — nooit een nieuwe
        # genereren op een ephemeral container (zou elke boot wisselen).
        raise RuntimeError(
            "SECRET_KEY ontbreekt in productie. Stel deze in als environment variable "
            "(zie crypto.generate_new_key())."
        )

    # Lokaal: laad uit bestand of genereer nieuw
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read().strip()

    # Genereer nieuwe sleutel (0600: alleen leesbaar voor de eigenaar)
    key = Fernet.generate_key()
    fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    print(
        "[CRYPTO] ⚠️  Nieuwe SECRET_KEY gegenereerd en opgeslagen in .secret_key\n"
        "         Stel in productie altijd SECRET_KEY=... in als env var!"
    )
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_api_key(plaintext: str) -> str:
    """Versleutel een Odoo API-sleutel. Geeft base64-token terug."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(token: str) -> str:
    """Ontsleutel een eerder versleuteld token. Geeft plaintext API-sleutel terug."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "API-sleutel kon niet worden ontsleuteld. Controleer of SECRET_KEY correct is."
        ) from exc


def encrypt_totp_secret(plaintext: str) -> str:
    """Versleutel een TOTP-geheim (base32 string). Geeft Fernet-token terug."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_totp_secret(token: str) -> str:
    """Ontsleutel een eerder versleuteld TOTP-geheim. Geeft base32 string terug."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "TOTP-geheim kon niet worden ontsleuteld. Controleer of SECRET_KEY correct is."
        ) from exc


def encrypt_text(plaintext: str) -> str:
    """Versleutel willekeurige tekst (bv. klantgegevens van de PWN-module)."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_text(token: str) -> str:
    """Ontsleutel tekst die met encrypt_text is versleuteld."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Gegevens konden niet worden ontsleuteld. Controleer of SECRET_KEY correct is."
        ) from exc


def is_totp_encrypted(value: str) -> bool:
    """
    Detecteer of een TOTP-waarde al Fernet-versleuteld is.
    Fernet-tokens beginnen altijd met 'gAAAAA' (base64 header).
    Onversleutelde TOTP-geheimen zijn base32 (A-Z2-7, geen kleine letters).
    """
    return value.startswith("gAAAAA")


def rotate_key(old_plaintext_key: bytes, new_plaintext_key: bytes, tokens: list[str]) -> list[str]:
    """
    Key-rotation: ontsleutel tokens met de oude sleutel en versleutel opnieuw
    met de nieuwe sleutel. Roep aan bij wijziging van SECRET_KEY.
    """
    old_f = Fernet(old_plaintext_key)
    new_f = Fernet(new_plaintext_key)
    return [new_f.encrypt(old_f.decrypt(t.encode())).decode() for t in tokens]


def generate_new_key() -> str:
    """Hulpfunctie: genereer een nieuwe Fernet-sleutel (voor key-rotation)."""
    return Fernet.generate_key().decode()
