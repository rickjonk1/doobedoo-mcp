"""
utils.py — Gedeelde hulpfuncties
"""
from datetime import datetime
from zoneinfo import ZoneInfo

_AMS = ZoneInfo("Europe/Amsterdam")


def nl_now() -> datetime:
    """Huidige datum/tijd in Nederlandse tijdzone (CET/CEST), timezone-naïef."""
    return datetime.now(_AMS).replace(tzinfo=None)
