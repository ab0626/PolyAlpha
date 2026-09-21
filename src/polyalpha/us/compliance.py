"""Polymarket US venue compliance / eligibility model.

Separate from the International compliance module (``polyalpha.compliance``).
Polymarket US (gateway/api.polymarket.us, Direct Exchange) is a CFTC-regulated
venue; US persons are generally eligible to trade there, unlike on Polymarket
International. Eligibility still depends on account onboarding (KYC/AML),
state-level restrictions, and product-specific availability — none of which
this module can verify on its own.

This module answers one question: "is this jurisdiction eligible to trade on
the US venue, and has an operator independently confirmed it?" It never
implements jurisdiction bypass, falsified location, or restriction
circumvention. It is NOT legal advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UsVenueRule:
    """Baseline eligibility for the US venue, per jurisdiction."""

    jurisdiction: str
    eligible: bool
    notes: str = ""
    source_url: str = ""


# Baseline rules for the Polymarket US venue. "Eligible" is NOT "confirmed":
# an operator must still verify account/KYC/state/product eligibility before
# trading. "SANCTIONED" mirrors the International module and blocks everything.
US_VENUE_RULES: dict[str, UsVenueRule] = {
    "US": UsVenueRule(
        jurisdiction="US",
        eligible=True,
        notes=(
            "Polymarket US is CFTC-regulated. US persons are generally eligible, "
            "subject to KYC/AML, account approval, and state/product restrictions. "
            "Verify independently; this is not legal advice."
        ),
        source_url="https://docs.polymarket.us",
    ),
    "SANCTIONED": UsVenueRule(
        jurisdiction="SANCTIONED",
        eligible=False,
        notes="Sanctioned jurisdictions have no access.",
        source_url="https://docs.polymarket.us",
    ),
    "DEFAULT": UsVenueRule(
        jurisdiction="DEFAULT",
        eligible=False,
        notes=(
            "Eligibility for this jurisdiction is unverified. Verify against "
            "current venue terms and CFTC rules before trading."
        ),
        source_url="https://docs.polymarket.us",
    ),
}


@dataclass(frozen=True)
class UsEligibility:
    """Result of a US-venue eligibility check."""

    jurisdiction: str
    eligible: bool  # baseline jurisdiction eligibility
    verified: bool  # operator independently confirmed eligibility
    can_trade: bool  # eligible AND verified AND not blocked
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction,
            "eligible": self.eligible,
            "verified": self.verified,
            "can_trade": self.can_trade,
            "warnings": list(self.warnings),
        }


def check_us_eligibility(jurisdiction: str, verified: bool = False) -> UsEligibility:
    """Check Polymarket US venue eligibility.

    ``verified`` must be True only after an operator has independently
    confirmed account/KYC/state/product eligibility. Baseline eligibility
    alone is never sufficient to trade.
    """
    upper = jurisdiction.upper().strip()
    rule = US_VENUE_RULES.get(upper, US_VENUE_RULES["DEFAULT"])

    warnings: list[str] = []
    if upper not in US_VENUE_RULES:
        warnings.append(
            f"Unknown jurisdiction '{jurisdiction}'; assuming not eligible. "
            "Verify independently."
        )
    if rule.notes:
        warnings.append(rule.notes)
    if rule.eligible and not verified:
        warnings.append("Eligibility not independently verified by an operator.")

    can_trade = rule.eligible and verified
    return UsEligibility(
        jurisdiction=upper,
        eligible=rule.eligible,
        verified=verified,
        can_trade=can_trade,
        warnings=tuple(warnings),
    )


def can_trade_us_venue(jurisdiction: str, verified: bool = False) -> bool:
    """True only when baseline-eligible AND operator-verified."""
    return check_us_eligibility(jurisdiction, verified).can_trade
