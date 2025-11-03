# dpc-hub/dpc_hub/auth.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi import WebSocket, Query, status as fastapi_status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials 
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from . import crud, models, schemas
from .database import get_db
from .settings import settings

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = HTTPBearer()

async def _verify_token(token_str: str, db: AsyncSession) -> models.User:
    """Core logic to decode a JWT and fetch the user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = await crud.get_user_by_email(db, email=email)
    if user is None:
        raise credentials_exception
    return user

async def get_current_user(token: HTTPAuthorizationCredentials = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    """FastAPI dependency to get the current user from a Bearer token."""
    return await _verify_token(token.credentials, db)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
#     credentials_exception = HTTPException(
#         status_code=status.HTTP_401_UNAUTHORIZED,
#         detail="Could not validate credentials",
#         headers={"WWW-Authenticate": "Bearer"},
#     )
#     try:
#         token_str = token.credentials
#         payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
#         email: str = payload.get("sub")
#         if email is None:
#             raise credentials_exception
#     except (JWTError, AttributeError):
#         raise credentials_exception
    
#     user = await crud.get_user_by_email(db, email=email)
#     if user is None:
#         raise credentials_exception
#     return user

async def get_current_user_ws(
    websocket: WebSocket,
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db)
) -> models.User | None:
    """
    Dependency to authenticate a user via a token in the query parameter
    for a WebSocket connection.
    """
    if token is None:
        await websocket.close(code=fastapi_status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return None
    try:
        user = await _verify_token(token, db)
        return user
    except HTTPException:
        await websocket.close(code=fastapi_status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
        return None