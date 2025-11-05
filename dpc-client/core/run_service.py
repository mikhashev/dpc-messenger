# dpc-client/core/run_service.py

import asyncio
import platform # Import the platform module to check the OS
from dpc_client_core.service import CoreService

async def main():
    """Main entrypoint to start and run the Core Service."""
    service = CoreService()
    
    # --- THE CORE FIX: Cross-platform shutdown logic ---
    
    # Create a future that will be used to signal shutdown
    shutdown_future = asyncio.Future()

    # On Windows, signal handlers are not supported. We rely on KeyboardInterrupt.
    # On other OSes, we can set up a more graceful signal handler.
    if platform.system() != "Windows":
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: shutdown_future.set_result(s))

    # ----------------------------------------------------

    service_task = asyncio.create_task(service.start())

    try:
        # Wait for either the service to finish (it shouldn't) or for a shutdown signal
        await asyncio.wait([service_task, shutdown_future], return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass # This is expected on shutdown
    finally:
        print("\nShutdown initiated...")
        await service.stop()
        # Ensure the main service task is also cancelled
        service_task.cancel()
        try:
            await service_task
        except asyncio.CancelledError:
            pass # Expected

if __name__ == "__main__":
    try:
        print("Starting D-PC Core Service... Press Ctrl+C to stop.")
        asyncio.run(main())
    except KeyboardInterrupt:
        # This is the primary shutdown mechanism on Windows
        print("\nShutdown requested by user (KeyboardInterrupt).")