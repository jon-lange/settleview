# Lost order answers: a list miss is not "no fill"

**Problem.** When a create order's answer was lost (timeout, reset, 5xx, an ambiguous 4xx), each venue path guessed
the outcome from a source that lags: Kalshi event contracts closed the entry as "not_found" after 60 s if the order
lists did not show it, freeing its committed risk and leaving a possibly real position with no take profit (and its
market counted as "ours", so reconcile did not report it either); a recovered entry was booked at its limit, not
its fill price; Polymarket US read "canceled, 0 filled" from the change in a positions list that lags our own fills
1-3 s (and whose "before" reading could itself be stale); a Polymarket IOC answered while still `PENDING_NEW` was
read as canceled; perps marked an unlisted order rejected after 60 s without looking at fills, and an entry found
filled late got no stop. A credential blip that dropped the mode to sandbox also blocked selling the prod position.

**Cause.** "Not in the list" was treated as evidence. The order lists lag placements (seen for seconds on Kalshi
demo), the positions lists lag fills (Polymarket 1-3 s, perps one step), and an IOC answer is not always final.

**Fix.** One rule in `btc_signal_engine/domain/order_resolution.py`, used by `application/trading.py` (Kalshi and
Polymarket US gateways) and `application/perp_trading.py`:

- Unknown until proven. FILLED = the order itself, found by client_order_id in the lists, or by id from a fill of
  ours since the send (Kalshi/perps fills name the order; read it and check its client_order_id), or for Polymarket
  US (trades carry no order id) unclaimed activity trades in that market since the send at prices it could fill at.
  NOT FILLED = the fills show nothing that could be it AND the position holds no more than our records explain.
  Anything else stays unknown, committed at its maximum, audited once (`order_unresolved`), warned
  (`settle_warnings` kind `unresolved_order`), re-checked every 10 s.
- Book the real average price; place the saved protection on a late fill (perps keep the approved legs on the order
  row, migration 0019), else mark the position unprotected and warn.
- Only 400/401/403/404/422 prove a refusal (`DEFINITIVE_4XX`).
- An exit is gated on the bracket's own environment (`TradingService.mode_of`), and an agent session paused by a
  mode change may still exit its own position on its pinned environment.
- Perps: approve's venue calls share one deadline (`send_deadline_s`, 30 s) so the lock is never held for minutes.

**How to avoid.** Never turn the absence of a row into a fact about money. Ask the source that records the event
(fills, activity trades) and cross-check the position; if they disagree or fail, say "unknown" and keep the risk
counted. Every list read pages to the end (Kalshi positions/fills/orders used to read one page of 200).

**Not verified (venue-only):** how long Kalshi's and the perps order and fills lists lag a placement in production;
whether Polymarket US activity trades are always priced in YES terms and how fast they appear; whether a Polymarket
IOC can really stay `PENDING_*` past ~2 s. The residual Polymarket risk: a trade the user made by hand in the same
market, second and price range as a lost order can be claimed for it (it is noted when used).
