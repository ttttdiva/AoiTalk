"""認証・セッション・cookie・token 検証・ログインログ関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403


class AuthMixin:
    """WebChatServer の認証系メソッド群。"""

    async def _get_user_info_from_websocket(
        self, websocket: WebSocket
    ) -> Optional[Dict[str, Any]]:
        """Resolve the authenticated user for a WebSocket connection."""
        if not self.auth_enabled:
            return {"id": "default_user", "username": "default_user", "role": "admin"}

        auth_header = websocket.headers.get("authorization")
        bearer_token = None
        if auth_header and auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:]
        else:
            bearer_token = websocket.query_params.get("token")
        if bearer_token:
            try:
                from ..auth_service import get_auth_service

                payload = get_auth_service().verify_token(bearer_token)
                if payload and USER_REPOSITORY_AVAILABLE and self._db_manager:
                    db_session = await self._db_manager.get_session()
                    try:
                        user = await UserRepository.get_by_id(
                            db_session, UUID(payload.user_id)
                        )
                        return user.to_dict() if user else None
                    finally:
                        await db_session.close()
            except Exception:
                return None

        cookie_header = websocket.headers.get("cookie")
        next_session = self._get_cookie_from_header(cookie_header, self.next_cookie_name)
        next_payload = self._decode_next_session_cookie(next_session)
        next_user_id = next_payload.get("sub") if next_payload else None

        username = None
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        if not session_id:
            session_id = self._get_cookie_from_header(cookie_header, self.legacy_cookie_name)
        if session_id:
            serializer = self._get_serializer()
            if serializer:
                try:
                    session_data = serializer.loads(
                        session_id, max_age=self.session_ttl_seconds
                    )
                    username = session_data.get("u")
                except Exception:
                    username = None

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None

        db_session = await self._db_manager.get_session()
        try:
            user = None
            if next_user_id:
                user = await UserRepository.get_by_id(db_session, UUID(next_user_id))
            elif username:
                user = await UserRepository.get_by_username(db_session, username)
            return user.to_dict() if user else None
        finally:
            await db_session.close()

    async def _websocket_session_allowed(self, session_id: str, user_id: str) -> bool:
        try:
            from ...memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            return await repo.user_has_session_access(session_id, user_id)
        except Exception as e:
            logger.warning("WebSocket session access check failed: %s", e)
            return False

    async def _setup_user_context(self, websocket: WebSocket):
        """
        Set up user context for os_operations permission checks and LLM session.

        Extracts user info from session cookie and sets the context
        so that file operations respect user/project permissions,
        and LLM client uses the correct user_id for Dreaming memory.
        """
        # Default to admin (backward compat for unauthenticated or fallback)
        user_id = None
        username = None
        is_admin = True
        project_ids = []

        try:
            if self.auth_enabled:
                # Get session from WebSocket cookies
                cookie_header = websocket.headers.get("cookie")
                session_id = self._get_cookie_from_header(
                    cookie_header, self.cookie_name
                )

                if session_id:
                    try:
                        serializer = self._get_serializer()
                        if serializer:
                            session_data = serializer.loads(
                                session_id, max_age=self.session_ttl_seconds
                            )
                            username = session_data.get("u")

                            if (
                                username
                                and USER_REPOSITORY_AVAILABLE
                                and self._db_manager
                            ):
                                # Get user from database
                                db_session = await self._db_manager.get_session()
                                try:
                                    user = await UserRepository.get_by_username(
                                        db_session, username
                                    )
                                    if user:
                                        user_id = str(user.id)
                                        is_admin = user.role == "admin"

                                        # Get user's projects for non-admin users
                                        if not is_admin:
                                            try:
                                                from ...memory.project_repository import (
                                                    ProjectRepository,
                                                )

                                                projects = await ProjectRepository.get_user_projects(
                                                    db_session, user.id
                                                )
                                                project_ids = [
                                                    str(p.get("id"))
                                                    for p in projects
                                                    if p.get("id")
                                                ]
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to get user projects: {e}"
                                                )
                                finally:
                                    await db_session.close()
                    except Exception as e:
                        logger.debug(f"Failed to parse session for user context: {e}")
        except Exception as e:
            logger.warning(f"Error setting up user context: {e}")

        # Set os_operations context
        if OS_OPS_CONTEXT_AVAILABLE and set_current_user_context:
            set_current_user_context(user_id, is_admin, project_ids)
            logger.debug(
                f"User context set: user_id={user_id}, is_admin={is_admin}, projects={len(project_ids)}"
            )

        # Set LLM client session context (for Dreaming memory per-user isolation)
        if (
            user_id
            and self._llm_client
            and hasattr(self._llm_client, "set_session_context")
        ):
            try:
                self._llm_client.set_session_context(
                    user_id=user_id,
                    metadata={"platform": "web", "username": username or ""},
                )
                logger.debug(f"LLM session context set: user_id={user_id}")
            except Exception as e:
                logger.warning(f"Failed to set LLM session context: {e}")

    async def _log_login_event(
        self,
        username: str,
        action: str,
        request: Request,
        success: bool = True,
        failure_reason: Optional[str] = None,
        session_duration: Optional[int] = None,
    ):
        """Log login/logout event to database

        Args:
            username: Username
            action: Action type ('login' or 'logout')
            request: FastAPI request object
            success: Whether the action was successful
            failure_reason: Reason for failure (if applicable)
            session_duration: Session duration in seconds (for logout events)
        """
        # Skip if database manager or repository is not available
        if self._db_manager is None or LoginLogRepository is None:
            return

        try:
            # Get client IP address
            ip_address = None
            if request.client:
                ip_address = request.client.host

            # Try to get IP from X-Forwarded-For header if behind proxy
            if not ip_address or ip_address == "127.0.0.1":
                forwarded_for = request.headers.get("X-Forwarded-For")
                if forwarded_for:
                    ip_address = forwarded_for.split(",")[0].strip()

            # Get user agent
            user_agent = request.headers.get("User-Agent", "")

            # Get database session
            session = await self._db_manager.get_session()
            try:
                await LoginLogRepository.create_log_entry(
                    session=session,
                    username=username,
                    action=action,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=success,
                    failure_reason=failure_reason,
                    session_duration_seconds=session_duration,
                )
            finally:
                await session.close()

            logger.info(
                f"Login event logged: {action} for {username} "
                f"(success={success}, ip={ip_address})"
            )

        except Exception as e:
            # Log error but don't fail the login/logout process
            logger.error(f"Failed to log login event: {e}")

    def _load_auth_settings(
        self,
    ) -> tuple[bool, Optional[str], Optional[str], Optional[str], int]:
        """Load authentication settings.

        DB認証に完全移行:
        - ユーザー名/パスワードは環境変数ではなくDBから取得
        - シークレットキーのみ環境変数から取得
        """
        auth_config: Dict[str, Any] = {}
        try:
            if hasattr(self.config, "get"):
                auth_config = self.config.get("web_interface.auth", {}) or {}
            elif isinstance(self.config, dict):
                auth_config = self.config.get("web_interface", {}).get("auth", {}) or {}
        except Exception as exc:
            logger.warning(f"WebUI 認証設定の取得に失敗しました: {exc}")

        # シークレットキーは環境変数から取得（セッション署名用）
        env_secret = os.getenv("AOITALK_WEB_AUTH_SECRET")
        secret = (env_secret or auth_config.get("secret") or "").strip() or None

        ttl_minutes = auth_config.get("session_ttl_minutes", 1440)
        try:
            ttl_minutes = int(ttl_minutes)
        except (TypeError, ValueError):
            ttl_minutes = 1440

        # DB認証が利用可能かチェック
        if USER_REPOSITORY_AVAILABLE and self._db_manager is not None:
            # DB認証モード: シークレット必須
            enabled = True
            if not secret:
                raise ValueError(
                    "WebUI 認証が有効ですが、AOITALK_WEB_AUTH_SECRET が設定されていません"
                )
            logger.info("WebUI 認証: DBベース認証が有効です")
            # username/password は None（DBから取得するため）
            return enabled, None, None, secret, max(60, ttl_minutes * 60)
        else:
            # DB利用不可の場合は認証無効
            logger.warning("WebUI 認証: UserRepositoryが利用不可のため、認証は無効です")
            return False, None, None, None, max(60, ttl_minutes * 60)

    async def _verify_credentials_async(
        self, username: str, password: str
    ) -> Optional[Any]:
        """Verify credentials against database (async).

        Returns User object if successful, None otherwise.
        """
        if not self.auth_enabled:
            return True

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            logger.warning("UserRepository not available for authentication")
            return None

        try:
            session = await self._db_manager.get_session()
            try:
                user = await UserRepository.authenticate(
                    session=session, username=username, password=password
                )
                return user
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return None

    def _verify_credentials(self, username: str, password: str) -> bool:
        """Verify credentials (sync wrapper for backward compatibility).

        Note: This is a sync wrapper. For full async support, use login endpoint directly.
        """
        if not self.auth_enabled:
            return True

        # 同期コンテキストでは非同期認証を実行できないため、
        # ログインエンドポイントでは直接 _verify_credentials_async を使用
        logger.warning(
            "_verify_credentials called in sync context - use _verify_credentials_async"
        )
        return False

    def _get_serializer(self) -> Optional[URLSafeTimedSerializer]:
        if not self.auth_secret:
            return None
        return URLSafeTimedSerializer(self.auth_secret, salt="aoitalk-webui-session-v2")

    def _sign_session(self, username: str) -> str:
        serializer = self._get_serializer()
        if not serializer:
            raise ValueError("WebUI 認証シークレットが未設定です")
        return serializer.dumps({"u": username})

    def _verify_session(self, session_id: str) -> bool:
        if not self.auth_enabled:
            return True
        serializer = self._get_serializer()
        if not serializer or not session_id:
            return False
        try:
            serializer.loads(session_id, max_age=self.session_ttl_seconds)
            return True
        except (BadSignature, SignatureExpired):
            return False

    def _decode_next_session_cookie(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """Decode the Next.js JWT session cookie used by the Web UI."""
        if not token:
            return None
        nextauth_secret = os.getenv("NEXTAUTH_SECRET")
        if not nextauth_secret:
            logger.error("NEXTAUTH_SECRET is required to verify Next.js session cookies")
            return None
        try:
            return jwt.decode(
                token,
                nextauth_secret,
                algorithms=["HS256"],
            )
        except Exception:
            return None

    def _verify_cookie_auth(self, request: Request) -> bool:
        session_id = request.cookies.get(self.cookie_name)
        if self._verify_session(session_id):
            return True

        legacy_session_id = request.cookies.get(self.legacy_cookie_name)
        if self._verify_session(legacy_session_id):
            return True

        return self._decode_next_session_cookie(
            request.cookies.get(self.next_cookie_name)
        ) is not None

    def _get_cookie_from_header(
        self, cookie_header: Optional[str], name: str
    ) -> Optional[str]:
        if not cookie_header:
            return None
        parts = cookie_header.split(";")
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return None

    def _is_request_authenticated(self, request: Request) -> bool:
        if not self.auth_enabled:
            return True
        # Next.js内部プロキシからのリクエストを許可
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            return bool(internal_key)  # 空文字の場合はFalse
        # Bearer token認証（モバイルアプリ / サーバー間アクセス）
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                return self._get_username_from_long_lived_token(token) is not None
            # JWTアクセストークン（モバイルアプリ用）
            if AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                return payload is not None
        # Cookie認証（Webブラウザ用）
        return self._verify_cookie_auth(request)

    def _enforce_cookie_auth(self, request: Request) -> None:
        if not self.auth_enabled:
            return
        if not self._is_request_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _get_username_from_long_lived_token(self, token: str) -> Optional[str]:
        """長期APIトークンを検証し、有効ならユーザー名を返す（同期）。

        同期セッションを使うため、同期の認証依存（スレッドプール実行）から呼ぶ。
        """
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None
        try:
            from ...memory.models import User

            with self._db_manager.get_sync_session() as session:
                record = ApiTokenRepository.verify_token_sync(session, token)
                if record is None:
                    return None
                user = session.get(User, record.user_id)
                if user is None or not user.is_active:
                    return None
                return user.username
        except Exception as exc:
            logger.warning(f"Long-lived token verification failed: {exc}")
            return None

    async def _get_user_info_from_long_lived_token(
        self, token: str
    ) -> Optional[Dict[str, Any]]:
        """長期APIトークンを検証し、有効ならユーザー情報dictを返す（非同期）。"""
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None
        if not USER_REPOSITORY_AVAILABLE:
            return None
        try:
            session = await self._db_manager.get_session()
            try:
                record = await ApiTokenRepository.verify_token(session, token)
                if record is None:
                    return None
                user = await UserRepository.get_by_id(session, record.user_id)
                if user is None or not user.is_active:
                    return None
                return user.to_dict()
            finally:
                await session.close()
        except Exception as exc:
            logger.error(f"Long-lived token user lookup failed: {exc}")
            return None

    def _get_username_from_request(self, request: Request) -> Optional[str]:
        """Extract username from session cookie or Bearer token.

        Returns:
            Username string if session is valid, None otherwise
        """
        if not self.auth_enabled:
            return None

        # Next.js内部プロキシからのx-forwarded-userヘッダー（internal-auth検証済みの場合のみ信頼）
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            forwarded_user = request.headers.get("x-forwarded-user")
            if forwarded_user:
                return forwarded_user

        # Bearer tokenから取得（モバイルアプリ / サーバー間アクセス）
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                username = self._get_username_from_long_lived_token(token)
                if username:
                    return username
            elif AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                if payload:
                    return payload.username

        # Cookieから取得（FastAPI直接ログイン/旧Cookie）
        session_id = request.cookies.get(self.cookie_name)
        if not session_id:
            session_id = request.cookies.get(self.legacy_cookie_name)
        if not session_id:
            return None

        serializer = self._get_serializer()
        if not serializer:
            return None

        try:
            session_data = serializer.loads(
                session_id, max_age=self.session_ttl_seconds
            )
            return session_data.get("u")
        except (BadSignature, SignatureExpired):
            return None

    def _get_next_user_id_from_request(self, request: Request) -> Optional[str]:
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            forwarded_user_id = request.headers.get("x-forwarded-user-id")
            if forwarded_user_id:
                return str(forwarded_user_id)

        payload = self._decode_next_session_cookie(
            request.cookies.get(self.next_cookie_name)
        )
        if not payload:
            return None
        user_id = payload.get("sub")
        return str(user_id) if user_id else None

    async def _is_admin_user(self, request: Request) -> bool:
        """Check if current user has admin role.

        Returns:
            True if user is admin, False otherwise
        """
        username = self._get_username_from_request(request)
        if not username:
            return False

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return False

        try:
            session = await self._db_manager.get_session()
            try:
                user = await UserRepository.get_by_username(session, username)
                if user and user.role == "admin":
                    return True
                return False
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to check admin status: {e}")
            return False

    async def _get_user_info_from_request(
        self, request: Request
    ) -> Optional[Dict[str, Any]]:
        """Get full user info from request session.

        Returns:
            User info dict with id, username, role, etc. or None if not authenticated
        """
        # 長期APIトークン（サーバー間アクセス）は非同期経路で先に解決し、
        # 同期DBアクセスでイベントループをブロックしないようにする。
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:]
            if bearer.startswith(LONG_LIVED_TOKEN_PREFIX):
                return await self._get_user_info_from_long_lived_token(bearer)

        next_user_id = self._get_next_user_id_from_request(request)
        username = None if next_user_id else self._get_username_from_request(request)
        if not username and not next_user_id:
            return None

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None

        try:
            session = await self._db_manager.get_session()
            try:
                if next_user_id:
                    from uuid import UUID

                    user = await UserRepository.get_by_id(session, UUID(next_user_id))
                else:
                    user = await UserRepository.get_by_username(session, username)
                if user:
                    return user.to_dict()
                return None
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
            return None
    def _set_session_cookie(
        self, response: JSONResponse, session_id: str, secure: bool
    ) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=self.session_ttl_seconds,
        )

    def _authorize_websocket(self, websocket: WebSocket) -> bool:
        if not self.auth_enabled:
            return True
        # Bearer token認証（モバイルアプリ用 — クエリパラメータ）
        token = websocket.query_params.get("token")
        if token and AUTH_SERVICE_AVAILABLE:
            payload = get_auth_service().verify_token(token)
            if payload is not None:
                return True
        # Cookie認証（Webブラウザ用）
        cookie_header = websocket.headers.get("cookie")
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        if self._verify_session(session_id):
            return True
        legacy_session_id = self._get_cookie_from_header(
            cookie_header, self.legacy_cookie_name
        )
        if self._verify_session(legacy_session_id):
            return True
        next_session = self._get_cookie_from_header(cookie_header, self.next_cookie_name)
        return self._decode_next_session_cookie(next_session) is not None

    def _verify_api_key(self, request: Request) -> bool:
        """Verify Bearer token for crawler API access"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]  # Remove "Bearer " prefix
        expected_key = os.getenv("CRAWLER_API_KEY")
        return token == expected_key if expected_key else False
