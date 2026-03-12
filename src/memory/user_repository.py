"""
Repository for User account management
"""

import bcrypt
from datetime import datetime
import secrets
from typing import List, Optional, Dict, Any
from uuid import UUID
from sqlalchemy import select, delete, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User


class UserRepository:
    """Repository for managing user accounts"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt
        
        Args:
            password: Plain text password
            
        Returns:
            str: Hashed password
        """
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify a password against its hash
        
        Args:
            password: Plain text password
            password_hash: Stored password hash
            
        Returns:
            bool: True if password matches
        """
        try:
            return bcrypt.checkpw(
                password.encode('utf-8'),
                password_hash.encode('utf-8')
            )
        except Exception:
            return False
    
    @staticmethod
    async def create_user(
        session: AsyncSession,
        username: str,
        password: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        role: str = 'user',
        is_password_reset_required: bool = True
    ) -> User:
        """Create a new user
        
        Args:
            session: Database session
            username: Unique username
            password: Plain text password (will be hashed)
            email: Optional email address
            display_name: Optional display name
            role: User role ('admin' or 'user')
            is_password_reset_required: Force password change on first login
            
        Returns:
            User: Created user
            
        Raises:
            ValueError: If username already exists
        """
        # Check if username already exists
        existing = await UserRepository.get_by_username(session, username)
        if existing:
            raise ValueError(f"Username '{username}' already exists")
        
        # Check if email already exists
        if email:
            existing_email = await UserRepository.get_by_email(session, email)
            if existing_email:
                raise ValueError(f"Email '{email}' already exists")
        
        user = User(
            username=username,
            password_hash=UserRepository.hash_password(password),
            email=email,
            display_name=display_name or username,
            role=role,
            is_password_reset_required=is_password_reset_required
        )
        
        session.add(user)
        await session.commit()
        await session.refresh(user)
        
        # Initialize git repository for user's workspace directory
        try:
            from ..services.git_service import ensure_user_git_repository
            ensure_user_git_repository(str(user.id))
        except Exception as e:
            # Log but don't fail user creation
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to initialize git repository for user {user.id}: {e}"
            )
        
        return user
    
    @staticmethod
    async def get_by_id(session: AsyncSession, user_id: UUID) -> Optional[User]:
        """Get user by ID
        
        Args:
            session: Database session
            user_id: User UUID
            
        Returns:
            User or None
        """
        query = select(User).where(User.id == user_id)
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_username(session: AsyncSession, username: str) -> Optional[User]:
        """Get user by username
        
        Args:
            session: Database session
            username: Username to search
            
        Returns:
            User or None
        """
        query = select(User).where(User.username == username)
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_email(session: AsyncSession, email: str) -> Optional[User]:
        """Get user by email
        
        Args:
            session: Database session
            email: Email to search
            
        Returns:
            User or None
        """
        query = select(User).where(User.email == email)
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def authenticate(
        session: AsyncSession,
        username: str,
        password: str
    ) -> Optional[User]:
        """Authenticate user with username and password
        
        Args:
            session: Database session
            username: Username
            password: Plain text password
            
        Returns:
            User if authentication successful, None otherwise
        """
        user = await UserRepository.get_by_username(session, username)
        
        if not user:
            return None
        
        if not user.is_active:
            return None
        
        if not UserRepository.verify_password(password, user.password_hash):
            return None
        
        # Update last login
        user.last_login = datetime.utcnow()
        await session.commit()
        
        return user
    
    @staticmethod
    async def update_password(
        session: AsyncSession,
        user_id: UUID,
        new_password: str,
        clear_reset_flag: bool = True
    ) -> bool:
        """Update user password
        
        Args:
            session: Database session
            user_id: User UUID
            new_password: New plain text password
            clear_reset_flag: Clear is_password_reset_required flag
            
        Returns:
            bool: True if successful
        """
        user = await UserRepository.get_by_id(session, user_id)
        if not user:
            return False
        
        user.password_hash = UserRepository.hash_password(new_password)
        if clear_reset_flag:
            user.is_password_reset_required = False
        user.updated_at = datetime.utcnow()
        
        await session.commit()
        return True
    
    @staticmethod
    async def update_user(
        session: AsyncSession,
        user_id: UUID,
        **kwargs
    ) -> Optional[User]:
        """Update user fields
        
        Args:
            session: Database session
            user_id: User UUID
            **kwargs: Fields to update (email, display_name, role, is_active, 
                      preferred_character, user_settings)
            
        Returns:
            Updated User or None
        """
        user = await UserRepository.get_by_id(session, user_id)
        if not user:
            return None
        
        allowed_fields = {
            'email', 'display_name', 'role', 'is_active',
            'preferred_character', 'user_settings', 'is_password_reset_required'
        }
        
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(user, key, value)
        
        user.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(user)
        
        return user
    
    @staticmethod
    async def delete_user(session: AsyncSession, user_id: UUID) -> bool:
        """Delete a user
        
        Args:
            session: Database session
            user_id: User UUID
            
        Returns:
            bool: True if deleted
        """
        user = await UserRepository.get_by_id(session, user_id)
        if not user:
            return False
        
        await session.delete(user)
        await session.commit()
        return True
    
    @staticmethod
    async def list_users(
        session: AsyncSession,
        limit: int = 100,
        offset: int = 0,
        include_inactive: bool = False,
        role: Optional[str] = None
    ) -> tuple[List[User], int]:
        """List users with pagination
        
        Args:
            session: Database session
            limit: Maximum users to return
            offset: Number of users to skip
            include_inactive: Include inactive users
            role: Filter by role
            
        Returns:
            tuple: (list of users, total count)
        """
        conditions = []
        
        if not include_inactive:
            conditions.append(User.is_active == True)
        
        if role:
            conditions.append(User.role == role)
        
        # Get total count
        count_query = select(User)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count_result = await session.execute(count_query)
        total_count = len(count_result.scalars().all())
        
        # Get paginated results
        query = select(User)
        if conditions:
            query = query.where(and_(*conditions))
        query = query.order_by(User.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await session.execute(query)
        users = result.scalars().all()
        
        return users, total_count
    
    @staticmethod
    async def count_admins(session: AsyncSession) -> int:
        """Count active admin users
        
        Args:
            session: Database session
            
        Returns:
            int: Number of active admins
        """
        query = select(User).where(
            and_(User.role == 'admin', User.is_active == True)
        )
        result = await session.execute(query)
        return len(result.scalars().all())
    
    @staticmethod
    async def ensure_admin_exists(
        session: AsyncSession,
        default_username: str = 'admin',
        default_password: Optional[str] = None
    ) -> str | None:
        """Ensure at least one admin user exists
        
        Creates a default admin if no admin exists.
        
        Args:
            session: Database session
            default_username: Default admin username
            default_password: Default admin password. If omitted, a random
                password is generated.
            
        Returns:
            Created password if admin was created, None if admin already existed
        """
        admin_count = await UserRepository.count_admins(session)
        
        if admin_count > 0:
            # Admin already exists
            return None

        password = default_password or secrets.token_urlsafe(16)
        
        # Create default admin
        await UserRepository.create_user(
            session=session,
            username=default_username,
            password=password,
            role='admin',
            display_name='Administrator',
            is_password_reset_required=True  # Force password change
        )
        return password
