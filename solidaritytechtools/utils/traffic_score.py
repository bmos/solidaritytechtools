"""Assign average daily traffic (AADT) to coordinates for yard-sign prioritization.

WisDOT publishes ~27k statewide traffic-count points, each with a RDWY_AADT value
(Annual Average Daily Traffic = average vehicles/day on that road segment) and a
lat/lon. Given a coordinate, we snap it to the nearest count point and report that
point's AADT as the location's "traffic score". If the nearest count point is
farther than ``max_distance_m``, the score is "no data" (aadt=None) rather than
attributing a distant road's traffic to the address.

Use this to build Solidarity Tech pull lists that prioritize people with the most
cars passing their house: score each member's coordinates, then sort high-to-low.

This module is self-contained (stdlib + httpx). If ``TRAFFIC_GEOJSON_PATH`` is
None, the WisDOT GeoJSON is downloaded to a cached file in the system temp dir on
first use.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

# WisDOT Traffic Counts open dataset (ArcGIS Hub), GeoJSON in WGS84 (lat/lon).
# URL as of 6/22/2026. If it is not valid, go to the following URL, "Downloads" section:
# https://data-wisdot.opendata.arcgis.com/datasets/WisDOT::traffic-counts/explore?location=44.697798%2C-89.852372%2C8
TRAFFIC_GEOJSON_URL: Final[str] = (
    "https://hub.arcgis.com/api/v3/datasets/"
    "c99c497fae6c4d5d8f5453ea9237c679_0/downloads/data"
    "?format=geojson&spatialRefId=4326&where=1%3D1"
)

# Set this to use a local GeoJSON file instead of downloading. When None, the file
# is downloaded to a cached path in the system temp dir on first use.
TRAFFIC_GEOJSON_PATH: Final[Path | None] = None

# Cached download location used when TRAFFIC_GEOJSON_PATH is None.
TEMP_GEOJSON_FILENAME: Final[str] = "widot_traffic_counts.geojson"

# Beyond this distance from the nearest count point, a location gets "no data"
# rather than borrowing a far-off road's traffic. ~Median spacing between counts.
DEFAULT_MAX_DISTANCE_M: Final[float] = 400.0

# When a street_hint is given, we look for a count on that same named road. Same-road
# counts are sparse, so we allow a wider radius than the plain (cross-street-prone)
# snap: a count up to ~1 mi away on YOUR street is a far better proxy than a closer
# count on a different street.
DEFAULT_STREET_MATCH_MAX_DISTANCE_M: Final[float] = 1600.0

# GeoJSON property names in the WisDOT dataset.
_AADT_FIELD: Final[str] = "RDWY_AADT"
_YEAR_FIELD: Final[str] = "AADT_RPTG_YR"
_LOCATION_FIELD: Final[str] = "TRFC_PT_LOC_DESC"
_COUNTY_FIELD: Final[str] = "COUNTY_NAME"

_EARTH_RADIUS_M: Final[float] = 6_371_000.0
_DOWNLOAD_TIMEOUT_S: Final[float] = 120.0

# Spatial grid cell size in degrees (~1.1 km in latitude) for nearest-neighbor.
_GRID_CELL_DEG: Final[float] = 0.01
# Stop expanding the ring search past this radius (nothing in WI is this far).
_MAX_SEARCH_M: Final[float] = 50_000.0

# Tokens dropped when reducing a road name/description to its bare street name, so
# that "N OAKLAND AVE", "Oakland Ave", and "oakland" all normalize to "OAKLAND".
_DIRECTIONALS: Final[frozenset[str]] = frozenset(
    {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NO", "SO", "NORTH", "SOUTH", "EAST", "WEST"}
)
_STREET_TYPES: Final[frozenset[str]] = frozenset(
    {
        "AVE",
        "AV",
        "AVENUE",
        "ST",
        "STR",
        "STREET",
        "BLVD",
        "BOULEVARD",
        "DR",
        "DRIVE",
        "RD",
        "ROAD",
        "CT",
        "COURT",
        "PL",
        "PLACE",
        "LN",
        "LANE",
        "WAY",
        "PKWY",
        "PKY",
        "PARKWAY",
        "HWY",
        "HIGHWAY",
        "CIR",
        "CIRCLE",
        "TER",
        "TERR",
        "TERRACE",
        "TRL",
        "TRAIL",
        "PT",
        "POINT",
        "SQ",
        "SQUARE",
        "PLZ",
        "PLAZA",
        "RUN",
        "PASS",
        "PATH",
    }
)
# Highway route classes (e.g. "STH 190", "USH 41", "CTH A") — stripped with their number.
_ROUTE_CLASSES: Final[frozenset[str]] = frozenset(
    {"STH", "USH", "IH", "CTH", "US", "STATE", "COUNTY", "HWY", "CTY"}
)
_ROUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:I|IH|STH|USH|CTH|CTY|US|STATE|COUNTY|HWY)\.?\s*[-A-Z]?\d+\b"
)

# Freeway-class counts (interstates, ramps, expressways) carry huge volumes that no
# yard sign benefits from — a house next to I-43 should not inherit 130,000 cars/day.
# Detected by name, or by a volume backstop above anything a surface street carries.
# Note: USH/STH are deliberately NOT treated as freeways — most are named surface
# arterials (e.g. "STH 190 CAPITOL DR", "USH 18 BLUEMOUND RD").
FREEWAY_AADT_BACKSTOP: Final[int] = 50_000
_FREEWAY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:I[-\s]?\d+|IH\b|FREEWAY|FRWY|EXPRESSWAY|EXPWY|EXPY|INTERSTATE|RAMP)\b"
)
# Splits a count description at the first cross-street reference so we keep only the
# road that was actually counted: e.g. "E LOCUST ST W OF N OAKLAND AVE" -> "E LOCUST ST".
_CROSS_STREET_RE: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:"
    r"(?:[NSEW]{1,2}|NO|SO|NORTH|SOUTH|EAST|WEST)\s+OF"  # "W OF", "NO OF", "SOUTH OF"
    r"|OF"  # bare "OF"
    r"|BTWN|BETWEEN|BET|AT|NEAR|TO"
    r"|\d+(?:\.\d+)?\s+MI"  # mileage markers, e.g. "0.5 MI EAST OF"
    r")\b"
)
_LEADING_HOUSE_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\d+[A-Z]?\s+")

# WisDOT often labels a count only by route number ("STH 31 BTWN STH 20 & NEWMAN RD")
# with no local street name, so street_hint can't match someone who lives on the road's
# common name (Green Bay Rd). But other descriptions DO spell it out ("...STH 31 GREEN
# BAY RD"), so we mine those to recover each route's local name. Route->name is regional
# (STH 100 is Mayfair Rd here, 108th St elsewhere), so a route-only count borrows the
# name from the nearest same-route count that has one, within this distance.
_ROUTE_NAME_MAX_M: Final[float] = 20_000.0
_STREET_TYPE_ALT: Final[str] = "|".join(sorted(_STREET_TYPES))  # \b makes order irrelevant
# Cross-street words: a road name must not span these (so "STH 20 WEST OF OHIO AVE" does
# not wrongly read OHIO as STH 20's name — OHIO is the cross street).
_NAME_STOPWORD_ALT: Final[str] = r"OF|BTWN|BETWEEN|BET|AT|NEAR|TO|AND"
_NO_STOPWORD: Final[str] = r"(?:(?!(?:" + _NAME_STOPWORD_ALT + r")\b)[A-Z]+)"
# A route designation immediately followed by its local name. Two forms:
#  - with a street type: "STH 190 E CAPITOL DR", "STH 31 GREEN BAY RD"
_ROUTE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(I|IH|US|USH|STH|CTH)\b[\s./-]*(\d+)\s*/?\s*"
    r"((?:[NSEW]\s+)?(?:" + _NO_STOPWORD + r"\s+){1,3}?(?:" + _STREET_TYPE_ALT + r"))\b"
)
#  - without one, when the name is bounded by "&", a number, or a cross-street word:
#    "STH 20 WASHINGTON & 13TH ST" -> WASHINGTON
_ROUTE_NAME_NOTYPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(I|IH|US|USH|STH|CTH)\b[\s./-]*(\d+)\s*/?\s*(?:[NSEW]\s+)?"
    r"(" + _NO_STOPWORD + r"{3,})"
    r"(?=\s*(?:&|\d|/|$|(?:" + _NAME_STOPWORD_ALT + r")\b))"
)
_ROUTE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b(I|IH|US|USH|STH|CTH)\b[\s./-]*(\d+)")
# Collapse equivalent route-class spellings so "I-43"/"IH 43" and "US 18"/"USH 18" agree.
_ROUTE_CANON: Final[dict[str, str]] = {"IH": "I", "I": "I", "US": "USH", "USH": "USH"}


def _normalize_road_name(text: str) -> str:
    """Reduce a road name or count description to a bare, comparable street name.

    Drops route designations, directionals, street-type suffixes, and bare house/route
    numbers. Returns "" when nothing nameable remains (e.g. an unnamed highway segment).
    """
    upper = text.upper()
    upper = _ROUTE_RE.sub(" ", upper)
    upper = re.sub(r"[^A-Z0-9 ]", " ", upper)
    tokens = [
        tok
        for tok in upper.split()
        if tok not in _DIRECTIONALS
        and tok not in _STREET_TYPES
        and tok not in _ROUTE_CLASSES
        and not tok.isdigit()  # bare house/route numbers; keeps ordinals like "76TH"
    ]
    return " ".join(tokens)


def _primary_segment(description: str) -> str:
    """The part of a count description before any cross-street reference."""
    match = _CROSS_STREET_RE.search(description)
    return description[: match.start()] if match else description


def _count_road_name(description: str) -> str:
    """Street name of the road a count was taken on (the part before any cross street)."""
    return _normalize_road_name(_primary_segment(description))


def _hint_road_name(street_hint: str) -> str:
    """Normalize a caller-supplied street line (e.g. "2600 N Oakland Ave") for matching."""
    return _normalize_road_name(_LEADING_HOUSE_NUMBER_RE.sub("", street_hint))


def _route_key(route_class: str, number: str) -> str:
    return f"{_ROUTE_CANON.get(route_class, route_class)}{number}"


def _extract_route_names(description: str) -> list[tuple[str, str]]:
    """Recover (route_key, local_name) pairs spelled out in a description.

    e.g. "STH 20 WEST OF STH 31 GREEN BAY RD" -> [("STH31", "GREEN BAY")].
    """
    upper = description.upper()
    pairs = []
    for regex in (_ROUTE_NAME_RE, _ROUTE_NAME_NOTYPE_RE):
        for match in regex.finditer(upper):
            name = _normalize_road_name(match.group(3))
            if name:
                pairs.append((_route_key(match.group(1), match.group(2)), name))
    return pairs


def _primary_route_key(primary: str) -> str | None:
    """The route key of a route-only primary segment (e.g. "STH 31" -> "STH31")."""
    match = _ROUTE_TOKEN_RE.search(primary.upper())
    return _route_key(match.group(1), match.group(2)) if match else None


def _nearest_route_name(lat: float, lon: float, named: list[tuple[float, float, str]]) -> str:
    """Local name of the nearest same-route count that has one, within range."""
    best = ""
    best_d = _ROUTE_NAME_MAX_M
    for nlat, nlon, name in named:
        d = _haversine_m(lat, lon, nlat, nlon)
        if d < best_d:
            best_d, best = d, name
    return best


def _is_freeway(description: str, aadt: int) -> bool:
    """True for interstate/ramp/expressway counts that shouldn't score a yard sign."""
    return aadt >= FREEWAY_AADT_BACKSTOP or bool(_FREEWAY_RE.search(description.upper()))


@dataclass(frozen=True)
class CountPoint:
    lat: float
    lon: float
    aadt: int
    year: int | None
    location: str
    county: str
    road_name: str  # normalized name of the counted road, for street_hint matching
    is_freeway: bool  # interstate / ramp / expressway — excluded from yard-sign scoring


@dataclass(frozen=True)
class TrafficScore:
    """Result for one coordinate. ``aadt is None`` means no counted road nearby."""

    aadt: int | None
    year: int | None
    distance_m: float | None
    location: str
    county: str

    @property
    def has_data(self) -> bool:
        return self.aadt is not None


_NO_DATA: Final[TrafficScore] = TrafficScore(
    aadt=None, year=None, distance_m=None, location="", county=""
)


def _to_score(point: CountPoint, distance_m: float) -> TrafficScore:
    return TrafficScore(
        aadt=point.aadt,
        year=point.year,
        distance_m=round(distance_m, 1),
        location=point.location,
        county=point.county,
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _download_geojson(url: str = TRAFFIC_GEOJSON_URL) -> Path:
    """Download the WisDOT GeoJSON to a cached temp file, reusing it if present."""
    cache_path = Path(tempfile.gettempdir()) / TEMP_GEOJSON_FILENAME
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_S) as response:
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    tmp_path.replace(cache_path)
    return cache_path


def _resolve_geojson_path() -> Path:
    if TRAFFIC_GEOJSON_PATH is not None:
        return TRAFFIC_GEOJSON_PATH
    return _download_geojson()


def _load_points(path: Path) -> list[CountPoint]:
    data = json.loads(Path(path).read_text())

    # Pass 1: parse raw records, and collect where each route's local name is spelled out.
    raw: list[dict] = []
    route_named: dict[str, list[tuple[float, float, str]]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        aadt = props.get(_AADT_FIELD)
        if aadt is None or not coords:
            continue
        lon, lat = coords[0], coords[1]
        location = props.get(_LOCATION_FIELD) or ""
        primary = _primary_segment(location)
        name = _normalize_road_name(primary)
        for route_key, route_name in _extract_route_names(location):
            route_named.setdefault(route_key, []).append((lat, lon, route_name))
        raw.append(
            {
                "lat": lat,
                "lon": lon,
                "aadt": int(aadt),
                "year": props.get(_YEAR_FIELD),
                "location": location,
                "county": props.get(_COUNTY_FIELD) or "",
                "name": name,
                "route_key": _primary_route_key(primary) if not name else None,
            }
        )

    # Pass 2: a route-only count (no local name) borrows the nearest same-route name.
    points: list[CountPoint] = []
    for r in raw:
        road_name = r["name"]
        if not road_name and r["route_key"]:
            road_name = _nearest_route_name(r["lat"], r["lon"], route_named.get(r["route_key"], []))
        points.append(
            CountPoint(
                lat=r["lat"],
                lon=r["lon"],
                aadt=r["aadt"],
                year=r["year"],
                location=r["location"],
                county=r["county"],
                road_name=road_name,
                is_freeway=_is_freeway(r["location"], r["aadt"]),
            )
        )
    return points


class TrafficScorer:
    """Builds a spatial index over the count points for fast nearest lookups.

    Loading and indexing the ~27k points is done once at construction; scoring is
    then cheap, so reuse a single instance across many addresses.
    """

    def __init__(self, geojson_path: Path | None = None) -> None:
        path = geojson_path if geojson_path is not None else _resolve_geojson_path()
        self.points: list[CountPoint] = _load_points(path)
        self._grid: dict[tuple[int, int], list[CountPoint]] = {}
        for point in self.points:
            self._grid.setdefault(self._cell(point.lat, point.lon), []).append(point)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat / _GRID_CELL_DEG), int(lon / _GRID_CELL_DEG))

    def _nearest(
        self,
        lat: float,
        lon: float,
        stop_distance_m: float,
        road_name: str | None = None,
        exclude_freeways: bool = True,
    ) -> tuple[CountPoint | None, float]:
        """Return (nearest_point, distance_m), searching outward in grid rings.

        Abandons once it's certain the nearest point is farther than
        ``stop_distance_m`` (a definitive "no data"); the returned distance is then
        a lower bound, which keeps no-data lookups in empty areas from scanning far.

        When ``road_name`` is given, only count points on that same named road are
        considered, so an address never inherits a parallel street's traffic. When
        ``exclude_freeways`` is set, interstate/ramp/expressway counts are skipped.
        """
        ci, cj = self._cell(lat, lon)
        best: CountPoint | None = None
        best_d = math.inf
        # Smallest cell edge in meters (longitude shrinks with latitude); once a
        # candidate is closer than the radius fully covered by searched rings,
        # nothing unsearched can be nearer.
        cell_m = _GRID_CELL_DEG * 111_320 * max(math.cos(math.radians(lat)), 0.1)
        ring = 0
        while True:
            for i in range(ci - ring, ci + ring + 1):
                for j in range(cj - ring, cj + ring + 1):
                    # Only the outer shell of the (2*ring+1) box is new each ring.
                    if ring > 0 and ci - ring < i < ci + ring and cj - ring < j < cj + ring:
                        continue
                    for point in self._grid.get((i, j), ()):
                        if road_name is not None and point.road_name != road_name:
                            continue
                        if exclude_freeways and point.is_freeway:
                            continue
                        d = _haversine_m(lat, lon, point.lat, point.lon)
                        if d < best_d:
                            best_d, best = d, point
            covered = ring * cell_m
            if best is not None and best_d <= covered:
                break
            if covered >= stop_distance_m and best_d > stop_distance_m:
                break
            ring += 1
            if ring * cell_m > _MAX_SEARCH_M:
                break
        return best, best_d

    def score(
        self,
        lat: float,
        lon: float,
        *,
        street_hint: str | None = None,
        max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
        street_match_max_distance_m: float = DEFAULT_STREET_MATCH_MAX_DISTANCE_M,
        fallback_to_nearest: bool = False,
        exclude_freeways: bool = True,
    ) -> TrafficScore:
        """Score a coordinate, optionally constrained to a known street.

        Without ``street_hint``, snaps to the nearest count within ``max_distance_m``.

        With ``street_hint`` (the member's street line, e.g. "2600 N Oakland Ave"),
        snaps only to a count *on that same road* within ``street_match_max_distance_m``.
        This is far more accurate in a dense grid: a quiet street no longer inherits a
        parallel arterial's count. If there's no count on that road nearby, the result
        is "no data" — unless ``fallback_to_nearest`` is set, which then falls back to
        the plain nearest-count snap within ``max_distance_m``.

        ``exclude_freeways`` (default True) drops interstate/ramp/expressway counts so a
        house never inherits a freeway's volume — set False only if you actually want it.
        """
        hint_name = _hint_road_name(street_hint) if street_hint else ""
        if hint_name:
            point, dist = self._nearest(
                lat,
                lon,
                stop_distance_m=street_match_max_distance_m,
                road_name=hint_name,
                exclude_freeways=exclude_freeways,
            )
            if point is not None and dist <= street_match_max_distance_m:
                return _to_score(point, dist)
            if not fallback_to_nearest:
                return _NO_DATA

        point, dist = self._nearest(
            lat, lon, stop_distance_m=max_distance_m, exclude_freeways=exclude_freeways
        )
        if point is None or dist > max_distance_m:
            return _NO_DATA
        return _to_score(point, dist)

    def score_many(
        self,
        coordinates: list[tuple[float, float]],
        *,
        street_hints: list[str | None] | None = None,
        max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
        street_match_max_distance_m: float = DEFAULT_STREET_MATCH_MAX_DISTANCE_M,
        fallback_to_nearest: bool = False,
        exclude_freeways: bool = True,
    ) -> list[TrafficScore]:
        """Score a list of (lat, lon) pairs, returning results in the same order.

        Pass ``street_hints`` (same length/order as ``coordinates``) to constrain each
        lookup to its known street; use None for entries with no known street.
        """
        if street_hints is not None and len(street_hints) != len(coordinates):
            raise ValueError("street_hints must be the same length as coordinates")
        return [
            self.score(
                lat,
                lon,
                street_hint=street_hints[i] if street_hints is not None else None,
                max_distance_m=max_distance_m,
                street_match_max_distance_m=street_match_max_distance_m,
                fallback_to_nearest=fallback_to_nearest,
                exclude_freeways=exclude_freeways,
            )
            for i, (lat, lon) in enumerate(coordinates)
        ]


_default_scorer: TrafficScorer | None = None


def get_default_scorer() -> TrafficScorer:
    """Return a process-wide scorer, building (and downloading) it on first call."""
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = TrafficScorer()
    return _default_scorer


def score_address(
    lat: float,
    lon: float,
    *,
    street_hint: str | None = None,
    max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
    street_match_max_distance_m: float = DEFAULT_STREET_MATCH_MAX_DISTANCE_M,
    fallback_to_nearest: bool = False,
    exclude_freeways: bool = True,
) -> TrafficScore:
    """Convenience one-shot scorer using a shared, lazily built index."""
    return get_default_scorer().score(
        lat,
        lon,
        street_hint=street_hint,
        max_distance_m=max_distance_m,
        street_match_max_distance_m=street_match_max_distance_m,
        fallback_to_nearest=fallback_to_nearest,
        exclude_freeways=exclude_freeways,
    )
