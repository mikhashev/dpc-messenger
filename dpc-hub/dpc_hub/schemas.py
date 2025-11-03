# dpc-hub/dpc_hub/schemas.py
from pydantic import BaseModel, Field
from typing import Dict, List, Any, Optional
from datetime import datetime

class Token(BaseModel):
    access_token: str
    token_type: str

class User(BaseModel):
    email: str
    node_id: str

    class Config:
        from_attributes = True # orm_mode = True for Pydantic v1

class PublicProfileBase(BaseModel):
    name: str
    description: Optional[str] = None
    expertise: Dict[str, int] = Field(default_factory=dict)
    compute: Dict[str, Any] = Field(default_factory=dict)
    p2p_uri_hint: Optional[str] = None

class PublicProfileCreate(PublicProfileBase):
    pass

class PublicProfile(PublicProfileBase):
    user_id: int
    updated_at: datetime

    class Config:
        from_attributes = True

class User(BaseModel):
    email: str
    node_id: str
    profile: Optional[PublicProfile] = None

    class Config:
        from_attributes = True