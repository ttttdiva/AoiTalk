"""
Repository for Project management
"""

import re
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from uuid import UUID
from sqlalchemy import select, delete, update, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import Project, ProjectMember, ProjectJoinRequest, User


def generate_slug(name: str) -> str:
    """Generate URL-safe slug from project name"""
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', name.lower())
    slug = re.sub(r'[-\s]+', '-', slug).strip('-')
    return slug[:100] if slug else 'project'


class ProjectRepository:
    """Repository for managing projects and memberships"""
    
    # ─── Project CRUD ───────────────────────────────────────────────────
    
    @staticmethod
    async def create_project(
        session: AsyncSession,
        owner_id: UUID,
        name: str,
        description: Optional[str] = None,
        slug: Optional[str] = None,
        allow_join_requests: bool = True,
        storage_quota_mb: int = 1000
    ) -> Project:
        """Create a new project
        
        Args:
            session: Database session
            owner_id: UUID of the project owner
            name: Project name
            description: Optional description
            slug: Optional custom slug (auto-generated if not provided)
            allow_join_requests: Whether to accept join requests
            storage_quota_mb: Storage quota in MB
            
        Returns:
            Created Project
        """
        # Generate unique slug
        base_slug = slug or generate_slug(name)
        final_slug = base_slug
        counter = 1
        
        # Check for slug conflicts
        while True:
            existing = await session.execute(
                select(Project).where(Project.slug == final_slug)
            )
            if not existing.scalar_one_or_none():
                break
            final_slug = f"{base_slug}-{counter}"
            counter += 1
        
        project = Project(
            name=name,
            description=description,
            slug=final_slug,
            owner_id=owner_id,
            allow_join_requests=allow_join_requests,
            storage_quota_mb=storage_quota_mb
        )
        session.add(project)
        await session.flush()
        
        # Add owner as a member with owner role
        owner_member = ProjectMember(
            project_id=project.id,
            user_id=owner_id,
            role='owner',
            permissions={
                'read': True,
                'write': True,
                'delete': True,
                'manage_members': True,
                'manage_settings': True
            }
        )
        session.add(owner_member)
        await session.commit()
        await session.refresh(project)
        
        # Initialize git repository for project's workspace directory
        try:
            from ..services.git_service import ensure_project_git_repository
            ensure_project_git_repository(str(project.id))
        except Exception as e:
            # Log but don't fail project creation
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to initialize git repository for project {project.id}: {e}"
            )
        
        return project
    
    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        project_id: UUID,
        include_members: bool = False
    ) -> Optional[Project]:
        """Get project by ID
        
        Args:
            session: Database session
            project_id: Project UUID
            include_members: Whether to eagerly load members
            
        Returns:
            Project or None
        """
        query = select(Project).where(Project.id == project_id)
        if include_members:
            query = query.options(selectinload(Project.members))
        
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_slug(
        session: AsyncSession,
        slug: str,
        include_members: bool = False
    ) -> Optional[Project]:
        """Get project by slug
        
        Args:
            session: Database session
            slug: Project slug
            include_members: Whether to eagerly load members
            
        Returns:
            Project or None
        """
        query = select(Project).where(Project.slug == slug)
        if include_members:
            query = query.options(selectinload(Project.members))
        
        result = await session.execute(query)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_user_projects(
        session: AsyncSession,
        user_id: UUID,
        include_public: bool = False
    ) -> List[Dict[str, Any]]:
        """Get all projects a user has access to
        
        Args:
            session: Database session
            user_id: User UUID
            include_public: Include public projects user is not a member of
            
        Returns:
            List of project dicts with membership info
        """
        # Get projects where user is a member
        query = (
            select(Project, ProjectMember)
            .join(ProjectMember, Project.id == ProjectMember.project_id)
            .where(ProjectMember.user_id == user_id)
            .order_by(Project.updated_at.desc())
        )
        
        result = await session.execute(query)
        projects = []
        
        for project, member in result.fetchall():
            proj_dict = project.to_dict()
            proj_dict['membership'] = member.to_dict()
            projects.append(proj_dict)
        
        return projects
    
    @staticmethod
    async def update_project(
        session: AsyncSession,
        project_id: UUID,
        **kwargs
    ) -> Optional[Project]:
        """Update project fields
        
        Args:
            session: Database session
            project_id: Project UUID
            **kwargs: Fields to update (name, description,
                      allow_join_requests, storage_quota_mb, project_metadata)
            
        Returns:
            Updated Project or None
        """
        allowed_fields = {
            'name', 'description', 'allow_join_requests',
            'storage_quota_mb', 'project_metadata'
        }
        
        update_data = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not update_data:
            return await ProjectRepository.get_by_id(session, project_id)
        
        update_data['updated_at'] = datetime.utcnow()
        
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(**update_data)
        )
        await session.commit()
        
        return await ProjectRepository.get_by_id(session, project_id)
    
    @staticmethod
    async def delete_project(
        session: AsyncSession,
        project_id: UUID
    ) -> bool:
        """Delete a project and all associated data
        
        Args:
            session: Database session
            project_id: Project UUID
            
        Returns:
            bool: True if deleted
        """
        result = await session.execute(
            delete(Project).where(Project.id == project_id)
        )
        await session.commit()
        return result.rowcount > 0
    
    # ─── Member Management ──────────────────────────────────────────────
    
    @staticmethod
    async def add_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        role: str = 'member',
        invited_by: Optional[UUID] = None,
        permissions: Optional[Dict[str, bool]] = None
    ) -> Optional[ProjectMember]:
        """Add a member to project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID to add
            role: Member role ('admin', 'member', 'viewer')
            invited_by: UUID of user who invited
            permissions: Custom permissions override
            
        Returns:
            ProjectMember or None if already exists
        """
        # Check if already a member
        existing = await session.execute(
            select(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        if existing.scalar_one_or_none():
            return None
        
        # Default permissions by role
        default_permissions = {
            'owner': {'read': True, 'write': True, 'delete': True, 'manage_members': True, 'manage_settings': True},
            'admin': {'read': True, 'write': True, 'delete': True, 'manage_members': True, 'manage_settings': False},
            'member': {'read': True, 'write': True, 'delete': False, 'manage_members': False, 'manage_settings': False},
            'viewer': {'read': True, 'write': False, 'delete': False, 'manage_members': False, 'manage_settings': False}
        }
        
        member = ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            permissions=permissions or default_permissions.get(role, default_permissions['member'])
        )
        session.add(member)
        await session.commit()
        await session.refresh(member)
        
        return member
    
    @staticmethod
    async def get_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID
    ) -> Optional[ProjectMember]:
        """Get membership info for a user in a project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            
        Returns:
            ProjectMember or None
        """
        result = await session.execute(
            select(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_project_members(
        session: AsyncSession,
        project_id: UUID
    ) -> List[Dict[str, Any]]:
        """Get all members of a project with user info
        
        Args:
            session: Database session
            project_id: Project UUID
            
        Returns:
            List of member dicts with user info
        """
        result = await session.execute(
            select(ProjectMember, User)
            .join(User, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.joined_at)
        )
        
        members = []
        for member, user in result.fetchall():
            member_dict = member.to_dict()
            member_dict['user'] = {
                'id': str(user.id),
                'username': user.username,
                'display_name': user.display_name,
                'email': user.email
            }
            members.append(member_dict)
        
        return members
    
    @staticmethod
    async def update_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        role: Optional[str] = None,
        permissions: Optional[Dict[str, bool]] = None
    ) -> Optional[ProjectMember]:
        """Update member role or permissions
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            role: New role
            permissions: New permissions
            
        Returns:
            Updated ProjectMember or None
        """
        update_data = {}
        if role is not None:
            update_data['role'] = role
        if permissions is not None:
            update_data['permissions'] = permissions
        
        if not update_data:
            return await ProjectRepository.get_member(session, project_id, user_id)
        
        await session.execute(
            update(ProjectMember)
            .where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
            .values(**update_data)
        )
        await session.commit()
        
        return await ProjectRepository.get_member(session, project_id, user_id)
    
    @staticmethod
    async def remove_member(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID
    ) -> bool:
        """Remove a member from project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID to remove
            
        Returns:
            bool: True if removed
        """
        # Cannot remove owner
        member = await ProjectRepository.get_member(session, project_id, user_id)
        if member and member.role == 'owner':
            return False
        
        result = await session.execute(
            delete(ProjectMember).where(
                and_(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id
                )
            )
        )
        await session.commit()
        return result.rowcount > 0
    
    # ─── Join Request Management ────────────────────────────────────────
    
    @staticmethod
    async def create_join_request(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        message: Optional[str] = None
    ) -> Optional[ProjectJoinRequest]:
        """Create a join request
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID requesting to join
            message: Optional request message
            
        Returns:
            ProjectJoinRequest or None if already member/pending
        """
        # Check if already a member
        existing_member = await ProjectRepository.get_member(session, project_id, user_id)
        if existing_member:
            return None
        
        # Check for existing pending request
        existing_request = await session.execute(
            select(ProjectJoinRequest).where(
                and_(
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.user_id == user_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
        )
        if existing_request.scalar_one_or_none():
            return None
        
        request = ProjectJoinRequest(
            project_id=project_id,
            user_id=user_id,
            message=message
        )
        session.add(request)
        await session.commit()
        await session.refresh(request)
        
        return request
    
    @staticmethod
    async def get_pending_requests(
        session: AsyncSession,
        project_id: UUID
    ) -> List[Dict[str, Any]]:
        """Get pending join requests for a project
        
        Args:
            session: Database session
            project_id: Project UUID
            
        Returns:
            List of request dicts with user info
        """
        result = await session.execute(
            select(ProjectJoinRequest, User)
            .join(User, ProjectJoinRequest.user_id == User.id)
            .where(
                and_(
                    ProjectJoinRequest.project_id == project_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
            .order_by(ProjectJoinRequest.created_at)
        )
        
        requests = []
        for req, user in result.fetchall():
            req_dict = req.to_dict()
            req_dict['user'] = {
                'id': str(user.id),
                'username': user.username,
                'display_name': user.display_name
            }
            requests.append(req_dict)
        
        return requests
    
    @staticmethod
    async def approve_join_request(
        session: AsyncSession,
        request_id: UUID,
        approved_by: UUID,
        role: str = 'member'
    ) -> Optional[ProjectMember]:
        """Approve a join request
        
        Args:
            session: Database session
            request_id: Request UUID
            approved_by: UUID of approving user
            role: Role to assign to new member
            
        Returns:
            ProjectMember if approved, None otherwise
        """
        # Get request
        result = await session.execute(
            select(ProjectJoinRequest).where(ProjectJoinRequest.id == request_id)
        )
        request = result.scalar_one_or_none()
        
        if not request or request.status != 'pending':
            return None
        
        # Update request status
        await session.execute(
            update(ProjectJoinRequest)
            .where(ProjectJoinRequest.id == request_id)
            .values(
                status='approved',
                processed_by=approved_by,
                processed_at=datetime.utcnow()
            )
        )
        
        # Add as member
        member = await ProjectRepository.add_member(
            session,
            request.project_id,
            request.user_id,
            role=role,
            invited_by=approved_by
        )
        
        return member
    
    @staticmethod
    async def reject_join_request(
        session: AsyncSession,
        request_id: UUID,
        rejected_by: UUID,
        reason: Optional[str] = None
    ) -> bool:
        """Reject a join request
        
        Args:
            session: Database session
            request_id: Request UUID
            rejected_by: UUID of rejecting user
            reason: Optional rejection reason
            
        Returns:
            bool: True if rejected
        """
        result = await session.execute(
            update(ProjectJoinRequest)
            .where(
                and_(
                    ProjectJoinRequest.id == request_id,
                    ProjectJoinRequest.status == 'pending'
                )
            )
            .values(
                status='rejected',
                processed_by=rejected_by,
                processed_at=datetime.utcnow(),
                rejection_reason=reason
            )
        )
        await session.commit()
        return result.rowcount > 0
    
    # ─── Utility Methods ────────────────────────────────────────────────
    
    @staticmethod
    async def has_permission(
        session: AsyncSession,
        project_id: UUID,
        user_id: UUID,
        permission: str
    ) -> bool:
        """Check if user has specific permission in project
        
        Args:
            session: Database session
            project_id: Project UUID
            user_id: User UUID
            permission: Permission key to check
            
        Returns:
            bool: True if has permission
        """
        member = await ProjectRepository.get_member(session, project_id, user_id)
        if not member:
            return False
        
        permissions = member.permissions or {}
        return permissions.get(permission, False)
    
    @staticmethod
    async def get_storage_path(project_id: UUID) -> str:
        """Get the storage path for a project
        
        Args:
            project_id: Project UUID
            
        Returns:
            str: Relative path to project storage
        """
        return f"_projects/project_{project_id}"
    
    @staticmethod
    async def get_user_storage_path(user_id: UUID) -> str:
        """Get the storage path for a user's personal storage
        
        Args:
            user_id: User UUID
            
        Returns:
            str: Relative path to user storage
        """
        return f"_users/user_{user_id}"
