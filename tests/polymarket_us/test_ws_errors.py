"""A server error message on a live socket is logged, not treated as a dead connection."""

from polymarket_us.errors import PolymarketUSError, WebSocketError

from btc_signal_engine.adapters.polymarket_us.market_data import ws_error_is_fatal


def test_server_error_messages_are_not_fatal_but_a_dead_loop_is():
    try:
        e = WebSocketError("market not found")
    except TypeError:  # constructor signature differs between SDK versions
        e = WebSocketError.__new__(WebSocketError)
    assert ws_error_is_fatal(e) is False
    assert ws_error_is_fatal(PolymarketUSError("connection reset")) is True
    assert ws_error_is_fatal(RuntimeError("boom")) is True
