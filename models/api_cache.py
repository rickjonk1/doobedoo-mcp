"""
models/api_cache.py — kleine sleutel/waarde-cache (o.a. OCR-uitkomsten van de MCP).
"""
import json
from datetime import timedelta

from database import db
from utils import nl_now

TTL_HOURS = 24


class ApiResponseCache(db.Model):
    __tablename__ = "api_response_cache"

    id            = db.Column(db.Integer, primary_key=True)
    source        = db.Column(db.String(16), nullable=False, default="mcp")
    endpoint      = db.Column(db.String(50), nullable=False)
    extra_key     = db.Column(db.String(128), nullable=True)
    response_json = db.Column(db.Text, nullable=False)
    cached_at     = db.Column(db.DateTime, nullable=False, default=nl_now)

    __table_args__ = (
        db.Index("ix_api_cache_lookup", "source", "endpoint", "extra_key"),
    )

    def is_fresh(self, ttl_hours: int | None = None) -> bool:
        ttl = ttl_hours if ttl_hours is not None else TTL_HOURS
        return (nl_now() - self.cached_at) < timedelta(hours=ttl)

    def get_data(self) -> dict:
        return json.loads(self.response_json)

    def set_data(self, data: dict) -> None:
        self.response_json = json.dumps(data, ensure_ascii=False, default=str)
        self.cached_at = nl_now()
