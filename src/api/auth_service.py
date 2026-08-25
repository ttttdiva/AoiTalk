"""
Authentication service with JWT token management
"""

import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from uuid import UUID

import jwt
from pydantic import BaseModel
from src.security_secret import auth_secret_required, resolve_auth_secret_env

logger = logging.getLogger(__name__)


class TokenPayload(BaseModel):
    """JWT token payload"""
    user_id: str
    username: str
    role: str
    is_password_reset_required: bool = False
    session_version: int = 1
    exp: datetime
    iat: datetime
    
    
class AuthResult(BaseModel):
    """Authentication result"""
    success: bool
    user_id: Optional[str] = None
    username: Optional[str] = None
    role: Optional[str] = None
    access_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: Optional[int] = None
    is_password_reset_required: bool = False
    error: Optional[str] = None


class AuthService:
    """JWT-based authentication service"""
    
    def __init__(
        self,
        secret_key: Optional[str] = None,
        algorithm: str = "HS256",
        access_token_expire_minutes: int = 60 * 24  # 24 hours
    ):
        """Initialize auth service
        
        Args:
            secret_key: JWT signing secret (defaults to env var)
            algorithm: JWT algorithm
            access_token_expire_minutes: Token expiry in minutes
        """
        provided_secret = secret_key

        if secret_key is not None and not secret_key.strip():
            raise ValueError("JWT signing secret must not be blank")
        if provided_secret is not None:
            self.secret_key = provided_secret
        else:
            env_secret = resolve_auth_secret_env(
                ("AOITALK_JWT_SECRET", "AUTH_SECRET"),
                error_type=ValueError,
            )
            if env_secret is not None:
                self.secret_key = env_secret
            elif auth_secret_required():
                raise ValueError(
                    "AOITALK_JWT_SECRET (or AUTH_SECRET) is required for Enterprise authentication"
                )
            else:
                # Development/test compatibility only. Enterprise never reaches
                # this fallback because it requires an explicit secret.
                self.secret_key = secrets.token_urlsafe(32)
                logger.warning(
                    "開発用の既定JWTシークレットを使用しています。本番では AOITALK_JWT_SECRET を設定してください。"
                )
        
        self.algorithm = algorithm
        self.access_token_expire_minutes = access_token_expire_minutes
        
    def create_access_token(
        self,
        user_id: str,
        username: str,
        role: str,
        expires_delta: Optional[timedelta] = None,
        is_password_reset_required: bool = False,
        session_version: int = 1,
    ) -> str:
        """Create JWT access token
        
        Args:
            user_id: User UUID as string
            username: Username
            role: User role
            expires_delta: Optional custom expiry
            
        Returns:
            str: JWT token
        """
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=self.access_token_expire_minutes)
        
        payload = {
            "user_id": user_id,
            "username": username,
            "role": role,
            "exp": expire,
            "iat": datetime.utcnow(),
            "type": "access",
            "password_reset_required": bool(is_password_reset_required),
            "session_version": max(1, int(session_version or 1)),
        }
        
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
    
    def verify_token(self, token: str) -> Optional[TokenPayload]:
        """Verify and decode JWT token
        
        Args:
            token: JWT token string
            
        Returns:
            TokenPayload if valid, None otherwise
        """
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm]
            )
            if payload.get("type") != "access":
                raise jwt.InvalidTokenError("Only access tokens can be verified")
            
            return TokenPayload(
                user_id=payload["user_id"],
                username=payload["username"],
                role=payload["role"],
                is_password_reset_required=bool(
                    payload.get("password_reset_required", False)
                ),
                session_version=max(1, int(payload.get("session_version", 1))),
                exp=datetime.fromtimestamp(payload["exp"]),
                iat=datetime.fromtimestamp(payload["iat"])
            )
        except jwt.ExpiredSignatureError:
            logger.warning("Token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid token: {e}")
            return None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None
    
    def create_auth_result(
        self,
        user_id: str,
        username: str,
        role: str,
        is_password_reset_required: bool = False,
        session_version: int = 1,
    ) -> AuthResult:
        """Create successful auth result with token
        
        Args:
            user_id: User UUID as string
            username: Username
            role: User role
            is_password_reset_required: Whether password change is needed
            
        Returns:
            AuthResult: Success result with token
        """
        token = self.create_access_token(
            user_id,
            username,
            role,
            is_password_reset_required=is_password_reset_required,
            session_version=session_version,
        )
        
        return AuthResult(
            success=True,
            user_id=user_id,
            username=username,
            role=role,
            access_token=token,
            expires_in=self.access_token_expire_minutes * 60,  # in seconds
            is_password_reset_required=is_password_reset_required
        )
    
    @staticmethod
    def create_error_result(error: str) -> AuthResult:
        """Create failed auth result
        
        Args:
            error: Error message
            
        Returns:
            AuthResult: Failure result
        """
        return AuthResult(
            success=False,
            error=error
        )
    
    def refresh_token(self, token: str) -> Optional[str]:
        """Refresh a signed access token within its bounded refresh window.
        
        Args:
            token: Current JWT token
            
        Returns:
            New token if valid, None otherwise
        """
        payload = self.verify_token(token)
        if not payload:
            try:
                raw_payload = jwt.decode(
                    token,
                    self.secret_key,
                    algorithms=[self.algorithm],
                    options={"verify_exp": False},
                )
                if raw_payload.get("type") != "access":
                    return None
                issued_at = datetime.utcfromtimestamp(raw_payload["iat"])
                expires_at = datetime.utcfromtimestamp(raw_payload["exp"])
                now = datetime.utcnow()
                # Access-token expiry is not the refresh-window boundary. A
                # mobile client can be offline while its short-lived access
                # token expires, so allow a signed token to refresh until it
                # reaches the 30-day maximum age. The route additionally
                # rechecks the user's active/session state in the database.
                if (
                    issued_at > now + timedelta(seconds=60)
                    or now - issued_at > timedelta(days=30)
                ):
                    return None
                payload = TokenPayload(
                    user_id=raw_payload["user_id"],
                    username=raw_payload["username"],
                    role=raw_payload["role"],
                    is_password_reset_required=bool(
                        raw_payload.get("password_reset_required", False)
                    ),
                    session_version=max(1, int(raw_payload.get("session_version", 1))),
                    exp=expires_at,
                    iat=issued_at,
                )
            except Exception as exc:
                logger.warning(f"Token refresh decode failed: {exc}")
                return None
        
        if payload.is_password_reset_required:
            return None

        return self.create_access_token(
            user_id=payload.user_id,
            username=payload.username,
            role=payload.role,
            session_version=payload.session_version,
        )
    
    def extract_token_from_header(self, authorization: Optional[str]) -> Optional[str]:
        """Extract token from Authorization header
        
        Args:
            authorization: Authorization header value
            
        Returns:
            Token string or None
        """
        if not authorization:
            return None
        
        parts = authorization.split()
        if len(parts) != 2:
            return None
        
        scheme, token = parts
        if scheme.lower() != "bearer":
            return None
        
        return token
    
    def is_admin(self, token: str) -> bool:
        """Check if token belongs to admin user
        
        Args:
            token: JWT token
            
        Returns:
            bool: True if admin
        """
        payload = self.verify_token(token)
        if not payload:
            return False
        return payload.role == "admin"


# Global instance
_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    """Get or create global auth service instance
    
    Returns:
        AuthService: Global instance
    """
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service
