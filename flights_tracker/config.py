"""Trip definitions: where from, where to, which dates, and the hard rules."""

from dataclasses import dataclass, field
from datetime import date

# London airports with long-haul service. STN/LTN have no long-haul to these regions.
LONDON = ["LHR", "LGW"]

# Outbound: 29 Dec +/- 1 day.
OUTBOUND_DATES = [date(2026, 12, 28), date(2026, 12, 29), date(2026, 12, 30)]
# Return: leave 9 Jan +/- 1 day, but you must be back in London by end of 10 Jan.
RETURN_DATES = [date(2027, 1, 8), date(2027, 1, 9), date(2027, 1, 10)]
LATEST_ARRIVAL_HOME = date(2027, 1, 10)

MAX_STOPS = 1
MAX_JOURNEY_MINUTES = 16 * 60  # door-to-door per direction, including the layover


@dataclass
class Trip:
    name: str
    # Airports you could fly into / out of. Open-jaw means in and out can differ.
    gateways: dict[str, str]
    # Airports worth pricing as a round trip (same in and out).
    round_trip_hubs: list[str] = field(default_factory=list)
    # Rough overland/ferry/bus cost (GBP) to get between gateways that are close.
    # Pairs not listed are assumed to need a domestic flight, which is priced live.
    overland: dict[frozenset, int] = field(default_factory=dict)


MEXICO = Trip(
    name="Mexico",
    gateways={
        "CUN": "Cancún",
        "MEX": "Mexico City",
        "PVR": "Puerto Vallarta",
        "SJD": "Los Cabos",
        "GDL": "Guadalajara",
        "MID": "Mérida",
        "OAX": "Oaxaca",
        "HUX": "Huatulco",
    },
    round_trip_hubs=["CUN", "MEX"],
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
        # Singapore is often far cheaper from London and is a bus/train ride from Malaysia.
        "SIN": "Singapore (gateway)",
    },
    round_trip_hubs=["KUL", "SIN"],
    overland={
        frozenset({"KUL", "SIN"}): 20,  # bus ~5h
        frozenset({"KUL", "PEN"}): 15,  # ETS train ~4h
        frozenset({"PEN", "LGK"}): 20,  # ferry
    },
)

TRIPS = {"mexico": MEXICO, "malaysia": MALAYSIA}
