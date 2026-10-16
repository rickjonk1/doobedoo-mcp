"""
models/company.py — CompanyOdoo
================================
Een geregistreerde Odoo-administratie: één rij per (database, company_id).
De API-sleutel wordt Fernet-versleuteld opgeslagen (crypto.py) en gaat nooit
terug naar de browser.
"""
from crypto import decrypt_api_key, encrypt_api_key
from database import db
from utils import nl_now


class CompanyOdoo(db.Model):
    __tablename__ = "companies_odoo"

    id           = db.Column(db.Integer, primary_key=True)
    db_name      = db.Column(db.String(100), nullable=False)
    db_url       = db.Column(db.String(255), nullable=False)
    db_user      = db.Column(db.String(150), nullable=False)
    db_key_enc   = db.Column(db.Text, nullable=False)
    company_id   = db.Column(db.Integer, nullable=False)
    company_name = db.Column(db.String(200), nullable=False)
    active       = db.Column(db.Boolean, default=True, nullable=False)
    created_at   = db.Column(db.DateTime, default=nl_now)
    updated_at   = db.Column(db.DateTime, default=nl_now, onupdate=nl_now)

    __table_args__ = (
        db.UniqueConstraint("db_name", "company_id", name="uq_company_db_cid"),
    )

    @classmethod
    def voor_mcp(cls):
        """Query met de administraties die de MCP-server mag gebruiken."""
        return cls.query.filter(cls.active.is_(True))

    def set_api_key(self, plaintext: str) -> None:
        self.db_key_enc = encrypt_api_key(plaintext)

    def get_api_key(self) -> str:
        return decrypt_api_key(self.db_key_enc)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "db_name":      self.db_name,
            "db_url":       self.db_url,
            "db_user":      self.db_user,
            "company_id":   self.company_id,
            "company_name": self.company_name,
            "active":       bool(self.active),
            "created_at":   self.created_at.strftime("%d-%m-%Y") if self.created_at else "",
        }

    def __repr__(self) -> str:
        return f"<CompanyOdoo {self.company_name} ({self.db_name})>"
