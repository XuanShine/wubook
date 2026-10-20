## Problem

The operator needs a local process that sells one WuBook property to a Grok voice agent over HTTP and to themselves in a French NiceGUI. The unfinished py4web route `get_rooms_between` does not enforce occupancy, arrival, departure, min/max stay, or parent availability, and it adds the `price` field from `fetch_rooms_values`, which Wired says to ignore. `wubook_api.Wubook` acquires a token on every `with` block, so it cannot be the session. `room_config.yml` is a hide list, while the new rule is an allow list. Dates are the other trap. Product check-out is the morning of departure. Avail and plan-0 reads use inclusive last night. `new_reservation` uses check-out as `dto`. A design that keeps one `dfrom`/`dto` pair for all three will book the wrong nights or quote the wrong total.

## Usage (caller's view)

Run one process. It owns the Wired token, the catalog cache, `sell.yml`, and the booking ledger.

```bash
WUBOOK_LCODE=… WUBOOK_TOKEN=… python -m desk
# or, if WUBOOK_TOKEN is unset: WUBOOK_USER WUBOOK_PASSWORD WUBOOK_PKEY
```

The voice agent does not import Python. Each HTTP route is one Grok tool. The model reads `dit` aloud and never sends an amount.

```python
# LiveKit tool: chercher
# POST /voix/chercher
# {"arrivee": "2026-10-21", "depart": "2026-10-23", "adultes": 2, "enfants": 0}
# ->
# {
#   "dit": "Double Balcon, 2 nuits, 180 euros. Double Cuisinette, 2 nuits, 210 euros. Double Balcon et Airbnb Double partagent le même stock.",
#   "offres": [
#     {"id": 329667, "chambre": "Double Balcon", "nuits": 2, "euros": 180}
#   ],
#   "refus": [
#     {"id": 405127, "chambre": "Single Classique", "raison": "trop de personnes"}
#   ]
# }

# LiveKit tool: reserver
# POST /voix/reserver
# Header Idempotency-Key: livekit-turn-8841
# {"id": 329667, "arrivee": "2026-10-21", "depart": "2026-10-23",
#  "adultes": 2, "enfants": 0,
#  "prenom": "Marie", "nom": "Dupont", "email": "marie@example.fr", "pays": "FR"}
# -> {"dit": "Réservation confirmée, code 1726577593.", "code": "1726577593"}
# The same key a second time returns the same code and does not call Wired.
```

The operator never types dates as a live filter. Date pickers plus a Chercher button call the same `Desk.quote`. A row's Réserver button opens guest fields and calls `Desk.book` with that row's stay, party, and room id.

```python
# NiceGUI, same process, same Desk
q = desk.quote(Stay.parse(check_in, check_out), Party(adults=2, children=0))
ui.label(q.dit)  # French, short
for row in q.rows:
    if isinstance(row, Offer):
        ui.button("Réserver", on_click=lambda o=row: book_dialog(o))
    else:
        ui.label(f"{row.room.name}: {row.reason.dit}")

desk.set_sell(RoomId(329667), True)   # writes sell.yml, the only sell list
desk.book(q.stay, q.party, row.room.id, guest, key, Origin.COMPTOIR)
```

The next engineer opens three files.

1. `desk/_quote.py` holds WuBook Parity (`pid` 0) as the only price source, the sum over inclusive nights, and the refusal table.
2. `desk/_sell.py` plus `sell.yml` mark a room unsellable when the id is missing or `false`.
3. `desk/_catalog.py` derives occupancy and the parent pool. Virtual `avail` is copied from the parent before quote runs.

They do not open `_wired.py` to learn either fact.

## Shape

The public type is a `Quote`. `Desk.quote(stay, party)` returns every catalog room as `Offer` or `Refusal`. `Desk.book` takes stay, party, and room id. It quotes again, refuses if that row is not an `Offer`, and sends the fresh plan-0 total. Neither caller passes an amount. An `Offer` is a display row, not a capability that could freeze a stale euro figure. per type-system-discipline and encode-lessons-in-structure.

`Stay` is check-in plus check-out. `InclusiveNights` is check-in through check-out minus one day. `ReservationSpan` is check-in through check-out. `_wired.py` is the only module that formats `DD/MM/YYYY`. A function that talks to `fetch_rooms_values` or `fetch_plan_prices` takes `InclusiveNights`. `new_reservation` takes `ReservationSpan`. Mixing them is a type error. per foundational-thinking.

`Night` has no `price` field. The current bug cannot be retyped. `Plan0[room_id]` is a tuple of `Euro` cents, one per inclusive night. `plan0_total` sums that tuple. Prices under 1 cent refuse with `prix_invalide`. per model-the-domain.

Sellability is one YAML map `room_id: bool`. Absent means not sold. The UI writes that file on each toggle. There is no `hide_rooms`, no `REAL_ROOMS`, no `type_room`. Quote reads a frozen snapshot of ids whose value is `true`. per laziness-protocol and subtract-before-you-add.

A `Pool` is the physical room id. Virtual rooms point at it. At catalog parse, each virtual night's `avail` is replaced by the parent's. Quote then reads `night.avail` with no parent branch. If two YAML-true rooms share a pool, `Quote.dit` names them and says they share one stock. per minimize-reader-load.

Refusal is a table of named checks, first failure wins, each with a spoken French clause.

- `pas_en_vente` from the YAML snapshot
- `trop_de_personnes` if `party.size > room.occupancy`
- `fermee` if any night is closed
- `arrivee_fermee` if check-in is `closed_arrival`
- `depart_ferme` if check-out is `closed_departure`
- `sejour_trop_court` if any night's `min_stay` or the arrival night's `min_stay_arrival` exceeds `stay.nights`
- `sejour_trop_long` if a positive `max_stay` is below `stay.nights`
- `plus_de_stock` if min avail across nights is below quantity 1
- `prix_invalide` if any plan-0 night is under 1 cent

`min_stay_arrival` is in the table because `ignore_restrs` stays 0 and Wired would reject the book after a spoken offer. `no_ota` and `woodoo` are not refusals. This process is the Online Reception path.

`Desk` is the one module callers import. It hides the token, flood caches, YAML, rule table, parent rewrite, amount, hardcoded `ccard=0` / `ignore_restrs=0` / `ignore_avail=0` / `status=1` / board `nb` / quantity 1, and the ledger. HTTP and NiceGUI parse into `Stay`, `Party`, `Guest`, then call `quote` or `book`. They do not see Wired. Day-to-day methods are `rooms`, `set_sell`, `quote`, and `book`. The HTTP layer is not a second API. It is JSON in, `dit` out. per boundary-discipline.

One process hosts NiceGUI and the voice routes. Two actors do not write `sell.yml` or the ledger. `set_sell` is UI-only. The voice agent cannot toggle rooms. Token acquire happens once in `Desk.open` when `WUBOOK_TOKEN` is unset, and release happens in `Desk.close`. per separate-before-serializing-shared-state.

Idempotency is a local ledger keyed by the caller key. Before Wired, write `pending`. After success, write `done` plus the reservation code, via temp file and rename. A repeat of a `done` key returns that code. A `pending` key does not call `new_reservation` again. Crash between Wired success and `done` leaves `pending` and no second booking. Recovery is an open question, not a silent retry. per make-operations-idempotent.

Catalog lives in memory with a long TTL (hours). Nights plus plan 0 share one cache key `(first, last)` with a short TTL (about a minute) and coalesce in-flight reads. Search is a button, not a keystroke. A stay repeats from cache. `fetch_rooms` is not on the search path.

Do not import `wubook_api.py`, `MyWubook.py`, or the py4web `__init__.py`. They encode the old session, the hide list, and the forbidden price field. per redesign-from-first-principles.

## Synthesis decision

filled by the parent arena

## Tradeoffs accepted

- We accept an empty `sell.yml` on first boot (nothing sold until the operator flips switches) in exchange for never auto-selling the Airbnb virtual rooms 562928 and 563031.
- We accept a local ledger that can strand a `pending` key after a crash in exchange for never double-booking Wired, which has no idempotency argument.
- We accept integer euro cents in the domain and a float only on the `new_reservation` wire in exchange for totals that do not drift from binary floats before the operator hears them.
- We accept `min_stay_arrival` in the refusal table even though the product list omitted it, in exchange for not offering a room Wired will reject with `ignore_restrs=0`.
- We accept no live search in the UI in exchange for staying under 240 `fetch_rooms_values` per 43200 seconds without a debounce state machine.
- We accept dropping the old `acquire_token` context manager rather than wrapping it, in exchange for one token owner that HTTP requests cannot construct.

## Alternatives considered

- A thin REST wrapper around Wired methods (`fetch_rooms_values`, `fetch_plan_prices`, `new_reservation`). The voice model would have to sum nights, skip the values-call price, apply parent avail, and send `amount`. That exposes protocol to the caller and hides nothing. It lost because the public methods would match the wire and every quote rule would leak into Grok's tool descriptions.
- Patch `MyWubook.get_rooms_between` and keep py4web. The function already returns a room dict with `closed` and `min_stay` for the caller to interpret, so a second voice route would re-implement the same checks. Two ways to quote. It lost because the LiveKit agent cannot be trusted to enforce occupancy, and the price field is the wrong source.
- Two processes, NiceGUI and a FastAPI voice server, sharing `sell.yml` and a token file. Toggle and search would both write or refresh shared files. It lost because the shared write is real and a lockfile is a smell when one process already can own the state.
- Quote as a list of offerable rooms only, refusals dropped. The agent cannot say why a named room is missing, and the operator's table cannot show a grey row with a reason. It lost because both consumers need the refusal, not only the offer.

## Open questions and risks

- If a `pending` ledger row survives a crash after Wired accepted the booking, should the operator paste a reservation code by hand, or should we call `fetch_bookings` once and match on ancillary `idempotency_key`?
- Should guest `pays` default to `FR` in the UI only, or also on the voice route when the model omits it?
- The official customer XML succeeds with `lname`, `fname`, `email`, `country`. If a live `new_reservation` on this property rejects that subset, do we require `city`, `phone`, `street`, `arrival_hour`, `notes` on both callers?
- Catalog cache of several hours can miss a room rename until restart or an explicit Rafraîchir. Is a push-update URL in scope for this local process?

## Next implementation step

Write `Stay`, `InclusiveNights`, `ReservationSpan`, and the tests that prove a one-night stay reads one plan-0 price and sends `dto` equal to check-out.
