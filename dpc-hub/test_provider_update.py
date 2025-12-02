"""
Test script to verify provider field updates on login.

This script simulates the OAuth callback behavior to test
that the provider field is correctly updated when a user
logs in with a different provider.

Run from dpc-hub directory:
    poetry run python test_provider_update.py
"""

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from dpc_hub.settings import settings
from dpc_hub import crud


async def test_provider_update():
    """Test that provider field updates correctly."""

    # Create async engine
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    test_email = "test_provider_update@example.com"

    async with async_session() as db:
        print(f"\n{'='*60}")
        print("Testing Provider Update Functionality")
        print(f"{'='*60}\n")

        # Clean up any existing test user
        existing_user = await crud.get_user_by_email(db, email=test_email)
        if existing_user:
            await db.delete(existing_user)
            await db.commit()
            print(f"OK: Cleaned up existing test user")

        # Step 1: Create user with Google
        print(f"\nStep 1: User registers with Google")
        print("-" * 40)
        user = await crud.create_user(db, email=test_email, provider="google")
        print(f"OK: Created user: {user.email}")
        print(f"  Provider: {user.provider}")
        assert user.provider == "google", "Provider should be 'google'"

        # Step 2: Simulate login with GitHub (provider update)
        print(f"\nStep 2: User logs in with GitHub")
        print("-" * 40)

        # Fetch user again
        user = await crud.get_user_by_email(db, email=test_email)
        print(f"OK: Fetched existing user: {user.email}")
        print(f"  Current provider: {user.provider}")

        # Update provider (simulating callback logic)
        old_provider = user.provider
        user.provider = "github"
        await db.commit()
        await db.refresh(user)

        print(f"OK: Updated provider: {old_provider} → {user.provider}")
        assert user.provider == "github", "Provider should be 'github'"

        # Step 3: Verify persistence
        print(f"\nStep 3: Verify provider persisted")
        print("-" * 40)
        user = await crud.get_user_by_email(db, email=test_email)
        print(f"OK: Fetched user again: {user.email}")
        print(f"  Provider: {user.provider}")
        assert user.provider == "github", "Provider should still be 'github'"

        # Step 4: Switch back to Google
        print(f"\nStep 4: User logs in with Google again")
        print("-" * 40)
        old_provider = user.provider
        user.provider = "google"
        await db.commit()
        await db.refresh(user)
        print(f"OK: Updated provider: {old_provider} → {user.provider}")
        assert user.provider == "google", "Provider should be 'google'"

        # Cleanup
        print(f"\nCleanup: Removing test user")
        print("-" * 40)
        await db.delete(user)
        await db.commit()
        print(f"OK: Deleted test user")

        print(f"\n{'='*60}")
        print("All tests passed!")
        print(f"{'='*60}\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(test_provider_update())
