# dpc-hub/dpc_hub/main.py
import asyncio
import traceback
from typing import List, Optional
from fastapi import FastAPI, Depends, Request, HTTPException, websockets
from pydantic import BaseModel
from starlette.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from fastapi import WebSocket, WebSocketDisconnect, Query
from typing import Annotated
import json
from .connection_manager import manager

from . import crud, models, schemas, auth
from .database import AsyncSessionLocal, get_db
from .settings import settings
from . import auth

app = FastAPI(
    title="D-PC Federation Hub",
    version="1.0.0",
    description="The central discovery and signaling server for the D-PC network.",
    debug=False
)

# This is the schema for the search result item
class SearchResult(BaseModel):
    node_id: str
    name: str
    description: Optional[str] = None

# This is the schema for the full search response
class SearchResponse(BaseModel):
    results: List[SearchResult]

@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    print("--- Unhandled Exception ---")
    traceback.print_exc()
    print("-------------------------")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

oauth = OAuth()
oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    client_kwargs={
        'scope': 'openid email profile'
    }
)

@app.get("/")
async def root():
    """A simple health check endpoint for the server."""
    return {"status": "ok", "project": "D-PC Hub", "version": app.version}

@app.get('/login/google')
async def login_google(request: Request):
    """Redirects user to Google for authentication."""
    redirect_uri = request.url_for('auth_google')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get('/auth/google') # <-- 2. Remove the response_model for this endpoint
async def auth_google(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handles the callback from Google, creates a user and JWT,
    and redirects back to the local client.
    """
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not authorize access token: {e}")

    user_info = token.get('userinfo')
    
    if not user_info or not user_info.get('email'):
        raise HTTPException(status_code=400, detail="Could not retrieve user info from Google")

    email = user_info['email']
    db_user = await crud.get_user_by_email(db, email=email)
    
    if not db_user:
        db_user = await crud.create_user(db, email=email, provider='google')

    access_token = auth.create_access_token(data={"sub": db_user.email})
    
    # --- 3. THE CORE FIX ---
    # Instead of returning JSON, we redirect back to the local server
    # that our HubClient is running.
    local_redirect_uri = f"http://127.0.0.1:8080/callback?access_token={access_token}"
    return RedirectResponse(url=local_redirect_uri)

@app.get("/users/me/", response_model=schemas.User)
async def read_users_me(current_user: models.User = Depends(auth.get_current_user)):
    """A protected endpoint to test authentication."""
    return current_user

@app.put("/profile", response_model=schemas.PublicProfile)
async def update_profile(
    profile_in: schemas.PublicProfileCreate,
    current_user: models.User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Creates or updates the public profile for the currently authenticated user.
    """
    profile = await crud.upsert_profile(db=db, user=current_user, profile_in=profile_in)
    
    response_data = {
        **profile.profile_data,
        "user_id": profile.user_id,
        "updated_at": profile.updated_at
    }
    return response_data

@app.get("/profile/{node_id}", response_model=schemas.PublicProfile)
async def read_profile(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user)
):
    """
    Retrieves the public profile for a given node_id.
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

@app.get("/discovery/search", response_model=SearchResponse)
async def search_profiles(
    q: str,
    min_level: int = 1,
    db: AsyncSession = Depends(get_db),
    _current_user: models.User = Depends(auth.get_current_user)
):
    """
    Searches for users based on their advertised expertise.
    """
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty.")

    profiles = await crud.search_profiles_by_expertise(db, topic=q.lower(), min_level=min_level)
    
    # Format the results to match the SearchResult schema
    results = [
        SearchResult(
            node_id=profile.user.node_id,
            name=profile.profile_data.get('name'),
            description=profile.profile_data.get('description')
        )
        for profile in profiles
    ]
    
    return {"results": results}

# --- NEW WEBSOCKET ENDPOINT ---

@app.websocket("/ws/signal")
async def websocket_endpoint(
    websocket: WebSocket,
    user: models.User = Depends(auth.get_current_user_ws)
):
    """
    Handles the signaling WebSocket connection.
    Authentication is handled by the `get_current_user_ws` dependency.
    """
    if user is None:
        # The dependency has already closed the connection if auth failed.
        # We just need to exit the function.
        return

    node_id = user.node_id
    await manager.connect(websocket, node_id)
    print(f"INFO:     WebSocket connected for node: {node_id}")
    await manager.send_personal_message(json.dumps({"type": "auth_ok", "message": f"Successfully connected as {node_id}"}), node_id)

    try:
        # Main loop to relay messages
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            
            if data.get("type") == "signal":
                target_node_id = data.get("target_node_id")
                if target_node_id:
                    data["sender_node_id"] = node_id
                    await manager.send_personal_message(json.dumps(data), target_node_id)
                    print(f"INFO:     Relayed signal from {node_id} to {target_node_id}")
    except WebSocketDisconnect:
        manager.disconnect(node_id)
        print(f"INFO:     WebSocket disconnected for node: {node_id}")
    except Exception as e:
        print(f"ERROR:    An error occurred in WebSocket for {node_id}: {e}")
        manager.disconnect(node_id)