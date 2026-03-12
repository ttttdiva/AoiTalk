"""
Authentication service with JWT token management
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from uuid import UUID

import jwt
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TokenPayload(BaseModel):
    """JWT token payload"""
    user_id: str
    username: str
    role: str
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
        # Check for environment variable first
        env_secret = os.getenv("AOITALK_JWT_SECRET") or os.getenv("AUTH_SECRET")
        
        if secret_key:
            self.secret_key = secret_key
        elif env_secret:
            self.secret_key = env_secret
        else:
            # Use default but warn strongly
            self.secret_key = "aoitalk-default-secret-change-in-production"
            logger.warning(
                "⚠️ 【セキュリティ警告】デフォルトのJWTシークレットを使用しています。"
                "本番環境では環境変数 AOITALK_JWT_SECRET を設定してください！"
            )
            logger.warning(
                "⚠️ [SECURITY WARNING] Using default JWT secret. "
                "Set AOITALK_JWT_SECRET environment variable for production!"
            )
        
        self.algorithm = algorithm
        self.access_token_expire_minutes = access_token_expire_minutes
        
    def create_access_token(
        self,
        user_id: str,
        username: str,
        role: str,
        expires_delta: Optional[timedelta] = None
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
            "type": "access"
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
            
            return TokenPayload(
                user_id=payload["user_id"],
                username=payload["username"],
                role=payload["role"],
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
        is_password_reset_required: bool = False
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
        token = self.create_access_token(user_id, username, role)
        
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
        """Refresh an existing token if still valid
        
        Args:
            token: Current JWT token
            
        Returns:
            New token if valid, None otherwise
        """
        payload = self.verify_token(token)
        if not payload:
            return None
        
        return self.create_access_token(
            user_id=payload.user_id,
            username=payload.username,
            role=payload.role
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
