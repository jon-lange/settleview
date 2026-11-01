"""Polymarket US adapters (official `polymarket_us` SDK only): market data over the markets WebSocket, the trading
gateway, the private order stream, settlement lookups and the shadow exchange over Polymarket's live book.

The application's "ticker" for a Polymarket market IS its slug (`cpc-btc-updown-15m-YYYY-MM-DD-HHMMz`, window start
UTC), so the series guard is the slug prefix. Polymarket has one instrument per market (YES = Up); every price on
the wire is the YES price. The adapters convert to the application's outcome terms (side yes|no, price on that side).
"""
