from datetime import date, timedelta

from comptoir.quote import (
    Catalog,
    InclusiveNights,
    ReservationSpan,
    Room,
    RoomId,
    SellBook,
    SleptNight,
    Stay,
    StayFacts,
    quote,
)

DAY = date(2025, 2, 21)
NEXT = date(2025, 2, 22)


def room(rid, name, occupancy=2, parent=None, anchor=None):
    return Room(
        RoomId(rid),
        name,
        occupancy,
        None if parent is None else RoomId(parent),
        None if anchor is None else RoomId(anchor),
    )


def night(day, **overrides):
    values = dict(
        date=day,
        avail=3,
        closed=False,
        closed_arrival=False,
        min_stay=0,
        max_stay=0,
        min_stay_arrival=0,
    )
    values.update(overrides)
    return SleptNight(**values)


def facts_for(rooms, nights, cents, departure=None):
    return StayFacts(
        nights=nights,
        cents=cents,
        departure_closed=departure or {item.id: False for item in rooms},
    )


def catalog_of(*rooms):
    return Catalog({item.id: item for item in rooms})


def sell_yes(*rooms):
    return SellBook({item.id: True for item in rooms})


def test_one_night_prices_one_plan0_night_and_splits_the_spans():
    stay = Stay(DAY, NEXT, adults=2, children=0, quantity=1)
    assert stay.slept() == (DAY,)
    assert stay.nights == 1
    price = stay.price_span()
    values = stay.values_span()
    booked = stay.reservation_span()
    assert isinstance(price, InclusiveNights)
    assert isinstance(values, InclusiveNights)
    assert isinstance(booked, ReservationSpan)
    assert not isinstance(booked, InclusiveNights)
    assert price == InclusiveNights(DAY, DAY)
    assert values == InclusiveNights(DAY, NEXT)
    assert booked.dfrom == DAY
    assert booked.dto == NEXT
    assert price.last != values.last

    balcon = room(329667, "Double Balcon")
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY),)},
        {balcon.id: (9000,)},
    )
    hits = quote(stay, catalog_of(balcon), window, sell_yes(balcon))
    assert len(hits) == 1
    assert hits[0].line() == "Double Balcon, 1 nuit, 90 euros"


def test_two_nights_sum_plan0_and_speak_a_whole_euro_amount():
    stay = Stay(DAY, date(2025, 2, 23), adults=2, children=0)
    balcon = room(329667, "Double Balcon")
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY), night(date(2025, 2, 22)))},
        {balcon.id: (9000, 9000)},
    )
    hits = quote(stay, catalog_of(balcon), window, sell_yes(balcon))
    assert hits[0].line() == "Double Balcon, 2 nuits, 180 euros"


def test_cents_use_a_comma_and_quantity_is_spoken():
    balcon = room(329667, "Double Balcon")
    one = Stay(DAY, NEXT, adults=2, children=0, quantity=1)
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY, avail=4),)},
        {balcon.id: (9050,)},
    )
    assert quote(one, catalog_of(balcon), window, sell_yes(balcon))[0].line() == (
        "Double Balcon, 1 nuit, 90,50 euros"
    )
    two = Stay(DAY, NEXT, adults=2, children=0, quantity=2)
    doubled = facts_for(
        (balcon,),
        {balcon.id: (night(DAY, avail=4),)},
        {balcon.id: (9000,)},
    )
    assert quote(two, catalog_of(balcon), doubled, sell_yes(balcon))[0].line() == (
        "Double Balcon, 1 nuit, 180 euros, 2 chambres"
    )


def test_virtual_room_mentions_the_other_yes_in_the_pool():
    parent = room(1, "Double Balcon")
    virtual = room(2, "Double Balcon Airbnb", parent=1)
    stay = Stay(DAY, NEXT, adults=2, children=0)
    shared = night(DAY, avail=2)
    window = facts_for(
        (parent, virtual),
        {parent.id: (shared,), virtual.id: (night(DAY, avail=2),)},
        {parent.id: (9000,), virtual.id: (9000,)},
    )
    hits = quote(stay, catalog_of(parent, virtual), window, sell_yes(parent, virtual))
    lines = [item.line() for item in hits]
    assert "Double Balcon, 1 nuit, 90 euros, même stock que Double Balcon Airbnb" in lines
    assert "Double Balcon Airbnb, 1 nuit, 90 euros, même stock que Double Balcon" in lines


def test_unsold_room_is_absent_from_search_and_refused_by_id():
    balcon = room(329667, "Double Balcon")
    hidden = room(562928, "Airbnb")
    stay = Stay(DAY, NEXT, adults=2, children=0)
    window = facts_for(
        (balcon, hidden),
        {balcon.id: (night(DAY),), hidden.id: (night(DAY),)},
        {balcon.id: (9000,), hidden.id: (9000,)},
    )
    sell = SellBook({balcon.id: True, hidden.id: False})
    search = quote(stay, catalog_of(balcon, hidden), window, sell)
    assert [item.room_id for item in search] == [balcon.id]
    missing = SellBook({})
    refused = quote(stay, catalog_of(hidden), window, missing, only=hidden.id)
    assert refused[0].line() == "Airbnb refusée, pas en vente"
    absent = quote(stay, catalog_of(balcon), window, sell_yes(balcon), only=RoomId(404))
    assert absent[0].line() == "404 refusée, pas en vente"


def test_each_refusal_sentence():
    balcon = room(329667, "Double Balcon", occupancy=2)
    stay = Stay(DAY, NEXT, adults=2, children=0)
    catalog = catalog_of(balcon)
    sell = sell_yes(balcon)

    def spoken(**night_kw):
        window = facts_for(
            (balcon,),
            {balcon.id: (night(DAY, **night_kw),)},
            {balcon.id: (9000,)},
        )
        return quote(stay, catalog, window, sell)[0].line()

    crowded = Stay(DAY, NEXT, adults=2, children=1)
    window = facts_for((balcon,), {balcon.id: (night(DAY),)}, {balcon.id: (9000,)})
    assert quote(crowded, catalog, window, sell)[0].line() == (
        "Double Balcon refusée, trop de personnes pour 2 places"
    )
    assert spoken(closed=True) == "Double Balcon refusée, fermée le 21 février"
    assert spoken(closed_arrival=True) == "Double Balcon refusée, arrivée fermée le 21 février"
    closed_out = facts_for(
        (balcon,),
        {balcon.id: (night(DAY),)},
        {balcon.id: (9000,)},
        {balcon.id: True},
    )
    assert quote(stay, catalog, closed_out, sell)[0].line() == (
        "Double Balcon refusée, départ fermé le 22 février"
    )
    assert spoken(min_stay=2) == "Double Balcon refusée, minimum 2 nuits"
    assert spoken(min_stay_arrival=3) == "Double Balcon refusée, minimum 3 nuits à l'arrivée"
    longer = Stay(DAY, date(2025, 2, 24), adults=2, children=0)
    three = (night(DAY, max_stay=2), night(NEXT, max_stay=0), night(date(2025, 2, 23), max_stay=4))
    capped = facts_for(
        (balcon,),
        {balcon.id: three},
        {balcon.id: (9000, 9000, 9000)},
    )
    assert quote(longer, catalog, capped, sell)[0].line() == (
        "Double Balcon refusée, maximum 2 nuits"
    )
    assert spoken(avail=0) == "Double Balcon refusée, il reste 0, il en faut 1"
    missing = facts_for((balcon,), {balcon.id: (night(DAY),)}, {balcon.id: (None,)})
    assert quote(stay, catalog, missing, sell)[0].line() == "Double Balcon refusée, prix manquant"


def test_min_stay_zero_does_not_refuse():
    balcon = room(329667, "Double Balcon")
    stay = Stay(DAY, NEXT, adults=2, children=0)
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY, min_stay=0, min_stay_arrival=0, max_stay=0),)},
        {balcon.id: (9000,)},
    )
    hit = quote(stay, catalog_of(balcon), window, sell_yes(balcon))[0]
    assert hit.line() == "Double Balcon, 1 nuit, 90 euros"


def test_min_stay_equal_to_the_stay_does_not_refuse():
    balcon = room(329667, "Double Balcon")
    stay = Stay(DAY, NEXT, adults=2, children=0)
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY, min_stay=1, min_stay_arrival=1),)},
        {balcon.id: (9000,)},
    )
    hit = quote(stay, catalog_of(balcon), window, sell_yes(balcon))[0]
    assert hit.line() == "Double Balcon, 1 nuit, 90 euros"


def test_own_series_wins_and_anchor_covers_a_missing_night():
    child = room(2, "Enfant", anchor=1)
    stay = Stay(DAY, NEXT, adults=1, children=0)
    own = facts_for((child,), {child.id: (night(DAY),)}, {child.id: (8000,), RoomId(1): (1000,)})
    assert quote(stay, catalog_of(child), own, sell_yes(child))[0].line() == "Enfant, 1 nuit, 80 euros"
    borrowed = facts_for(
        (child,),
        {child.id: (night(DAY),)},
        {child.id: (None,), RoomId(1): (8000,)},
    )
    assert quote(stay, catalog_of(child), borrowed, sell_yes(child))[0].line() == (
        "Enfant, 1 nuit, 80 euros"
    )


def test_party_that_fits_is_offered():
    balcon = room(1, "Double Balcon", occupancy=2)
    stay = Stay(DAY, NEXT, adults=2, children=0)
    window = facts_for((balcon,), {balcon.id: (night(DAY),)}, {balcon.id: (9000,)})
    assert quote(stay, catalog_of(balcon), window, sell_yes(balcon))[0].line().startswith(
        "Double Balcon, 1 nuit"
    )


def test_from_iso_sentences():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    assert Stay.from_iso("pas-une-date", tomorrow.isoformat(), 2, 0).line() == "date invalide"
    assert Stay.from_iso((today - timedelta(days=1)).isoformat(), today.isoformat(), 2, 0).line() == (
        "arrivée déjà passée"
    )
    assert Stay.from_iso(today.isoformat(), today.isoformat(), 2, 0).line() == "départ avant l'arrivée"
    try:
        horizon = today.replace(year=today.year + 2)
    except ValueError:
        horizon = today.replace(year=today.year + 2, day=28)
    too_long = Stay.from_iso(today.isoformat(), (horizon + timedelta(days=1)).isoformat(), 2, 0)
    assert too_long.line() == "séjour trop long"
    exact = Stay.from_iso(today.isoformat(), horizon.isoformat(), 2, 0)
    assert isinstance(exact, Stay)
    assert Stay.from_iso(today.isoformat(), tomorrow.isoformat(), 0, 0).line() == "indiquez les personnes"
    assert Stay.from_iso(today.isoformat(), tomorrow.isoformat(), 2, 0, quantity=0).line() == (
        "quantité invalide"
    )
    parsed = Stay.from_iso(today.isoformat(), tomorrow.isoformat(), 2, 0)
    assert isinstance(parsed, Stay)
    assert parsed.quantity == 1


def test_first_failure_wins():
    balcon = room(1, "Double Balcon", occupancy=2)
    stay = Stay(DAY, NEXT, adults=3, children=0)
    window = facts_for(
        (balcon,),
        {balcon.id: (night(DAY, closed=True, avail=0),)},
        {balcon.id: (None,)},
    )
    line = quote(stay, catalog_of(balcon), window, sell_yes(balcon))[0].line()
    assert line == "Double Balcon refusée, trop de personnes pour 2 places"


def test_pool_clause_on_a_refusal():
    parent = room(1, "Double Balcon")
    virtual = room(2, "Double Balcon Airbnb", parent=1)
    stay = Stay(DAY, NEXT, adults=2, children=0)
    window = facts_for(
        (parent, virtual),
        {
            parent.id: (night(DAY, closed=True),),
            virtual.id: (night(DAY, avail=2),),
        },
        {parent.id: (9000,), virtual.id: (9000,)},
    )
    hits = quote(stay, catalog_of(parent, virtual), window, sell_yes(parent, virtual))
    refused = next(item for item in hits if item.room_id == parent.id)
    assert refused.line() == (
        "Double Balcon refusée, fermée le 21 février, même stock que Double Balcon Airbnb"
    )
