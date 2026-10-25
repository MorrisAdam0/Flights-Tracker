# Flights-Tracker

Brute-forces the cheapest way to get from London to **Mexico** or **Malaysia** and back over New Year,
using Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights)), and tracks how
those prices move so you can tell whether to book now or wait.

## The rules (edit in `flights_tracker/config.py`)

- Leave London 27–31 Dec 2026; leave the destination 7–9 Jan 2027; be home by 10 Jan.
- One stop at most. Tickets straight from London: ≤1 stop, ≤16h door to door.
- Via a European hub: two separate **nonstop** tickets (the hub is your one stop), at least 3h to
  connect, and up to 2 nights there (costed at ~£70/night).
- You visit **Mexico City** / **Kuala Lumpur** at some point, so every total includes getting from
  where you land to there, and from there to where you fly home.

## What it tries

| Strategy | Example |
|---|---|
| Direct one-ways, open-jaw allowed | LHR→CUN, MEX→LHR |
| Return from London | LHR⇄CUN |
| Via a hub, one-ways | STN→MXP, MXP→CUN … CUN→MAD, MAD→LGW |
| Hub return + hops | LTN→MAD, MAD⇄CUN, MAD→LGW |
| Any mix | Direct out, via Amsterdam back |

- **Mexico gateways:** MEX, CUN, PVR, SJD, GDL, MID, OAX, HUX.
- **Malaysia gateways:** KUL, PEN, LGK, BKI, KCH, plus SIN (5h bus from KL).
- **Hubs:** Madrid, Barcelona, Paris, Amsterdam, Frankfurt, Munich, Düsseldorf, Zurich, Milan, Rome,
  Brussels, Lisbon, Warsaw, Istanbul, Copenhagen and Helsinki. Doha, Dubai, Abu Dhabi and Muscat are
  also tried for Malaysia.
- **London airports for the short hops:** LHR, LGW, STN, LTN, LCY, SEN.

The London↔hub hops are only searched for hubs that turned out to have long-haul flights.

## Run it

```sh
pip install -r requirements.txt
python -m flights_tracker                 # full brute force, both trips (~1,500–3,000 searches, ~1h)
python -m flights_tracker --trip mexico   # one trip
python -m flights_tracker --track         # re-price only the shortlist from the last full run (minutes)
```

Output in `results/`:

- `latest.md`: cheapest per strategy, top routes, and the top 5 in detail with a Google Flights link
  and a price-history verdict for every ticket.
- `<trip>_itineraries.csv`: the top 300 combinations with a cost breakdown (flights / hub nights /
  in-country), ready to add accommodation and internal travel to.
- `watchlist.json`: the shortlist (top 25 per trip) that `--track` re-prices.
- `history/observations.csv`: the cheapest price for every search on every run, plus Google's own
  60-day price history (low / typical / high / recent trend) where the page includes it.
- `history/itineraries.csv`: the total for each shortlisted itinerary on every run.

## Price history: should you book now?

Each ticket in the report gets a verdict:

- **Low vs its 60-day history:** good time to book.
- **High but falling:** watch for a few days.
- **High and not falling:** holiday fares rarely come back down, so consider booking.
- **Rising:** likely to keep climbing as Christmas nears.

It uses Google's 60-day history when available, and otherwise the trend across this tool's own runs.
The more often it runs, the better the picture. For UK long-haul Christmas trips, KAYAK found
fares were lowest about six weeks before travel (mid-November for a 27–31 Dec departure) and then
climbed.

## Run it on GitHub

`.github/workflows/search.yml` runs a full search on Mondays and Thursdays and re-prices the
shortlist every day. Each run commits to `results/`, so the history builds up in the repo. You can
also start it by hand: Actions → Flight search → Run workflow.

## Tests

```sh
pip install pytest && python -m pytest
```
