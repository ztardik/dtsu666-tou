"""Process-wide mutable state shared across dtsu666_tou subsystems.

Only ``running`` and ``mqtt_client`` are truly cross-cutting in the
original single-file implementation; they are kept here so every subsystem
can read/update them without circular imports.  All other module-local
state lives in its own module.
"""

running = True
mqtt_client = None
meter = None                 # active meter row, set by the acquisition loop

