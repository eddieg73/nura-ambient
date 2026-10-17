# contracts/

`omi-routes.json` is the extracted route table from BasedHardware/omi at
HEAD 2391c059d90cf9dbd6bb63e6d4f2f17fc23c6c20 - 712 routes across 113 router
modules, parsed from the FastAPI decorators.

Format: `[module, METHOD, path]` per entry.

This is a reference artifact, not generated code. It exists so the size and shape
of the surface we are choosing NOT to match stays visible. See
docs/ROUTE-SURFACE.md.
