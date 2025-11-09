"""
CRUD operations for database models.

Handles user creation, profile management, and node identity registration.
"""

import logging
from sqlalchemy import Integer, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects.postgresql import JSONB

from . import models, schemas
from .crypto_validation import validate_node_registration, CryptoValidationError

logger = logging.getLogger(__name__)


# User Operations

async def get_user_by_email(db: AsyncSession, email: str) -> models.User:
    """
    Get user by email address.
    
    Args:
        db: Database session
        email: User's email address
    
    Returns:
        User object or None if not found
    """
    result = await db.execute(
        select(models.User)
        .options(joinedload(models.User.profile))
        .filter(models.User.email == email)
    )
    return result.scalars().first()


async def get_user_by_node_id(db: AsyncSession, node_id: str) -> models.User:
    """
    Get user by node_id.
    
    Args:
        db: Database session
        node_id: User's node ID
    
    Returns:
        User object or None if not found
    """
    result = await db.execute(
        select(models.User)
        .options(joinedload(models.User.profile))
        .filter(models.User.node_id == node_id)
    )
    return result.scalars().first()


async def create_user(
    db: AsyncSession, 
    email: str, 
    provider: str, 
    node_id: str = None
) -> models.User:
    """
    Create a new user.
    
    If node_id is provided, uses it (for clients registering crypto ID).
    If node_id is None, generates temporary placeholder (for OAuth flow).
    
    Args:
        db: Database session
        email: User's email address
        provider: OAuth provider (google, github, etc.)
        node_id: Optional cryptographic node_id
    
    Returns:
        Created User object
    
    Raises:
        ValueError: If node_id format is invalid
    """
    if node_id is None:
        # Generate temporary node_id during OAuth
        # Client MUST call register_node_id afterward
        import uuid
        node_id = f"dpc-node-temp-{uuid.uuid4().hex[:16]}"
        logger.warning(
            f"Generated temporary node_id for {email}. "
            f"Client must register cryptographic node_id!"
        )
    
    # Validate node_id format
    if not node_id.startswith("dpc-node-"):
        raise ValueError(f"Invalid node_id format: {node_id}")
    
    db_user = models.User(
        email=email, 
        provider=provider, 
        node_id=node_id,
        node_id_verified=False
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    
    logger.info(f"Created user: {email} with node_id: {node_id}")
    return db_user


async def register_node_identity(
    db: AsyncSession,
    user: models.User,
    node_id: str,
    public_key: str,
    certificate: str
) -> models.User:
    """
    Register a user's cryptographic identity.
    
    This should be called immediately after OAuth authentication.
    Validates the cryptographic identity and updates the user record.
    
    Args:
        db: Database session
        user: User object to update
        node_id: Cryptographic node_id
        public_key: PEM-formatted public key
        certificate: PEM-formatted certificate
    
    Returns:
        Updated User object
    
    Raises:
        ValueError: If validation fails or node_id is taken
    """
    # Validate the registration
    try:
        validate_node_registration(node_id, public_key, certificate)
    except CryptoValidationError as e:
        logger.error(f"Node registration validation failed for {user.email}: {e}")
        raise ValueError(f"Invalid node registration: {str(e)}")
    
    # Check if node_id is already taken by another user
    existing = await db.execute(
        select(models.User)
        .filter(models.User.node_id == node_id)
        .filter(models.User.id != user.id)
    )
    if existing.scalars().first():
        logger.error(f"Node ID {node_id} already registered to another user")
        raise ValueError(f"Node ID {node_id} is already registered to another user")
    
    # Update user with verified identity
    user.node_id = node_id
    user.public_key = public_key
    user.certificate = certificate
    user.node_id_verified = True
    
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"Registered cryptographic identity for {user.email}: {node_id}")
    return user


async def delete_user(db: AsyncSession, user: models.User):
    """
    Delete a user and all associated data.
    
    Args:
        db: Database session
        user: User object to delete
    """
    logger.info(f"Deleting user: {user.email} (node_id: {user.node_id})")
    await db.delete(user)
    await db.commit()


# Profile Operations

async def upsert_profile(
    db: AsyncSession, 
    user: models.User, 
    profile_in: schemas.PublicProfileCreate
) -> models.PublicProfile:
    """
    Create or update a user's profile.
    
    Args:
        db: Database session
        user: User object
        profile_in: Profile data
    
    Returns:
        PublicProfile object
    """
    if user.profile:
        # Update existing profile
        user.profile.profile_data = profile_in.model_dump()
        logger.info(f"Updated profile for user: {user.email}")
    else:
        # Create new profile
        db_profile = models.PublicProfile(
            user_id=user.id,
            profile_data=profile_in.model_dump()
        )
        db.add(db_profile)
        user.profile = db_profile
        logger.info(f"Created profile for user: {user.email}")
    
    await db.commit()
    await db.refresh(user)
    return user.profile


async def get_profile_by_node_id(
    db: AsyncSession, 
    node_id: str
) -> models.PublicProfile:
    """
    Get a user's profile by their node_id.
    
    Args:
        db: Database session
        node_id: User's node ID
    
    Returns:
        PublicProfile object or None if not found
    """
    result = await db.execute(
        select(models.PublicProfile)
        .join(models.User)
        .filter(models.User.node_id == node_id)
        .options(joinedload(models.PublicProfile.user))
    )
    return result.scalars().first()


async def delete_profile(db: AsyncSession, user: models.User):
    """
    Delete a user's profile.
    
    Args:
        db: Database session
        user: User object
    """
    if user.profile:
        logger.info(f"Deleting profile for user: {user.email}")
        await db.delete(user.profile)
        await db.commit()


# Search Operations

async def search_profiles_by_expertise(
    db: AsyncSession, 
    topic: str, 
    min_level: int = 1,
    limit: int = 50
) -> list[models.PublicProfile]:
    """
    Search for users by their expertise.
    
    Uses PostgreSQL JSONB operators for efficient searching.
    
    Args:
        db: Database session
        topic: Expertise topic to search for
        min_level: Minimum proficiency level (1-5)
        limit: Maximum number of results
    
    Returns:
        List of PublicProfile objects
    """
    # Sanitize and validate inputs
    if not topic or len(topic) > 100:
        raise ValueError("Invalid topic")
    
    if not 1 <= min_level <= 5:
        raise ValueError("min_level must be between 1 and 5")
    
    # Lowercase and remove special characters
    import re
    topic = re.sub(r'[^a-z0-9_]', '', topic.lower())
    
    logger.info(f"Searching profiles for expertise: {topic} (min_level: {min_level})")
    
    # Query using JSONB operators with index
    result = await db.execute(
        select(models.PublicProfile)
        .where(
            models.PublicProfile.profile_data['expertise'].has_key(topic),
            (models.PublicProfile.profile_data['expertise'][topic]
             .astext.cast(Integer) >= min_level)
        )
        .options(joinedload(models.PublicProfile.user))
        .limit(limit)
    )
    
    profiles = result.scalars().all()
    logger.info(f"Found {len(profiles)} profiles matching '{topic}'")
    return profiles


async def get_all_profiles(
    db: AsyncSession,
    skip: int = 0,
    limit: int = 100
) -> list[models.PublicProfile]:
    """
    Get all profiles with pagination.
    
    Args:
        db: Database session
        skip: Number of records to skip
        limit: Maximum number of records to return
    
    Returns:
        List of PublicProfile objects
    """
    result = await db.execute(
        select(models.PublicProfile)
        .options(joinedload(models.PublicProfile.user))
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


async def count_profiles(db: AsyncSession) -> int:
    """
    Count total number of profiles.
    
    Args:
        db: Database session
    
    Returns:
        Total count of profiles
    """
    result = await db.execute(
        select(func.count()).select_from(models.PublicProfile)
    )
    return result.scalar()


async def count_users(db: AsyncSession) -> int:
    """
    Count total number of users.
    
    Args:
        db: Database session
    
    Returns:
        Total count of users
    """
    result = await db.execute(
        select(func.count()).select_from(models.User)
    )
    return result.scalar()


async def count_verified_users(db: AsyncSession) -> int:
    """
    Count users with verified node_id.
    
    Args:
        db: Database session
    
    Returns:
        Count of verified users
    """
    result = await db.execute(
        select(func.count())
        .select_from(models.User)
        .where(models.User.node_id_verified == True)
    )
    return result.scalar()