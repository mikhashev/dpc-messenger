"""
D-PC Federation Hub - Main Application

FastAPI application with OAuth authentication, WebSocket signaling,
profile management, and discovery features.
"""

import asyncio
import json
import logging
import traceback
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import (
    FastAPI, Depends, Request, HTTPException, 
    WebSocket, WebSocketDisconnect, Query, status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from . import crud, models, schemas, auth
from .database import get_db
from .settings import settings
from .connection_manager import manager
from .token_blacklist import get_blacklist, start_blacklist, stop_blacklist
from .crypto_validation import CryptoValidationError

# Configure logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Application startup time for uptime tracking
app_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan events.
    
    Handles startup and shutdown tasks.
    """
    # Startup
    logger.info("🚀 Starting D-PC Federation Hub")
    logger.info(f"   Version: {settings.APP_VERSION}")
    logger.info(f"   Debug: {settings.DEBUG}")
    
    # Start token blacklist cleanup
    start_blacklist()
    logger.info("   Token blacklist started")
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down D-PC Federation Hub")
    
    # Stop token blacklist
    await stop_blacklist()
    logger.info("   Token blacklist stopped")
    
    # Close all WebSocket connections
    await manager.close_all()
    logger.info("   WebSocket connections closed")
    
    logger.info("✅ Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="D-PC Federation Hub",
    version=settings.APP_VERSION,
    description="""
    # D-PC Federation Hub API
    
    The central discovery and signaling server for the D-PC network.
    
    ## Features
    - 🔐 OAuth authentication (Google, GitHub)
    - 👤 User profiles with expertise search
    - 🔍 Discovery API for finding peers
    - 📡 WebRTC signaling via WebSocket
    - 🛡️ Cryptographic identity verification
    - 🚀 Rate limiting and security features
    
    ## Authentication
    All endpoints (except /auth/*) require Bearer token authentication.
    Get a token by logging in via OAuth.
    
    ## WebSocket
    WebRTC signaling is done via WebSocket at `/ws/signal?token=<JWT>`.
    """,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
    debug=settings.DEBUG
)

# Middleware Configuration

# 1. Sessions (required for OAuth)
app.add_middleware(
    SessionMiddleware, 
    secret_key=settings.SECRET_KEY,
    max_age=3600  # 1 hour
)

# 2. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"]
)

# 3. Gzip compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 4. Trusted hosts (production)
if settings.is_production:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*.dpc.network", "hub.dpc.network"]
    )

# Rate Limiting
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"] if settings.RATE_LIMIT_ENABLED else []
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Custom Middleware for Request ID and Security Headers
@app.middleware("http")
async def add_security_and_request_id(request: Request, call_next):
    """Add security headers and request ID to all responses"""
    import uuid
    
    # Generate request ID
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    # Process request
    try:
        response = await call_next(request)
    except Exception as e:
        logger.error(f"Request {request_id} failed: {e}")
        raise
    
    # Add headers
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    
    return response


# Exception Handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions"""
    request_id = getattr(request.state, 'request_id', 'unknown')
    
    logger.error(f"Request {request_id} - Unhandled exception:")
    logger.error(traceback.format_exc())
    
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "request_id": request_id,
            "type": "internal_error"
        }
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Handle validation errors"""
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "type": "validation_error"}
    )


# OAuth Configuration
oauth = OAuth()

oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    client_kwargs={'scope': 'openid email profile'}
)

if settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET:
    oauth.register(
        name='github',
        access_token_url='https://github.com/login/oauth/access_token',
        authorize_url='https://github.com/login/oauth/authorize',
        api_base_url='https://api.github.com/',
        client_id=settings.GITHUB_CLIENT_ID,
        client_secret=settings.GITHUB_CLIENT_SECRET,
        client_kwargs={'scope': 'user:email'}
    )


# ============================================================================
# HEALTH CHECK & INFO ENDPOINTS
# ============================================================================

@app.get("/", tags=["Health"])
async def root():
    """Simple root endpoint"""
    return {
        "status": "ok",
        "project": settings.APP_NAME,
        "version": settings.APP_VERSION
    }


@app.get("/health", response_model=schemas.HealthCheckResponse, tags=["Health"])
@limiter.exempt  # Don't rate limit health checks
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Comprehensive health check with database and service status.
    
    Returns system status including:
    - Overall health status
    - Database connectivity
    - WebSocket connection count
    - Token blacklist size
    - Uptime
    """
    health_status = "healthy"
    db_status = "healthy"
    
    # Test database connection
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = f"unhealthy: {str(e)}"
        health_status = "degraded"
    
    # Get metrics
    blacklist = get_blacklist()
    uptime = time.time() - app_start_time
    
    return {
        "status": health_status,
        "version": settings.APP_VERSION,
        "database": db_status,
        "websocket_connections": manager.get_connection_count(),
        "blacklist_size": blacklist.size(),
        "uptime_seconds": uptime
    }


# ============================================================================
# AUTHENTICATION ENDPOINTS
# ============================================================================

@app.get('/login/{provider}', tags=["Authentication"])
@limiter.limit("5/minute")
async def login(request: Request, provider: str):
    """
    Initiate OAuth login flow.
    
    Supported providers: google, github
    """
    if provider not in ['google', 'github']:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    
    redirect_uri = request.url_for('auth_callback', provider=provider)
    
    if provider == 'google':
        return await oauth.google.authorize_redirect(request, redirect_uri)
    elif provider == 'github':
        return await oauth.github.authorize_redirect(request, redirect_uri)


@app.get('/auth/{provider}/callback', tags=["Authentication"])
async def auth_callback(request: Request, provider: str, db: AsyncSession = Depends(get_db)):
    """
    OAuth callback endpoint.
    
    Handles the callback from OAuth provider, creates/updates user,
    and redirects back to local client with JWT token.
    """
    try:
        # Get token from provider
        if provider == 'google':
            token = await oauth.google.authorize_access_token(request)
            user_info = token.get('userinfo')
        elif provider == 'github':
            token = await oauth.github.authorize_access_token(request)
            # GitHub requires separate API call for email
            user_info = token.get('userinfo')
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
            
    except Exception as e:
        logger.error(f"OAuth token exchange failed: {e}")
        raise HTTPException(
            status_code=400, 
            detail=f"Could not authorize access token: {str(e)}"
        )

    # Validate user info
    if not user_info or not user_info.get('email'):
        raise HTTPException(
            status_code=400, 
            detail="Could not retrieve user info from provider"
        )

    email = user_info['email']
    
    # Get or create user
    db_user = await crud.get_user_by_email(db, email=email)
    
    if not db_user:
        db_user = await crud.create_user(db, email=email, provider=provider)
        logger.info(f"Created new user: {email}")
    else:
        logger.info(f"Existing user logged in: {email}")

    # Create JWT token
    access_token = auth.create_access_token(data={"sub": db_user.email})
    
    # Redirect to local client with token
    local_redirect_uri = f"http://127.0.0.1:8080/callback?access_token={access_token}"
    return RedirectResponse(url=local_redirect_uri)


@app.get("/users/me", response_model=schemas.User, tags=["Authentication"])
async def read_users_me(current_user: models.User = Depends(auth.get_current_user)):
    """
    Get current authenticated user's information.
    
    Requires Bearer token in Authorization header.
    """
    return current_user


@app.post("/logout", response_model=schemas.LogoutResponse, tags=["Authentication"])
@limiter.limit("5/minute")
async def logout(
    request: Request,
    token: auth.HTTPAuthorizationCredentials = Depends(auth.oauth2_scheme),
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Logout by blacklisting the current JWT token.
    
    The token will be invalid until it expires naturally.
    """
    auth.blacklist_token(token.credentials)
    logger.info(f"User logged out: {current_user.email}")
    
    return {
        "message": "Successfully logged out",
        "node_id": current_user.node_id
    }


# ============================================================================
# NODE REGISTRATION ENDPOINTS
# ============================================================================

@app.post("/register-node-id", response_model=schemas.NodeRegistrationResponse, tags=["Node Identity"])
@limiter.limit("10/minute")
async def register_node_id(
    request: Request,
    registration: schemas.NodeRegistration,
    current_user: models.User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Register the user's cryptographic node identity.
    
    This should be called immediately after OAuth authentication.
    The client provides their node_id (derived from public key),
    their public key, and their self-signed certificate.
    
    The Hub validates:
    1. The certificate is valid
    2. The public key matches the certificate
    3. The node_id is correctly derived from the public key
    4. The certificate's CN matches the node_id
    
    If all checks pass, the Hub stores the cryptographic identity
    and marks the node_id as verified.
    """
    try:
        updated_user = await crud.register_node_identity(
            db=db,
            user=current_user,
            node_id=registration.node_id,
            public_key=registration.public_key,
            certificate=registration.certificate
        )
        
        logger.info(f"Node identity registered: {updated_user.node_id} for {updated_user.email}")
        
        return {
            "node_id": updated_user.node_id,
            "verified": updated_user.node_id_verified,
            "message": "Node identity successfully registered and verified"
        }
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except CryptoValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation failed: {str(e)}")


# ============================================================================
# PROFILE ENDPOINTS
# ============================================================================

@app.get("/profile", response_model=schemas.PublicProfile, tags=["Profiles"])
async def get_own_profile(
    current_user: models.User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get the authenticated user's own profile.
    """
    if not current_user.profile:
        raise HTTPException(
            status_code=404,
            detail="Profile not found. Please create a profile first using PUT /profile"
        )
    
    response_data = {
        **current_user.profile.profile_data,
        "user_id": current_user.profile.user_id,
        "updated_at": current_user.profile.updated_at 
    }
    return response_data


@app.put("/profile", response_model=schemas.PublicProfile, tags=["Profiles"])
@limiter.limit("20/minute")
async def update_profile(
    request: Request,
    profile_in: schemas.PublicProfileCreate,
    current_user: models.User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Create or update the user's public profile.
    
    This profile is visible to other users via discovery.
    """
    profile = await crud.upsert_profile(db=db, user=current_user, profile_in=profile_in)
    
    response_data = {
        **profile.profile_data,
        "user_id": profile.user_id,
        "updated_at": profile.updated_at
    }
    return response_data


@app.get("/profile/{node_id}", response_model=schemas.PublicProfile, tags=["Profiles"])
async def read_profile(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user)
):
    """
    Get a user's public profile by their node_id.
    
    Requires authentication to view profiles.
    """
    profile = await crud.get_profile_by_node_id(db, node_id=node_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
        
    response_data = {
        **profile.profile_data,
        "user_id": profile.user_id,
        "updated_at": profile.updated_at
    }
    return response_data


@app.delete("/profile", status_code=204, tags=["Profiles"])
@limiter.limit("5/minute")
async def delete_account(
    request: Request,
    current_user: models.User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete the user's account and all associated data.
    
    This action cannot be undone. All data including profile,
    expertise, and authentication will be permanently deleted.
    """
    logger.info(f"Deleting account: {current_user.email}")
    await crud.delete_user(db, current_user)
    return None


# ============================================================================
# DISCOVERY ENDPOINTS
# ============================================================================

@app.get("/discovery/search", response_model=schemas.SearchResponse, tags=["Discovery"])
@limiter.limit("30/minute")
async def search_profiles(
    request: Request,
    q: str,
    min_level: int = 1,
    db: AsyncSession = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user)
):
    """
    Search for users based on their expertise.
    
    Query parameters:
    - q: Topic or skill to search for (required)
    - min_level: Minimum proficiency level 1-5 (optional, default: 1)
    
    Returns users who have the specified skill at or above the minimum level.
    """
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty")

    try:
        profiles = await crud.search_profiles_by_expertise(
            db, 
            topic=q.lower(), 
            min_level=min_level
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Format results
    results = [
        schemas.SearchResult(
            node_id=profile.user.node_id,
            name=profile.profile_data.get('name'),
            description=profile.profile_data.get('description'),
            expertise=profile.profile_data.get('expertise')
        )
        for profile in profiles
    ]
    
    return {
        "results": results,
        "total": len(results)
    }


# ============================================================================
# WEBSOCKET SIGNALING ENDPOINT
# ============================================================================

@app.websocket("/ws/signal")
async def websocket_endpoint(
    websocket: WebSocket,
    user: models.User = Depends(auth.get_current_user_ws)
):
    """
    WebRTC signaling WebSocket endpoint.
    
    Authentication is handled by the get_current_user_ws dependency.
    Client connects with token in query parameter: /ws/signal?token=<JWT>
    
    Message types:
    - signal: WebRTC signaling messages (offer, answer, ice-candidate)
    - error: Error messages from server
    - auth_ok: Authentication confirmation
    
    The server acts as a relay, forwarding messages between peers
    without inspecting the payload.
    """
    if user is None:
        # Connection already closed by dependency
        return

    node_id = user.node_id
    
    try:
        # Connect and register WebSocket
        await manager.connect(websocket, node_id)
        
        # Send authentication confirmation
        await manager.send_personal_message(
            json.dumps({
                "type": "auth_ok",
                "message": f"Successfully connected as {node_id}",
                "node_id": node_id
            }),
            node_id
        )
        
        logger.info(f"WebSocket connected: {node_id}")
        
        # Main message loop
        while True:
            try:
                # Receive message from client
                data_str = await websocket.receive_text()
                data = json.loads(data_str)
                
                # Handle different message types
                message_type = data.get("type")
                
                if message_type == "signal":
                    # WebRTC signaling message
                    target_node_id = data.get("target_node_id")
                    
                    if not target_node_id:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "target_node_id is required for signal messages",
                            "code": "missing_target"
                        }))
                        continue
                    
                    # Check if target is connected
                    if not manager.is_connected(target_node_id):
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": f"Target node {target_node_id} is not connected",
                            "code": "target_offline"
                        }))
                        continue
                    
                    # Add sender info and relay
                    data["sender_node_id"] = node_id
                    
                    success = await manager.send_personal_message(
                        json.dumps(data), 
                        target_node_id
                    )
                    
                    if success:
                        logger.info(f"Relayed signal: {node_id} → {target_node_id}")
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Failed to relay signal to target",
                            "code": "relay_failed"
                        }))
                
                elif message_type == "ping":
                    # Keepalive ping
                    await websocket.send_text(json.dumps({
                        "type": "pong",
                        "timestamp": data.get("timestamp")
                    }))
                
                else:
                    # Unknown message type
                    logger.warning(f"Unknown message type from {node_id}: {message_type}")
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Unknown message type: {message_type}",
                        "code": "unknown_message_type"
                    }))
                    
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from {node_id}: {e}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON format",
                    "code": "invalid_json"
                }))
            
            except Exception as e:
                logger.error(f"Error processing message from {node_id}: {e}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Failed to process message",
                    "code": "processing_error"
                }))
                
    except WebSocketDisconnect:
        manager.disconnect(node_id)
        logger.info(f"WebSocket disconnected: {node_id}")
        
    except Exception as e:
        logger.error(f"WebSocket error for {node_id}: {e}")
        logger.error(traceback.format_exc())
        manager.disconnect(node_id)


# ============================================================================
# ADMIN/DEBUG ENDPOINTS (Development Only)
# ============================================================================

if settings.DEBUG:
    @app.get("/debug/connections", tags=["Debug"])
    async def debug_connections(
        _current_user: models.User = Depends(auth.get_current_user)
    ):
        """Get list of active WebSocket connections (debug only)"""
        return {
            "count": manager.get_connection_count(),
            "nodes": manager.get_connected_nodes()
        }
    
    @app.get("/debug/blacklist", tags=["Debug"])
    async def debug_blacklist(
        _current_user: models.User = Depends(auth.get_current_user)
    ):
        """Get blacklist size (debug only)"""
        blacklist = get_blacklist()
        return {
            "size": blacklist.size()
        }
    
    @app.post("/debug/blacklist/clear", tags=["Debug"])
    async def debug_clear_blacklist(
        _current_user: models.User = Depends(auth.get_current_user)
    ):
        """Clear token blacklist (debug only)"""
        blacklist = get_blacklist()
        blacklist.clear()
        return {"message": "Blacklist cleared"}


# ============================================================================
# STARTUP MESSAGE
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Log startup message"""
    logger.info("=" * 60)
    logger.info(f"  {settings.APP_NAME}")
    logger.info(f"  Version: {settings.APP_VERSION}")
    logger.info("=" * 60)
    logger.info(f"  Docs: http://localhost:8000/docs")
    logger.info(f"  Health: http://localhost:8000/health")
    logger.info("=" * 60)


# Export app for uvicorn
__all__ = ['app']