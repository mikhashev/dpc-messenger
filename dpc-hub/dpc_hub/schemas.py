"""
Pydantic schemas for request/response validation.

Includes schemas for authentication, profiles, node registration, and discovery.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Any, Optional
from datetime import datetime


# Authentication Schemas

class Token(BaseModel):
    """JWT token response"""
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    """Decoded JWT token data"""
    email: Optional[str] = None


# User Schemas

class UserBase(BaseModel):
    """Base user schema"""
    email: str
    node_id: str


class UserCreate(UserBase):
    """Schema for creating a user"""
    provider: str


class User(UserBase):
    """Full user schema"""
    id: int
    node_id_verified: bool
    provider: str
    created_at: datetime
    profile: Optional['PublicProfile'] = None

    class Config:
        from_attributes = True


class UserWithIdentity(User):
    """User schema including cryptographic identity (admin only)"""
    public_key: Optional[str] = None
    certificate: Optional[str] = None


# Node Registration Schemas

class NodeRegistration(BaseModel):
    """
    Schema for registering a node's cryptographic identity.
    
    The client provides their node_id (derived from public key),
    public key, and self-signed certificate.
    """
    node_id: str = Field(
        ..., 
        pattern=r'^dpc-node-[a-f0-9]{16,}$',
        description="Cryptographic node ID (format: dpc-node-{hex})"
    )
    public_key: str = Field(
        ..., 
        description="PEM-formatted RSA public key",
        min_length=100
    )
    certificate: str = Field(
        ..., 
        description="PEM-formatted X.509 certificate",
        min_length=100
    )
    
    @field_validator('public_key')
    def validate_public_key(cls, v):
        if not v.startswith('-----BEGIN PUBLIC KEY-----'):
            raise ValueError('Invalid public key format (must be PEM)')
        if not v.strip().endswith('-----END PUBLIC KEY-----'):
            raise ValueError('Invalid public key format (must be PEM)')
        return v
    
    @field_validator('certificate')
    def validate_certificate(cls, v):
        if not v.startswith('-----BEGIN CERTIFICATE-----'):
            raise ValueError('Invalid certificate format (must be PEM)')
        if not v.strip().endswith('-----END CERTIFICATE-----'):
            raise ValueError('Invalid certificate format (must be PEM)')
        return v


class NodeRegistrationResponse(BaseModel):
    """Response after successful node registration"""
    node_id: str
    verified: bool
    message: str


# Profile Schemas

class PublicProfileBase(BaseModel):
    """Base profile schema"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    expertise: Dict[str, int] = Field(default_factory=dict)
    compute: Dict[str, Any] = Field(default_factory=dict)
    p2p_uri_hint: Optional[str] = None
    
    @field_validator('expertise')
    def validate_expertise(cls, v):
        for skill, level in v.items():
            if not isinstance(level, int) or not 1 <= level <= 5:
                raise ValueError(f'Expertise level must be 1-5, got {level} for {skill}')
            if len(skill) > 50:
                raise ValueError(f'Skill name too long: {skill}')
        return v


class PublicProfileCreate(PublicProfileBase):
    """Schema for creating/updating a profile"""
    pass


class PublicProfile(PublicProfileBase):
    """Full profile schema with metadata"""
    user_id: int
    updated_at: datetime

    class Config:
        from_attributes = True


# Discovery Schemas

class SearchResult(BaseModel):
    """Single search result"""
    node_id: str
    name: str
    description: Optional[str] = None
    expertise: Optional[Dict[str, int]] = None


class SearchResponse(BaseModel):
    """Search results response"""
    results: List[SearchResult]
    total: int = Field(default=0, description="Total number of results")
    
    class Config:
        json_schema_extra = {
            "example": {
                "results": [
                    {
                        "node_id": "dpc-node-8b066c7f3d7e",
                        "name": "Alice",
                        "description": "AI Researcher",
                        "expertise": {"python": 5, "machine_learning": 4}
                    }
                ],
                "total": 1
            }
        }


# Health Check Schema

class HealthCheckResponse(BaseModel):
    """Health check response with system status"""
    status: str = Field(..., description="Overall status: healthy, degraded, or unhealthy")
    version: str
    database: str = Field(..., description="Database connection status")
    websocket_connections: int = Field(default=0, description="Number of active WebSocket connections")
    blacklist_size: int = Field(default=0, description="Number of blacklisted tokens")
    uptime_seconds: Optional[float] = None


# WebSocket Schemas

class WebSocketMessage(BaseModel):
    """Base WebSocket message schema"""
    type: str
    

class SignalMessage(WebSocketMessage):
    """WebRTC signaling message"""
    type: str = "signal"
    target_node_id: str
    payload: Dict[str, Any]
    sender_node_id: Optional[str] = None  # Added by server


class AuthMessage(WebSocketMessage):
    """Authentication message for WebSocket"""
    type: str = "auth"
    token: str


class ErrorMessage(WebSocketMessage):
    """Error message"""
    type: str = "error"
    message: str
    code: Optional[str] = None


class AuthOkMessage(WebSocketMessage):
    """Authentication success message"""
    type: str = "auth_ok"
    message: str
    node_id: str


# Logout Schema

class LogoutResponse(BaseModel):
    """Logout response"""
    message: str
    node_id: Optional[str] = None


# Error Schema

class ErrorDetail(BaseModel):
    """Detailed error response"""
    detail: str
    error_code: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)