"""Geographic and platform compliance checking.

Section 38: Checks whether platform access/trading is permitted
in the user's jurisdiction using official platform information.

The research system may continue collecting publicly available data where lawful.
This module does NOT implement methods to bypass geographic restrictions.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JurisdictionRule:
    """A single compliance rule for a jurisdiction."""

    jurisdiction: str
    can_collect_public_data: bool
    can_trade: bool
    can_paper_trade: bool
    notes: str = ""
    source_url: str = ""


# Known jurisdiction rules based on publicly available Polymarket information.
# This is NOT legal advice. Users must verify independently.
# Source: https://docs.polymarket.com/#get-started
KNOWN_RULES: dict[str, JurisdictionRule] = {
    "US": JurisdictionRule(
        jurisdiction="US",
        can_collect_public_data=True,
        can_trade=False,
        can_paper_trade=True,
        notes=(
            "Polymarket is not available to US persons for trading. "
            "Public data collection is permitted."
        ),
        source_url="https://docs.polymarket.com",
    ),
    "UK": JurisdictionRule(
        jurisdiction="UK",
        can_collect_public_data=True,
        can_trade=False,
        can_paper_trade=True,
        notes=(
            "Polymarket is not available to UK residents for trading. "
            "Public data collection is permitted."
        ),
        source_url="https://docs.polymarket.com",
    ),
    "SANCTIONED": JurisdictionRule(
        jurisdiction="SANCTIONED",
        can_collect_public_data=False,
        can_trade=False,
        can_paper_trade=False,
        notes="Sanctioned jurisdictions have no access.",
        source_url="https://docs.polymarket.com",
    ),
    "DEFAULT": JurisdictionRule(
        jurisdiction="DEFAULT",
        can_collect_public_data=True,
        can_trade=True,
        can_paper_trade=True,
        notes=(
            "Default: assume permitted. User must verify against current platform terms of service."
        ),
        source_url="https://docs.polymarket.com",
    ),
}


@dataclass
class ComplianceCheck:
    """Result of a compliance check."""

    jurisdiction: str
    can_collect: bool
    can_trade: bool
    can_paper_trade: bool
    mode: str  # "research_only", "paper_trading", "full_trading", "blocked"
    warnings: list[str] = field(default_factory=list)
    rule_source: str = ""


def check_jurisdiction(jurisdiction: str) -> ComplianceCheck:
    """Check compliance for a given jurisdiction.

    Args:
        jurisdiction: ISO country code or region identifier.

    Returns:
        ComplianceCheck with permitted modes and warnings.
    """
    upper = jurisdiction.upper().strip()
    rule = KNOWN_RULES.get(upper, KNOWN_RULES["DEFAULT"])

    warnings = []
    if upper not in KNOWN_RULES:
        warnings.append(
            f"Unknown jurisdiction '{jurisdiction}'. "
            "Using default rules. User must verify compliance independently."
        )
    if rule.notes:
        warnings.append(rule.notes)

    if not rule.can_collect_public_data and not rule.can_paper_trade:
        mode = "blocked"
    elif not rule.can_trade:
        mode = "paper_trading" if rule.can_paper_trade else "research_only"
    else:
        mode = "full_trading"

    return ComplianceCheck(
        jurisdiction=upper,
        can_collect=rule.can_collect_public_data,
        can_trade=rule.can_trade,
        can_paper_trade=rule.can_paper_trade,
        mode=mode,
        warnings=warnings,
        rule_source=rule.source_url,
    )


def system_mode(jurisdiction: str) -> str:
    """Return the operating mode string for the system.

    Returns one of:
    - "research_only": collect data, no trading, no paper trading
    - "paper_trading": collect data + paper trading, no real trading
    - "full_trading": all capabilities (still research/paper system)
    - "blocked": no operations permitted
    """
    return check_jurisdiction(jurisdiction).mode


def enforce_paper_only(jurisdiction: str) -> bool:
    """Return True if the system must remain paper-only for this jurisdiction."""
    check = check_jurisdiction(jurisdiction)
    return not check.can_trade or check.mode in ("research_only", "paper_trading", "blocked")
