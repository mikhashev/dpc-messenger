# dpc-hub/dpc_hub/crud.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload
from . import models, schemas

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(
        select(models.User)
        .options(joinedload(models.User.profile))
        .filter(models.User.email == email)
    )
    return result.scalars().first()

async def create_user(db: AsyncSession, email: str, provider: str):
    # TODO: Generate a real node_id, for now we use a placeholder
    import uuid
    node_id = f"dpc-node-{uuid.uuid4().hex[:16]}"
    
    db_user = models.User(email=email, provider=provider, node_id=node_id)
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user

async def upsert_profile(db: AsyncSession, user: models.User, profile_in: schemas.PublicProfileCreate):
    """
    "Upsert" = UPdate or inSERT.
    """
    if user.profile:
        user.profile.profile_data = profile_in.model_dump() # Pydantic v2
    else:
        db_profile = models.PublicProfile(
            user_id=user.id,
            profile_data=profile_in.model_dump() # Pydantic v2
        )
        db.add(db_profile)
        user.profile = db_profile
    
    await db.commit()
    await db.refresh(user)
    return user.profile

async def get_profile_by_node_id(db: AsyncSession, node_id: str):
    result = await db.execute(
        select(models.PublicProfile)
        .join(models.User)
        .filter(models.User.node_id == node_id)
    )
    return result.scalars().first()