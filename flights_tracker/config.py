"""Trip definitions: where from, where to, which dates, and the hard rules."""

from dataclasses import dataclass, field
from datetime import date, timedelta


def days(first: date, last: date) -> list[date]:
    return [first + timedelta(n) for n in range((last - first).days + 1)]


# London airports with long-haul service to these regions.
LONDON_LONG_HAUL = ["LHR", "LGW"]
# Every London airport, for short hops to a European hub.
LONDON_ALL = ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"]

# Leave London 27-31 Dec; leave the destination 7-9 Jan; be home by 10 Jan.
OUTBOUND_DATES = days(date(2026, 12, 27), date(2026, 12, 31))
RETURN_DATES = days(date(2027, 1, 7), date(2027, 1, 9))
LATEST_ARRIVAL_HOME = date(2027, 1, 10)

# Tickets from London straight to the destination: at most 1 stop, at most 16h door to door.
MAX_STOPS = 1
MAX_JOURNEY_MINUTES = 16 * 60

# Going via a European hub on separate tickets: the hub *is* the one stop, so both
# flights must be nonstop. You can stay up to 2 nights there.
MAX_STOPOVER_DAYS = 2
MIN_SELF_TRANSFER_MINUTES = 3 * 60  # separate tickets: leave room for bag re-check and delays
STOPOVER_NIGHT_GBP = 70  # rough hotel cost per night in the hub, so stopovers compare fairly

# Dates to price in-country flights to/from the must-visit city (move them as you like).
HOP_TO_MUST_VISIT = date(2027, 1, 2)
HOP_FROM_MUST_VISIT = date(2027, 1, 5)


@dataclass
class Trip:
    name: str
    # Airports you could fly into / out of. Open-jaw means in and out can differ.
    gateways: dict[str, str]
    # You visit this city at some point, so the in-country cost is in -> must_visit -> out.
    must_visit: str
    # Hubs worth trying as a cheap stepping stone (separate tickets, nonstop each way).
    hubs: dict[str, str]
    # Gateways that actually get nonstop flights from those hubs.
    hub_gateways: list[str]
    # Rough bus/train/ferry cost (GBP) between nearby airports. Other pairs get a live-priced flight.
    overland: dict[frozenset, int] = field(default_factory=dict)


MEXICO = Trip(
    name="Mexico",
    gateways={
        "MEX": "Mexico City",
        "CUN": "Cancún",
        "PVR": "Puerto Vallarta",
        "SJD": "Los Cabos",
        "GDL": "Guadalajara",
        "MID": "Mérida",
        "OAX": "Oaxaca",
        "HUX": "Huatulco",
    },
    must_visit="MEX",
    hubs={
        "MAD": "Madrid", "BCN": "Barcelona", "CDG": "Paris", "AMS": "Amsterdam",
        "FRA": "Frankfurt", "MUC": "Munich", "DUS": "Düsseldorf", "ZRH": "Zurich",
        "MXP": "Milan", "FCO": "Rome", "BRU": "Brussels", "LIS": "Lisbon",
        "WAW": "Warsaw", "IST": "Istanbul",
    },
    hub_gateways=["MEX", "CUN", "PVR", "SJD"],
    overland={
        frozenset({"CUN", "MID"}): 25,  # ADO bus ~4h
        frozenset({"OAX", "HUX"}): 30,  # shuttle ~6h
        frozenset({"MEX", "OAX"}): 40,  # ADO bus ~6.5h
        frozenset({"GDL", "PVR"}): 35,  # bus ~5h
        frozenset({"MEX", "GDL"}): 40,  # bus ~7h
    },
)

MALAYSIA = Trip(
    name="Malaysia",
    gateways={
        "KUL": "Kuala Lumpur",
        "PEN": "Penang",
        "LGK": "Langkawi",
        "BKI": "Kota Kinabalu (Borneo)",
        "KCH": "Kuching (Borneo)",
        # Often far cheaper from London/Europe, and a 5h bus from KL.
        "SIN": "Singapore (gateway)",
    },
    must_visit="KUL",
    hubs={
        "IST": "Istanbul", "AMS": "Amsterdam", "CDG": "Paris", "FRA": "Frankfurt",
        "MUC": "Munich", "ZRH": "Zurich", "MXP": "Milan", "FCO": "Rome",
        "BCN": "Barcelona", "MAD": "Madrid", "CPH": "Copenhagen", "HEL": "Helsinki",
        # Not Europe, but the classic cheap stepping stones to Asia.
        "DOH": "Doha", "DXB": "Dubai", "AUH": "Abu Dhabi", "MCT": "Muscat",
    },
    hub_gateways=["KUL", "PEN", "SIN"],
    overland={
        frozenset({"KUL", "SIN"}): 20,  # bus ~5h
        frozenset({"KUL", "PEN"}): 15,  # ETS train ~4h
        frozenset({"PEN", "LGK"}): 20,  # ferry
    },
)

TRIPS = {"mexico": MEXICO, "malaysia": MALAYSIA}
