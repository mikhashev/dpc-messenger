"""
Authentication module with JWT tokens and blacklist support.

Handles user authentication, token generation/validation, and logout.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status, WebSocket, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from . import crud, models
from .database import get_db
from .settings import settings
from .token_blacklist import get_blacklist

logger = logging.getLogger(__name__)

# JWT configuration
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30

# Security scheme
oauth2_scheme = HTTPBearer()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token.

    Args:
        data: Dictionary with token claims (must include 'sub')
        expires_delta: Optional expiration time delta

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access"
    })

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    logger.debug(f"Created access token for: {data.get('sub')}")
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT refresh token.

    Args:
        data: Dictionary with token claims (must include 'sub')
        expires_delta: Optional expiration time delta

    Returns:
        Encoded JWT refresh token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh"
    })

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    logger.debug(f"Created refresh token for: {data.get('sub')}")
    return encoded_jwt


def verify_refresh_token(token_str: str) -> Optional[str]:
    """
    Verify a refresh token and return the email if valid.

    Args:
        token_str: JWT refresh token string

    Returns:
        Email from token if valid, None otherwise

    Raises:
        HTTPException: If token is invalid, expired, or not a refresh token
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Check if token is blacklisted
    blacklist = get_blacklist()
    if blacklist.is_blacklisted(token_str):
        logger.warning("Attempted use of blacklisted refresh token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        # Decode token
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        token_type: str = payload.get("type")

        if email is None:
            logger.warning("Refresh token missing 'sub' claim")
            raise credentials_exception

        if token_type != "refresh":
            logger.warning(f"Token is not a refresh token: {token_type}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type. Expected refresh token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return email

    except JWTError as e:
        logger.warning(f"JWT decode error on refresh token: {e}")
        raise credentials_exception


async def _verify_token(token_str: str, db: AsyncSession) -> models.User:
    """
    Verify JWT token and return user.
    
    Core logic to decode a JWT and fetch the user.
    Checks token blacklist for logged out tokens.
    
    Args:
        token_str: JWT token string
        db: Database session
    
    Returns:
        User object
    
    Raises:
        HTTPException: If token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # Check if token is blacklisted (user logged out)
    blacklist = get_blacklist()
    if blacklist.is_blacklisted(token_str):
        logger.warning("Attempted use of blacklisted token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    try:
        # Decode token
        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        token_type: str = payload.get("type")

        if email is None:
            logger.warning("Token missing 'sub' claim")
            raise credentials_exception

        # Ensure it's an access token (not a refresh token)
        if token_type != "access":
            logger.warning(f"Invalid token type for API call: {token_type}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type. Expected access token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    except JWTError as e:
        logger.warning(f"JWT decode error: {e}")
        raise credentials_exception
    
    # Fetch user from database
    user = await crud.get_user_by_email(db, email=email)
    if user is None:
        logger.warning(f"User not found for email: {email}")
        raise credentials_exception
    
    return user


async def get_current_user(
    token: HTTPAuthorizationCredentials = Depends(oauth2_scheme), 
    db: AsyncSession = Depends(get_db)
) -> models.User:
    """
    FastAPI dependency to get the current authenticated user from Bearer token.
    
    Usage:
        @app.get("/protected")
        async def protected_route(user: User = Depends(get_current_user)):
            return {"user": user.email}
    
    Args:
        token: HTTP Bearer token from Authorization header
        db: Database session
    
    Returns:
        Current authenticated User object
    
    Raises:
        HTTPException: If authentication fails
    """
    return await _verify_token(token.credentials, db)


async def get_current_verified_user(
    current_user: models.User = Depends(get_current_user)
) -> models.User:
    """
    FastAPI dependency to get current user with verified node_id.
    
    Use this for endpoints that require cryptographic identity verification.
    
    Args:
        current_user: Current user from get_current_user
    
    Returns:
        Verified User object
    
    Raises:
        HTTPException: If user's node_id is not verified
    """
    if not current_user.node_id_verified:
        logger.warning(f"User {current_user.email} has unverified node_id")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Node identity not verified. Please register your cryptographic identity."
        )
    return current_user


async def get_current_user_ws(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
) -> Optional[models.User]:
    """
    WebSocket authentication dependency.
    
    Authenticates a WebSocket connection via token in query parameter.
    Closes the connection if authentication fails.
    
    Usage:
        @app.websocket("/ws")
        async def websocket_endpoint(
            websocket: WebSocket,
            user: User = Depends(get_current_user_ws)
        ):
            if user is None:
                return  # Connection already closed
            # ... handle websocket
    
    Args:
        websocket: WebSocket connection
        token: JWT token from query parameter
        db: Database session
    
    Returns:
        User object or None if authentication fails
    """
    if not token:
        logger.warning("WebSocket connection without token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    
    try:
        user = await _verify_token(token, db)
        logger.info(f"WebSocket authenticated for user: {user.email}")
        return user
        
    except HTTPException as e:
        logger.warning(f"WebSocket authentication failed: {e.detail}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    except Exception as e:
        logger.error(f"WebSocket authentication error: {e}")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return None


async def verify_websocket_message_auth(
    websocket: WebSocket,
    message: dict,
    db: AsyncSession
) -> Optional[models.User]:
    """
    Verify authentication from WebSocket message.
    
    Alternative authentication method where client sends auth message
    after connecting.
    
    Args:
        websocket: WebSocket connection
        message: Dict with 'type': 'auth' and 'token': '<jwt>'
        db: Database session
    
    Returns:
        User object or None if authentication fails
    """
    if message.get('type') != 'auth':
        return None
    
    token = message.get('token')
    if not token:
        await websocket.send_json({
            'type': 'error',
            'message': 'Missing token in auth message'
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    
    try:
        user = await _verify_token(token, db)
        logger.info(f"WebSocket message auth successful for: {user.email}")
        return user
        
    except HTTPException as e:
        await websocket.send_json({
            'type': 'error',
            'message': e.detail
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None


def blacklist_token(token: str):
    """
    Add a token to the blacklist (logout).
    
    Args:
        token: JWT token string to blacklist
    """
    blacklist = get_blacklist()
    blacklist.add(token)
    logger.info("Token added to blacklist")


def get_token_expiry(token: str) -> Optional[datetime]:
    """
    Get expiration time from a token.
    
    Args:
        token: JWT token string
    
    Returns:
        Expiration datetime or None if invalid
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        exp = payload.get('exp')
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except JWTError:
        pass
    return None


def is_token_expired(token: str) -> bool:
    """
    Check if a token is expired.
    
    Args:
        token: JWT token string
    
    Returns:
        True if expired
    """
    expiry = get_token_expiry(token)
    if expiry is None:
        return True
    return datetime.now(timezone.utc) > expiry