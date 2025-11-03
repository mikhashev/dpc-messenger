# dpc-hub/dpc_hub/schemas.py
from pydantic import BaseModel

class Token(BaseModel):
    access_token: str
    token_type: str

class User(BaseModel):
    email: str
    node_id: str

    class Config:
        from_attributes = True # orm_mode = True for Pydantic v1