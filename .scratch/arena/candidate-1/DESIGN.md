## Problem

The local process replaces the unfinished py4web app. A LiveKit agent and a NiceGUI screen sell stays for one property. The next engineer has to see the price source and the unsellable mark in three files.

`Wubook.__init__` calls `acquire_token` on every entry, and `__exit__` releases it. The docs also allow a permanent token. `MyWubook.get_rooms_between` sums `price` on `fetch_rooms_values`, which the docs say to ignore, and it never checks party size, `closed_arrival`, `closed_departure`, or `max_stay`. If a parent id were hidden, the virtual-avail copy would miss that parent. `room_config.yml` hides `562928` and `563031`, while `wubook_api.py` keeps a second list in `REAL_ROOMS`. The HTTP `new_reservation` handler is empty.

Wired returns `[code, payload]` from `https://wired.wubook.net/xrws/`. Code 0 is success. A negative code means the payload is a human-readable string. Dates on the wire are `DD/MM/YYYY`. `new_reservation` takes `dto` as the departure morning. The official one-night example uses `dfrom` `21/02/2025` and `dto` `22/02/2025`. The property board is `nb`. `ignore_restrs` and `ignore_avail` stay 0. `ccard` stays 0. Those three must not be arguments on a route. `fetch_rooms` is capped at 240 calls per 86400 seconds. `fetch_rooms_values` is capped at 240 per 43200 seconds. The property is capped at 600 calls per 3600 seconds. A search that runs on each keystroke will hit those caps.

## Usage (caller's view)

You run `python -m comptoir`. One process serves the voice tools and the desk on `127.0.0.1:8071`. The voice agent does not import the package. Each tool is one POST, and the agent speaks `lignes`.

`chercher` sends the stay. The body is `arrivee`, `depart`, `adultes`, and `enfants`. `depart` is the morning the guest leaves, as `YYYY-MM-DD`.

```python
def chercher(arrivee: str, depart: str, adultes: int, enfants: int) -> str:
    body = requests.post(
        "http://127.0.0.1:8071/outils/chercher",
        json={
            "arrivee": arrivee,
            "depart": depart,
            "adultes": adultes,
            "enfants": enfants,
        },
        timeout=30,
    ).json()
    return " ".join(body["lignes"])
```

A shared pool comes back as `Double Balcon, 2 nuits, 180 euros, même stock que Double Balcon Airbnb.` A refusal comes back as `Triple Économique refusée, arrivée fermée le 21 octobre.`

`reserver` sends the tool-call id as `cle`. A second POST with the same `cle` returns the stored WuBook code.

```python
def reserver(cle: str, chambre: int, arrivee: str, depart: str, adultes: int, enfants: int, nom: str) -> str:
    body = requests.post(
        "http://127.0.0.1:8071/outils/reserver",
        json={
            "cle": cle,
            "chambre": chambre,
            "arrivee": arrivee,
            "depart": depart,
            "adultes": adultes,
            "enfants": enfants,
            "client": {"nom": nom, "pays": "FR"},
        },
        timeout=30,
    ).json()
    return body["lignes"][0]
```

The page in `app.py` calls the same `Desk` as those routes. The reserve control keeps one `IdempotencyKey` for its lifetime. A second click repeats that key. `guest_from_form` is the only map from the French field names to `Guest`.

```python
from comptoir import Blocked, Desk, IdempotencyKey, Offer, Origin, Stay

desk = Desk()

def on_switch(row, sell: bool) -> None:
    desk.set_sell(row.room_id, sell)

def on_submit() -> None:
    parsed = Stay.from_iso("2026-10-21", "2026-10-23", adults=2, children=0)
    if isinstance(parsed, Blocked):
        ui.notify(parsed.line())
        return
    found = desk.search(parsed)
    if isinstance(found, Blocked):
        ui.notify(found.line())
        return
    for verdict in found:
        ui.label(verdict.line())

def on_reserve(stay: Stay, offer: Offer, key: IdempotencyKey) -> None:
    guest = guest_from_form({"nom": "Martin", "pays": "FR"})
    if isinstance(guest, Blocked):
        ui.notify(guest.line())
        return
    ui.notify(desk.book(stay, offer.room_id, guest, key, Origin.GUICHET).line())
```

The third caller is the test that locks the price. `facts.cents` holds plan 0 for 21 and 22 October, 9000 and 9000. `SleptNight` has no price field. `quote` is imported from `comptoir.quote` here and nowhere in `app.py`.

```python
from comptoir.quote import RoomId, SellBook, quote

hits = quote(stay, catalog, facts, SellBook({RoomId(329667): True}))
assert hits[0].line() == "Double Balcon, 2 nuits, 180 euros"
```

## Shape

Four methods cover the product. `search`, `book`, `rooms`, and `set_sell`. The voice routes parse JSON and call those methods. The page calls the same methods. Fetch, cache, parent availability, plan 0, the board, the card constant, the ignore flags, and the receipt file stay inside `Desk`. Callers still choose dates, party, room, guest, idempotency key, and origin, because those are their decisions. Per minimize-reader-load, `Desk.search` fetches the window and calls `quote`. The route parses JSON and calls `Desk.search`.

The records live in `quote.py`. A `Stay` stores a check-in, a checkout, adults, children, and a quantity. `slept()` is check-in inclusive and checkout exclusive. `values_span()` runs through checkout so the departure flag exists. `price_span()` stops on the last slept night. `reservation_span()` sends checkout as `dto`. That split is the domain, per model-the-domain. Per type-system-discipline, the departure bit is `StayFacts.departure_closed`. A field on `SleptNight` would be read as the last night the guest sleeps.

`Room` keeps id, name, occupancy, parent, and price anchor. The catalog parse drops wire `price`, `min_price`, `max_price`, board, shortname, `men`, `children`, and woodoo. A price field on `Room` would get spoken. Per encode-lessons-in-structure, the dropped list is the lesson. `StayFacts` holds slept nights, plan-0 cents, and the departure flags for one window, aligned by room id. Cents are ints. `None` means missing or below 0.01, and the parser does not round 0.009 up to one cent. `series_for` reads the room series and, when any night is `None`, the anchor series. `total_cents` multiplies that sum by quantity. No other function reads `cents`. Per foundational-thinking, the cache stores `StayFacts` under `(check_in, check_out)` with the time it was filled. Search uses an entry for 15 minutes. A booking reloads the window, quotes again, and sends that total.

`quote` walks one ordered list. Unsold rooms are omitted on a search. A book of one room can still say `pas en vente`. Then party size against occupancy, any closed slept night, `closed_arrival` on check-in, `closed_departure` on checkout, `min_stay`, `max_stay`, availability, then price. A 0 on `min_stay` or `max_stay` adds no limit. Availability is the minimum across slept nights, already replaced with the parent value in `Desk._grid`. The first failure is the only reason. `pool_names` names the other rooms `sell.yml` marks `yes` in the same pool, and that clause is attached to offers and refusals.

`sell.yml` is the only room list. The word `yes` sells. The word `no` marks the room unsold. A missing id stays unsold. Any other word aborts startup. The shelf lists every catalog room, and the switch writes `yes` or `no` through `set_sell` with a temp file and a rename. The two Airbnb virtuals start as `no`. The file ships with no `yes` entries. Per redesign-from-first-principles, this file replaces `REAL_ROOMS` and `hide_rooms`.

`Desk` owns the token. When `WUBOOK_TOKEN` is set, that value is the token for the life of the process. Otherwise `acquire_token` runs once, and `close` releases it. `lcode` is `WUBOOK_LCODE`. The page and the routes run in one process. One lock covers the token, `sell.yml`, and `receipts.json`, because those three are the shared write targets. Per separate-before-serializing-shared-state, the lock is that serialization. `Memory` counts `fetch_rooms`, `fetch_rooms_values`, and every other call against the Wired ceilings and stops locally with `WuBook limité, réessayez plus tard.` The catalog is fetched at startup and again after six hours. The only search entry is a complete stay. The page calls `search` from the submit control. Per experience-first, a past arrival is spoken as `arrivée déjà passée`. Clamping the date to today would quote the wrong nights.

The card, the ignore flags, the status, and the board are constants on `_Wired`. Those constants are `ccard = 0`, `ignore_restrs = 0`, `ignore_avail = 0`, `status_confirmed = 1`, `board = "nb"`, and `parity_pid = 0`. `ReservationOrder` carries the stay, the room, the quantity, the euros, the guest, and the origin. `_place` is the only caller of `new_reservation`, and `_place` formats the European dates. Per boundary-discipline, `Stay.from_iso` and `guest_from_form` are the doors, and `quote` trusts `Stay`, `StayFacts`, and `SellBook`.

The receipt is the idempotency record, per make-operations-idempotent. The fingerprint is room, dates, quantity, last name, email, and cents. `open` is on disk before the call. `sent` is written before the socket. `booked` stores the code. `rejected` means Wired returned a negative code. The same key and the same fingerprint return the stored code. The same key and a different fingerprint speak `clé déjà utilisée pour une autre demande.` A `sent` receipt with no `fetch_bookings` match speaks `envoi incertain, vérifiez WuBook.` `_place` runs from `open` or `rejected`. A crash in `open` can be repeated. A crash in `sent` stays `sent` until a person checks WuBook.

The engineer opens three files. `sell.yml` shows the mark. `quote.py` shows `sells`, the reason order, and `series_for`. `desk.py` shows `fetch_plan_prices` with pid 0 and the parent-avail copy in `_grid`. `app.py` parses JSON and calls `Desk`.

## Synthesis decision

filled by the parent arena

## Tradeoffs accepted

- We accept a first boot that sells no rooms, in exchange for an allow-list and no room ids in Python. `562928` and `563031` start as `no`.
- We accept one extra day on `fetch_rooms_values`, in exchange for `closed_departure` on the morning the guest leaves. That day is not priced and is not a slept night.
- We accept refusing a room when any slept night is under 0.01, in exchange for a total built only from nights at or above 0.01.
- We accept `min_stay` and `max_stay` of 0 as "no limit", in exchange for unrestricted rooms staying quotable.
- We accept a `sent` receipt that will not retry, in exchange for never creating a second WuBook booking after a lost response.
- We accept an exact-date cache of 15 minutes, in exchange for one dict keyed by the stay dates. `book` bypasses it and pays for a fresh window.
- We accept confirmed bookings only, in exchange for a `book` method with no status argument.
- We accept French sentences inside `quote`, in exchange for one reason string shared by the voice and the page.
- We accept tests importing `comptoir.quote`, in exchange for a pure `quote` that never opens a socket. `app.py` imports `Desk` only.
- We accept `Room` without `men` and `children`, in exchange for a type that cannot grow a second occupancy rule beside `party()`.

## Alternatives considered

A pipeline of `catalog.py`, `availability.py`, `prices.py`, `policy.py`, and `booking.py` lost. The route would call each stage, so the interface would be wide and would hide little. You would still have to know pid 0, the two date windows, and the parent copy. The price rule would sit past the third file. That shape is temporal decomposition.

Extending `MyWubook.get_rooms_between` lost. The method would still sum the values payload, and the token would still be acquired per request. Callers would keep reading WuBook dict keys. The new checks would be a second stay loop beside the old one.

A rule file beside `sell.yml` lost. It would hide the predicates from Python and expose a second language. You would then have two files that mean "unsellable", and an edit to one would leave the other stale. The predicates stay in `quote`.

## Open questions and risks

- Does live `fetch_rooms_values` store `closed_departure` on the checkout date? `values_span` assumes it does. If the flag is on the last slept night, the extra day is the wrong day.
- Does plan 0 already store the anchor-derived price on the child room id? `series_for` uses the child series first, and the anchor only when the child has a night under 0.01.
- Should `min_stay_arrival`, `men`, `children`, or `no_ota` refuse a room? `quote` enforces the listed rules. `ignore_restrs` stays 0, so WuBook can still reject a stay `quote` returned as an offer.
- Is WooDoo Online Reception enabled? `new_reservation` requires it, and every `book` fails closed if it is not.
- Is `fetch_bookings` with `oncreated` 0 the stay-date filter on this property? `_find_existing` assumes that, and matches room id, last name, email, and cents. A loose match could attach the wrong code to a `sent` receipt.

## Next implementation step

Write `quote`, `series_for`, and `total_cents` against hand-built `Catalog`, `StayFacts`, and `SellBook` values, including a virtual room whose avail was copied from its parent and a checkout date with `closed_departure`, before any XML-RPC call.
