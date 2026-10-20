## Synthesis decision

Base is candidate A, `comptoir`. The judge scored it 29 to 25. The parent scored it 29 to 27. The gap is criterion 1 only. Both readings pick A.

Candidate B loses because `Wired.create_reservation` is a second booking path that takes an amount, and `closed_departure` sits on slept nights while checkout is the next morning.

## Grafts taken from B

1. One `CHECKS` tuple owns the refusal order, and it includes `min_stay_arrival`. The spoken sentences stay the dated French lines from A.
2. `InclusiveNights` and `ReservationSpan` are distinct types. Callers still pass `Stay`. `departure_closed` stays on the checkout morning, not on a slept night.
3. `new_reservation` stores the idempotency key in `ancillary`. Recovery matches that key only. A name and email match is rejected because it can attach the wrong code.

## Rejected from B

Search does not speak every unsold room. The voice result lists sold rooms only. A direct book of an unsold id still says `pas en vente`.

`Night` does not carry `closed_departure`. The values fetch includes the checkout date for that flag and does not price it.

The XML-RPC object stays private and has no amount parameter.

`sell.yml` keeps the words `yes` and `no`. An unknown word aborts startup.

## Agreement

One process, one `Desk`, methods `search`, `book`, `rooms`, `set_sell`. Plan 0 is the only price. The voice model does not send an amount. `ccard`, `ignore_restrs`, and `ignore_avail` are constants. `sell.yml` is the only room list. A missing id is not sold. Book quotes again from a fresh window. Status is confirmed (1).
