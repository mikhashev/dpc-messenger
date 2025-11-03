# dpc-hub/dpc_hub/models.py
from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB

from .database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    node_id = Column(String, unique=True, index=True, nullable=False)
    provider = Column(String, nullable=False) # e.g., 'google', 'github'
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    profile = relationship("PublicProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")

class PublicProfile(Base):
    __tablename__ = "public_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    profile_data = Column(JSONB, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="profile")