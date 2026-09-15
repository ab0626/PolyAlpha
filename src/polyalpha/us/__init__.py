"""Polymarket US adapter package.

Venue family: USRetailAdapter (marketSlug / gateway REST / auth WS) and
USExchangeAdapter (symbol / refdata / Exchange REST / gRPC). Both normalize
into the canonical domain layer (Market, Book, Level, Trade, MarketSnapshot,
Decimal probability) so the core research objects never see venue-specific
IDs or price representations.

US is a separate empirical lineage from Polymarket International: it has its
own identifiers, price forms, market states, rate limits, and auth model.
"""