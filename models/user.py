"""
models/user.py — Gebruikersmodel voor authenticatie
=====================================================
Flask-Login integratie met bcrypt wachtwoord-hashing.
Rollen: 'admin' (gebruikers, databases en koppelingen beheren) en
'gebruiker' (de MCP-server koppelen en gebruiken).
Brute-force beveiliging via failed_logins + locked_until.
"""

from datetime import datetime, timedelta
from utils import nl_now
from flask_login import UserMixin
from database import db


LOCKOUT_THRESHOLD = 5        # Aantal mislukte pogingen voor lockout
LOCKOUT_DURATION_MINUTES = 15


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id             = db.Column(db.Integer, primary_key=True)
    email          = db.Column(db.String(150), unique=True, nullable=False)
    name           = db.Column(db.String(150))                                   # volledige naam
    password_hash  = db.Column(db.String(255), nullable=False)
    role           = db.Column(db.String(20), default="gebruiker", nullable=False)  # gebruiker / admin
    active         = db.Column(db.Boolean, default=True, nullable=False)
    failed_logins  = db.Column(db.Integer, default=0, nullable=False)
    locked_until   = db.Column(db.DateTime)
    last_login_at  = db.Column(db.DateTime)
    created_at     = db.Column(db.DateTime, default=nl_now)
    totp_secret    = db.Column(db.String(200))                                   # TOTP geheim (Fernet-versleuteld)
    totp_enabled   = db.Column(db.Boolean, default=False, nullable=False)        # 2FA actief
    last_ip        = db.Column(db.String(45))                                    # IPv4/IPv6 bij laatste login
    # Uitnodiging / wachtwoord instellen: eenmalige link, alleen de hash bewaard.
    uitnodiging_hash = db.Column(db.String(64), index=True)
    uitnodiging_tot  = db.Column(db.DateTime)

    def is_active(self) -> bool:  # type: ignore[override]
        return self.active

    def is_locked(self) -> bool:
        if self.locked_until and nl_now() < self.locked_until:
            return True
        return False

    def record_failed_login(self) -> None:
        self.failed_logins += 1
        if self.failed_logins >= LOCKOUT_THRESHOLD:
            self.locked_until = nl_now() + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
        db.session.commit()

    def record_successful_login(self, ip: str | None = None) -> None:
        self.failed_logins = 0
        self.locked_until  = None
        self.last_login_at = nl_now()
        if ip:
            self.last_ip = ip
        db.session.commit()

    def is_admin(self) -> bool:
        return self.role == "admin"

    def has_role(self, *roles: str) -> bool:
        """Geeft True als de gebruiker één van de opgegeven rollen heeft."""
        return self.role in roles

    def __repr__(self) -> str:
        return f"<User {self.email} [{self.role}]>"
