"""yantrasim — warehouse AMR fleet simulator emitting VDA 5050 v2.1 state.

Layout:
    world      -- static warehouse waypoint graph (nodes, edges, BFS paths)
    sim        -- pure simulation core (robots, battery, tasks, faults)
    vda        -- VDA 5050 v2.1 message builders (pure functions)
    translate  -- VDA state -> Supabase table rows (pure functions)
    transports -- Supabase (httpx bulk upsert) and MQTT (optional paho)
"""

__version__ = "0.1.0"
