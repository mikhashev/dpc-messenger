"""
Database models for DPC Hub.

SQLAlchemy models for users and profiles with cryptographic identity support.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB

from .database import Base


class User(Base):
    """
    User model with cryptographic identity.
    
    The user's identity is tied to their cryptographic node_id,
    which is derived from their RSA public key.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    
    # Cryptographic identity
    node_id = Column(String, unique=True, index=True, nullable=False)
    public_key = Column(Text, nullable=True)  # PEM-formatted RSA public key
    certificate = Column(Text, nullable=True, index=True)  # PEM-formatted X.509 certificate
    node_id_verified = Column(Boolean, default=False, nullable=False)  # True after crypto validation
    
    # OAuth provider info
    provider = Column(String, nullable=False)  # 'google', 'github', etc.
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    profile = relationship(
        "PublicProfile", 
        back_populates="user", 
        uselist=False, 
        cascade="all, delete-orphan"
    )
    
    def __repr__(self):
        return f"<User(id={self.id}, email='{self.email}', node_id='{self.node_id}', verified={self.node_id_verified})>"


class PublicProfile(Base):
    """
    Public profile with JSONB storage for flexible schema.
    
    The profile_data column stores all profile information as JSON,
    allowing for flexible schema evolution without migrations.
    """
    __tablename__ = "public_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # JSONB for flexible profile data
    # Contains: name, description, expertise, compute, p2p_uri_hint, etc.
    profile_data = Column(JSONB, nullable=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="profile")
    
    def __repr__(self):
        name = self.profile_data.get('name', 'Unknown')
        return f"<PublicProfile(id={self.id}, user_id={self.user_id}, name='{name}')>"


# Optional: Add session tracking for security
class UserSession(Base):
    """
    Track active user sessions for security monitoring.
    Optional - for future enhancement.
    """
    __tablename__ = "user_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    session_token = Column(String, unique=True, index=True, nullable=False)
    ip_address = Column(String)
    user_agent = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_active = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    def __repr__(self):
        return f"<UserSession(id={self.id}, user_id={self.user_id})>"