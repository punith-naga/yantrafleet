"""yantrabridge — VDA 5050 v2.1 -> Supabase connector for YantraFleet.

Consumes VDA 5050 ``state`` messages (from an MQTT broker via the optional
paho-mqtt subscriber, or from a JSONL file for testing) and translates them
into Supabase rows:

* upserts into ``robots``
* deduplicated alert rows into ``alerts`` (VDA ``errors[]`` + low battery)
* heartbeat upserts into ``fleet_meta``

The translation and alert-dedup logic (:mod:`yantrabridge.translate`) is pure
and fully unit-testable offline; the HTTP sink (:mod:`yantrabridge.sink`)
accepts an injectable ``httpx`` transport so tests never touch the network.
"""

from yantrabridge.translate import (
    AlertDeduper,
    Translator,
    translate_state,
)
from yantrabridge.sink import SupabaseSink, DEFAULT_SUPABASE_URL

__version__ = "0.1.0"

__all__ = [
    "AlertDeduper",
    "Translator",
    "translate_state",
    "SupabaseSink",
    "DEFAULT_SUPABASE_URL",
    "__version__",
]
