from .aggregate import DiscoveryResult, build_providers, discover_emails
from .base import (
    DiscoveryContext,
    EmailCandidate,
    EmailProvider,
    classify_role,
    is_valid_email,
)
from .patterns import PatternProvider, generate_for_person, split_name
from .verify import resolve_mx, smtp_probe
from .website_provider import WebsiteProvider

__all__ = [
    "DiscoveryContext",
    "DiscoveryResult",
    "EmailCandidate",
    "EmailProvider",
    "PatternProvider",
    "WebsiteProvider",
    "build_providers",
    "discover_emails",
    "classify_role",
    "is_valid_email",
    "generate_for_person",
    "split_name",
    "resolve_mx",
    "smtp_probe",
]
