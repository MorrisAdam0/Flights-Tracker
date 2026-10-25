# Flights-Tracker

Searches Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights)) for every
combination of a London → Mexico or London → Malaysia winter trip, and ranks them by total cost.

**Rules** (edit in `flights_tracker/config.py`):

- Leave London (LHR/LGW) 28, 29 or 30 Dec 2026
- Leave for home 8, 9 or 10 Jan 2027, landing in London by 10 Jan
- At most 1 stop, at most 16h door to door each way (layover included)

**What it searches**

1. One-way out of London to every gateway, and one-way back from every gateway, on every date.
2. Every pairing of those as an open-jaw trip (fly into Cancún, home from Mexico City, etc.), adding
   the cost of getting between the two gateways: a rough bus/train/ferry estimate for nearby
   pairs, or a live-priced domestic flight on 4 Jan for the rest.
3. Round-trip fares to the main hubs (CUN, MEX, KUL, SIN), which are sometimes cheaper than
   two one-ways. The return leg of these isn't checked against the rules, so check it when booking.

Gateways: Mexico — CUN, MEX, PVR, SJD, GDL, MID, OAX, HUX. Malaysia — KUL, PEN, LGK, BKI, KCH,
plus Singapore (SIN), which is often cheaper from London and a 5h bus from KL.

## Run it

```sh
pip install -r requirements.txt
python -m flights_tracker              # both trips (~350 searches, ~10 min)
python -m flights_tracker --trip mexico
```

Output in `results/`:

- `latest.md` — ranked combos with Google Flights links for each leg
- `<trip>_legs_<date>.csv` — every flight found, with a `meets_rules` column
- `history.csv` — the best total from each run, to see whether prices are moving

Results are cached for the day in `.cache/`; use `--no-cache` to refetch.

## Run it on GitHub

`.github/workflows/search.yml` runs daily and on demand (Actions → Flight search → Run workflow).
The report appears on the run's summary page and is committed to `results/`.

## Tests

```sh
pip install pytest && python -m pytest
```
