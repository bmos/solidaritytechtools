"""Runner: score Members in Good Standing by nearby traffic (AADT) for yard-sign
prioritization, and (optionally) write the score back to a custom user property.

All of the real work — fetching contacts, freeway filtering, snapping to the nearest
sign-visible road, and writing — lives in solidaritytechtools.tools.add_traffic_data and
solidaritytechtools.utils.traffic_score. This script only wires up config and prints a
preview of the highest-traffic members.

Run with dry_run=True first to preview. Writing requires the TRAFFIC_SCORE_PROPERTY
custom property to exist in your ST instance.
"""

import logging
import os
from pathlib import Path

from solidaritytechtools import add_traffic_data
from solidaritytechtools.tools.add_traffic_data import DEFAULT_MAX_AADT, format_address

logger = logging.getLogger(__name__)

# Constants - update for your environment
API_KEY = os.environ.get("ST_API_KEY", "...")
# Optional local WisDOT Traffic Counts GeoJSON; downloaded and cached automatically if None.
GEOJSON_PATH: Path | None = None
# Drop count points above this AADT as freeway-grade (None disables the cap).
MAX_AADT: int | None = DEFAULT_MAX_AADT
# How many top members to preview.
TOP_N = 20


def run(*, dry_run: bool = True) -> None:
    result = add_traffic_data(
        api_key=API_KEY,
        members_in_good_standing_only=True,
        max_aadt=MAX_AADT,
        geojson_path=GEOJSON_PATH,
        dry_run=dry_run,
        refresh=not dry_run,
    )

    logger.info(f"Top {TOP_N} Members in Good Standing by traffic:")
    for contact in result.scored[:TOP_N]:
        address = contact.address
        coords = (
            f"{address.latitude:.5f},{address.longitude:.5f}"
            if address and address.latitude is not None and address.longitude is not None
            else ""
        )
        logger.info(
            f"  {contact.hash_id}  id={contact.user_id}  aadt={contact.aadt:>6}\n"
            f"      addr: {format_address(address)}\n"
            f"      coords: ({coords})   road: {contact.score.location[:50]}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Set dry_run=False when ready to write scores (needs TRAFFIC_SCORE_PROPERTY in ST).
    run(dry_run=True)
