"""Create initial tables for users and profiles with all columns

Revision ID: 3b5fdfb64f0b
Revises: 
Create Date: 2025-11-04 00:15:05.603485

CORRECTED VERSION - Includes updated_at, public_key, certificate, node_id_verified
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3b5fdfb64f0b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema with all required columns."""
    # Create users table with ALL columns
    op.create_table('users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('node_id', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        
        # NEW: Cryptographic identity columns
        sa.Column('public_key', sa.Text(), nullable=True),
        sa.Column('certificate', sa.Text(), nullable=True),
        sa.Column('node_id_verified', sa.Boolean(), server_default='false', nullable=False),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes on users table
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)
    op.create_index(op.f('ix_users_node_id'), 'users', ['node_id'], unique=True)
    op.create_index('ix_users_certificate', 'users', ['certificate'], unique=False)
    
    # Create public_profiles table with ALL columns
    op.create_table('public_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('profile_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    op.create_index(op.f('ix_public_profiles_id'), 'public_profiles', ['id'], unique=False)
    
    # Add GIN indexes for JSONB search performance
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_profile_expertise 
        ON public_profiles USING gin (profile_data jsonb_path_ops)
    """)
    
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_profile_expertise_keys 
        ON public_profiles USING gin ((profile_data->'expertise'))
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_profile_expertise_keys")
    op.execute("DROP INDEX IF EXISTS idx_profile_expertise")
    
    op.drop_index(op.f('ix_public_profiles_id'), table_name='public_profiles')
    op.drop_table('public_profiles')
    
    op.drop_index('ix_users_certificate', table_name='users')
    op.drop_index(op.f('ix_users_node_id'), table_name='users')
    op.drop_index(op.f('ix_users_id'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')