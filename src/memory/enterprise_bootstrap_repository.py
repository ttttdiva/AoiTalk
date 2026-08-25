"""Durable Enterprise LAN bootstrap state transitions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import EnterpriseBootstrapState, User


class EnterpriseBootstrapRepository:
    """Own the singleton bootstrap transition without username inference."""

    SINGLETON_ID = 1

    @staticmethod
    async def _get_state(
        session: AsyncSession, *, for_update: bool = False
    ) -> EnterpriseBootstrapState | None:
        statement = select(EnterpriseBootstrapState).where(
            EnterpriseBootstrapState.id == EnterpriseBootstrapRepository.SINGLETON_ID
        )
        if for_update:
            statement = statement.with_for_update()
        result = await session.execute(statement)
        return result.scalar_one_or_none()

    @staticmethod
    def _is_active_admin(user: User | None) -> bool:
        return bool(
            user is not None
            and getattr(user, "is_active", False)
            and str(getattr(user, "role", "")).lower() == "admin"
        )

    @staticmethod
    async def initialize(
        session: AsyncSession, *, configured_username: str
    ) -> EnterpriseBootstrapState:
        """Bind once, including a safe upgrade from the pre-state schema."""

        state = await EnterpriseBootstrapRepository._get_state(
            session, for_update=True
        )
        if state is None:
            result = await session.execute(
                select(User).where(User.username == configured_username).limit(1)
            )
            bootstrap_user = result.scalar_one_or_none()
            if (
                not EnterpriseBootstrapRepository._is_active_admin(bootstrap_user)
                or getattr(bootstrap_user, "is_password_reset_required", True)
            ):
                # The durable table is new, so an already-bootstrapped database
                # may have renamed its original administrator. Perform this
                # inference once only: a reset-complete active admin proves the
                # old LAN gate had already been passed. Prefer it even when the
                # configured username now points to a newly-created pending admin.
                # Persist that decision so later users/reset flags can never
                # relock the deployment.
                completed_result = await session.execute(
                    select(User)
                    .where(
                        User.role == "admin",
                        User.is_active.is_(True),
                        User.is_password_reset_required.is_(False),
                    )
                    .order_by(User.created_at.asc().nulls_last(), User.id.asc())
                    .limit(1)
                )
                bootstrap_user = completed_result.scalar_one_or_none()

            if not EnterpriseBootstrapRepository._is_active_admin(bootstrap_user):
                # A fresh deployment with a renamed configuration can still be
                # bound safely when there is exactly one active pending admin.
                # Multiple pending admins are ambiguous and fail closed.
                pending_result = await session.execute(
                    select(User)
                    .where(
                        User.role == "admin",
                        User.is_active.is_(True),
                        User.is_password_reset_required.is_(True),
                    )
                    .order_by(User.created_at.asc().nulls_last(), User.id.asc())
                    .limit(2)
                )
                pending_admins = list(pending_result.scalars().all())
                bootstrap_user = pending_admins[0] if len(pending_admins) == 1 else None

            if not EnterpriseBootstrapRepository._is_active_admin(bootstrap_user):
                raise RuntimeError(
                    "Enterprise bootstrap user is missing or ambiguous; refusing "
                    "to infer a privileged identity"
                )
            state = EnterpriseBootstrapState(
                id=EnterpriseBootstrapRepository.SINGLETON_ID,
                bootstrap_user_id=bootstrap_user.id,
            )
            session.add(state)
        elif state.completed_at is not None:
            return state
        elif state.bootstrap_user_id is None:
            raise RuntimeError("Enterprise bootstrap user identity is unavailable")
        else:
            bootstrap_user = await session.get(User, state.bootstrap_user_id)
            if not EnterpriseBootstrapRepository._is_active_admin(bootstrap_user):
                raise RuntimeError(
                    "The bound Enterprise bootstrap user must remain an active "
                    "administrator until bootstrap completes"
                )

        if not getattr(bootstrap_user, "is_password_reset_required", True):
            state.completed_at = datetime.utcnow()
            state.updated_at = datetime.utcnow()
        await session.flush()
        return state

    @staticmethod
    async def is_complete(session: AsyncSession) -> bool:
        """Return durable gate state, completing only for the bound user ID."""

        state = await EnterpriseBootstrapRepository._get_state(
            session, for_update=True
        )
        if state is None:
            return False
        if state.completed_at is not None:
            return True
        if state.bootstrap_user_id is None:
            return False

        bootstrap_user = await session.get(User, state.bootstrap_user_id)
        if not EnterpriseBootstrapRepository._is_active_admin(bootstrap_user):
            return False
        if getattr(bootstrap_user, "is_password_reset_required", True):
            return False

        state.completed_at = datetime.utcnow()
        state.updated_at = datetime.utcnow()
        await session.flush()
        return True
