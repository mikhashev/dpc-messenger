# dpc-hub/dpc_hub/main.py
from fastapi import FastAPI, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from . import crud, models, schemas, auth
from .database import get_db
from .settings import settings

app = FastAPI(
    title="D-PC Federation Hub",
    version="1.0.0",
    description="The central discovery and signaling server for the D-PC network."
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

@app.get('/auth/google', response_model=schemas.Token)
async def auth_google(request: Request, db: AsyncSession = Depends(get_db)):
    """Handles the callback from Google and returns a JWT."""
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get('userinfo')
    
    if not user_info or not user_info.get('email'):
        return {"error": "Could not retrieve user info from Google"}

    email = user_info['email']
    db_user = await crud.get_user_by_email(db, email=email)
    
    if not db_user:
        db_user = await crud.create_user(db, email=email, provider='google')

    access_token = auth.create_access_token(data={"sub": db_user.email})
    
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me/", response_model=schemas.User)
async def read_users_me(current_user: models.User = Depends(auth.get_current_user)):
    """A protected endpoint to test authentication."""
    return current_user