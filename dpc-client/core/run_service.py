# dpc-client/core/run_service.py

import asyncio
from dpc_client_core.service import CoreService

async def main():
    """Main entrypoint to start and run the Core Service."""
    service = CoreService()
    try:
        await service.start()
    except asyncio.CancelledError:
        print("Main task cancelled, shutting down.")
    finally:
        await service.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutdown requested by user.")