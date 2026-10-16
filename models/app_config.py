"""
models/app_config.py — Dynamische applicatieconfiguratie
==========================================================
Vervangt de statische ALLOWED_IPS en EMAIL_CONFIG omgevingsvariabelen.
Sleutels: 'allowed_ips', 'email_smtp', 'fallback_email'
"""

from datetime import datetime
from utils import nl_now
from database import db


class AppConfig(db.Model):
    __tablename__ = "app_config"

    id          = db.Column(db.Integer, primary_key=True)
    config_key  = db.Column(db.String(100), unique=True, nullable=False)
    config_val  = db.Column(db.Text, nullable=False)   # JSON of plaintext
    updated_at  = db.Column(db.DateTime, default=nl_now, onupdate=nl_now)
    updated_by  = db.Column(db.String(100))

    @classmethod
    def get(cls, key: str, default=None):
        """Haal een configuratiewaarde op als string."""
        record = cls.query.filter_by(config_key=key).first()
        return record.config_val if record else default

    @classmethod
    def set(cls, key: str, value: str, updated_by: str = None) -> None:
        """Sla een configuratiewaarde op (upsert)."""
        record = cls.query.filter_by(config_key=key).first()
        if record:
            record.config_val = value
            record.updated_by = updated_by
            record.updated_at = nl_now()
        else:
            record = cls(config_key=key, config_val=value, updated_by=updated_by)
            db.session.add(record)
        db.session.commit()

    def __repr__(self) -> str:
        return f"<AppConfig {self.config_key}>"
