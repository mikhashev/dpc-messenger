# dpc-hub/dpc_hub/main.py
from fastapi import FastAPI

app = FastAPI(
    title="D-PC Federation Hub",
    version="1.0.0",
    description="The central discovery and signaling server for the D-PC network."
)

@app.get("/")
async def root():
    """A simple health check endpoint for the server."""
    return {"status": "ok", "project": "D-PC Hub", "version": app.version}

# We will add other endpoints here (profiles, search, etc.)