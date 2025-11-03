# dpc-hub/dpc_hub/crud.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from . import models

async def get_user_by_email(db: AsyncSession, email: str):
    result = await db.execute(select(models.User).filter(models.User.email == email))
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