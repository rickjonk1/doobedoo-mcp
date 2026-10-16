"""Alle modellen, zodat db.create_all() ze kent."""
from models.api_cache import ApiResponseCache  # noqa: F401
from models.app_config import AppConfig  # noqa: F401
from models.company import CompanyOdoo  # noqa: F401
from models.partner_client import OAuthAuthCode, PartnerApiLog, PartnerClient, PartnerToken  # noqa: F401
from models.user import User  # noqa: F401
