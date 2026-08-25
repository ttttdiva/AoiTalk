"""認証・セッション・cookie・token 検証・ログインログ関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403
from ...features import Features


class AuthMixin:
    """WebChatServer の認証系メソッド群。"""

    async def _get_user_info_from_websocket(
        self, websocket: WebSocket
    ) -> Optional[Dict[str, Any]]:
        """Resolve the authenticated user for a WebSocket connection."""
        if self.auth_enabled is False:
            return {"id": "default_user", "username": "default_user", "role": "admin"}

        auth_header_present, auth_header = self._get_authorization_header(
            websocket.headers
        )
        token_supplied = False
        bearer_token = None
        normalized_auth_header = (auth_header or "").strip()
        if auth_header_present:
            if not normalized_auth_header:
                token_supplied = True
            else:
                scheme, separator, value = normalized_auth_header.partition(" ")
                if scheme.lower() == "bearer":
                    bearer_token = value.strip() if separator else ""
                    token_supplied = True
                else:
                    # An unsupported Authorization scheme is still an explicit
                    # credential attempt; do not let a browser cookie override it.
                    token_supplied = True
        else:
            query_token = websocket.query_params.get("token")
            if query_token is not None:
                bearer_token = query_token.strip()
                token_supplied = True
        if token_supplied:
            try:
                if bearer_token and bearer_token.startswith(LONG_LIVED_TOKEN_PREFIX):
                    return await self._get_user_info_from_long_lived_token(
                        bearer_token
                    )
                from ..auth_service import get_auth_service

                payload = (
                    get_auth_service().verify_token(bearer_token)
                    if bearer_token
                    else None
                )
                if (
                    payload
                    and not getattr(payload, "is_password_reset_required", False)
                    and USER_REPOSITORY_AVAILABLE
                    and self._db_manager
                    and self._jwt_payload_allows_access(payload)
                ):
                    db_session = await self._db_manager.get_session()
                    try:
                        user = await UserRepository.get_by_id(
                            db_session, UUID(payload.user_id)
                        )
                        return (
                            user.to_dict()
                            if user
                            and user.is_active
                            and not user.is_password_reset_required
                            else None
                        )
                    finally:
                        await db_session.close()
            except Exception:
                return None
            # An explicitly supplied but invalid bearer/query token must not
            # fall through to an unrelated browser cookie.
            return None

        cookie_header = self._get_cookie_header(websocket.headers)
        username = None
        next_user_id = None
        next_payload = None
        session_version = None
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
                    session_version = int(session_data.get("v", 1) or 1)
                except Exception:
                    username = None
                    session_version = None

        # Match HTTP principal resolution: a valid FastAPI/legacy cookie wins
        # over a concurrently supplied Next.js cookie.  Only fall back to the
        # Next.js cookie when no valid FastAPI cookie resolved a principal.
        if not username:
            next_session = self._get_cookie_from_header(
                cookie_header, self.next_cookie_name
            )
            next_payload = self._decode_next_session_cookie(next_session)
            next_user_id = (
                next_payload.get("sub")
                if next_payload
                and not next_payload.get("password_reset_required")
                and await asyncio.to_thread(
                    self._next_payload_allows_access, next_payload
                )
                else None
            )
            session_version = (
                int(next_payload.get("session_version", 1) or 1)
                if next_user_id and next_payload
                else None
            )

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            return None

        db_session = await self._db_manager.get_session()
        try:
            user = None
            if next_user_id:
                user = await UserRepository.get_by_id(db_session, UUID(next_user_id))
            elif username:
                user = await UserRepository.get_by_username(db_session, username)
            if (
                user is None
                or not user.is_active
                or user.is_password_reset_required
                or not self._session_version_matches(
                    session_version, getattr(user, "session_version", 1)
                )
            ):
                return None
            return user.to_dict()
        finally:
            await db_session.close()

    async def _websocket_session_allowed(
        self,
        session_id: str,
        user_id: str,
        *,
        require_write: bool = False,
        is_admin: bool = False,
    ) -> bool:
        if not self.auth_enabled or is_admin:
            return True
        try:
            from ...memory.conversation_repository import ConversationRepository

            repo = ConversationRepository()
            if require_write:
                return await repo.user_has_session_write_access(session_id, user_id)
            return await repo.user_has_session_access(session_id, user_id)
        except Exception as e:
            logger.warning("WebSocket session access check failed: %s", e)
            return False

    async def _websocket_connection_allowed(self, websocket: WebSocket) -> bool:
        """Revalidate a connected WebSocket before server-side push delivery."""
        if not self.auth_enabled:
            return True
        try:
            user_info = await self._get_user_info_from_websocket(websocket)
            if not user_info or not user_info.get("id"):
                return False
            context = self.manager.connection_contexts.get(websocket, {})
            if str(context.get("user_id")) != str(user_info["id"]):
                return False
            session_id = context.get("session_id")
            if session_id and not await self._websocket_session_allowed(
                str(session_id),
                str(user_info["id"]),
                is_admin=user_info.get("role") == "admin",
            ):
                return False
            return True
        except Exception as exc:
            logger.warning("WebSocket push authorization failed: %s", exc)
            return False

    async def _websocket_is_admin_user(self, websocket: WebSocket) -> bool:
        """Push 時点の principal から管理者権限を再評価する。"""
        if not self.auth_enabled:
            return True
        try:
            user_info = await self._get_user_info_from_websocket(websocket)
            if not user_info or not user_info.get("id"):
                return False
            context = self.manager.connection_contexts.get(websocket, {})
            if str(context.get("user_id")) != str(user_info["id"]):
                return False
            return user_info.get("role") == "admin"
        except Exception as exc:
            logger.warning("WebSocket admin role check failed: %s", exc)
            return False

    async def _setup_user_context(
        self,
        websocket: WebSocket,
        user_info: Optional[Dict[str, Any]] = None,
    ):
        """
        Set up user context for os_operations permission checks and LLM session.

        Extracts user info from session cookie and sets the context
        so that file operations respect user/project permissions,
        and LLM client uses the correct user_id for Dreaming memory.
        """
        # 未認証運用では従来どおりdefault_userを管理者扱いにするが、
        # 認証有効時は主体が解決できない限り最小権限にする。
        user_id = None
        username = None
        is_admin = not self.auth_enabled
        project_ids = []
        writable_project_ids = []
        deletable_project_ids = []

        try:
            if self.auth_enabled:
                # WebSocketの認証主体は、直前にJWT/Next cookie/FastAPI cookieを
                # 共通解決した結果をそのまま使う。再解析してcookie種別を落とすと、
                # Next.jsログイン時にuser_id=None・管理者既定値へ戻るため。
                if user_info and user_info.get("id"):
                    user_id = str(user_info["id"])
                    username = str(user_info.get("username") or "") or None
                    is_admin = user_info.get("role") == "admin"

                    # Get user's projects for non-admin users
                    if not is_admin and USER_REPOSITORY_AVAILABLE and self._db_manager:
                        db_session = await self._db_manager.get_session()
                        try:
                            from ...memory.project_repository import ProjectRepository
                            from ...services.task_management._shared import (
                                _normalize_member_permissions,
                            )
                            from uuid import UUID

                            projects = await ProjectRepository.get_user_projects(
                                db_session, UUID(user_id)
                            )
                            for project in projects:
                                project_id = project.get("id")
                                membership = project.get("membership") or {}
                                if not project_id:
                                    continue
                                role = str(membership.get("role") or "member")
                                permissions = _normalize_member_permissions(
                                    role, membership.get("permissions")
                                )
                                project_readable = str(project.get("owner_id")) == user_id or (
                                    permissions.get("read") is True
                                )
                                if project_readable:
                                    project_ids.append(str(project_id))
                                if str(project.get("owner_id")) == user_id or (
                                    permissions.get("write") is True
                                ):
                                    writable_project_ids.append(str(project_id))
                                if str(project.get("owner_id")) == user_id or (
                                    permissions.get("delete") is True
                                ):
                                    deletable_project_ids.append(str(project_id))
                        except Exception as e:
                            logger.warning(f"Failed to get user projects: {e}")
                        finally:
                            await db_session.close()
        except Exception as e:
            logger.warning(f"Error setting up user context: {e}")

        # Set os_operations context
        if OS_OPS_CONTEXT_AVAILABLE and set_current_user_context:
            set_current_user_context(
                user_id,
                is_admin,
                project_ids,
                writable_project_ids,
                deletable_project_ids,
            )
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
        session: Optional[Any] = None,
    ) -> bool:
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
            return False

        try:
            # The audit row and login limiter must use the exact same trusted
            # proxy boundary; otherwise crafted forwarding headers can split
            # writes and reads across different rate-limit keys.
            peer = request.client.host if request.client else None
            ip_address = LoginLogRepository.resolve_login_client_ip(
                peer,
                request.headers.getlist("x-forwarded-for"),
            )

            # Get user agent
            user_agent = request.headers.get("User-Agent", "")

            owns_session = session is None
            if owns_session:
                session = await self._db_manager.get_session()
            try:
                if owns_session:
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
                else:
                    # PostgreSQL aborts an entire transaction after a failed
                    # statement. Isolate audit writes in a savepoint so the
                    # login transaction can still return the intended 503.
                    async with session.begin_nested():
                        await LoginLogRepository.create_log_entry(
                            session=session,
                            username=username,
                            action=action,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            success=success,
                            failure_reason=failure_reason,
                            session_duration_seconds=session_duration,
                            commit=False,
                        )
            finally:
                if owns_session:
                    await session.close()

            logger.info(
                f"Login event logged: {action} for {username} "
                f"(success={success}, ip={ip_address})"
            )
            return True

        except Exception as e:
            # 呼び出し側がEnterpriseでfail-closedにできるよう失敗を返す。
            logger.error(f"Failed to log login event: {e}")
            return False

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

        require_database = os.getenv("AOITALK_REQUIRE_DATABASE", "").lower() in {
            "1", "true", "yes", "on"
        }
        allow_unauthenticated_dev = os.getenv(
            "AOITALK_ALLOW_UNAUTHENTICATED_DEV", ""
        ).lower() in {"1", "true", "yes", "on"}
        enterprise = Features.is_enterprise()

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
            if require_database or enterprise or not allow_unauthenticated_dev:
                raise RuntimeError(
                    "WebUI authentication requires a working database and UserRepository; "
                    "set AOITALK_ALLOW_UNAUTHENTICATED_DEV=true only for isolated local development"
                )
            logger.warning(
                "WebUI 認証: UserRepositoryが利用不可のため、明示的な開発互換モードで認証を無効化します"
            )
            return False, None, None, None, max(60, ttl_minutes * 60)

    async def _verify_credentials_async(
        self,
        username: str,
        password: str,
        *,
        session: Optional[Any] = None,
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
            owns_session = session is None
            if owns_session:
                session = await self._db_manager.get_session()
            try:
                user = await UserRepository.authenticate(
                    session=session,
                    username=username,
                    password=password,
                    commit=owns_session,
                )
                return user
            finally:
                if owns_session:
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

    @staticmethod
    def _session_version_matches(
        token_version: Optional[Any], user_version: Optional[Any]
    ) -> bool:
        """Compare credential generation values without accepting malformed claims."""
        try:
            return int(token_version or 1) == int(user_version or 1)
        except (TypeError, ValueError):
            return False

    def _sign_session(
        self, username: str, session_version: Optional[int] = 1
    ) -> str:
        serializer = self._get_serializer()
        if not serializer:
            raise ValueError("WebUI 認証シークレットが未設定です")
        return serializer.dumps(
            {"u": username, "v": max(1, int(session_version or 1))}
        )

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

    def _signed_session_data(
        self, session_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        serializer = self._get_serializer()
        if not serializer or not session_id:
            return None
        try:
            session_data = serializer.loads(
                session_id, max_age=self.session_ttl_seconds
            )
            return session_data if isinstance(session_data, dict) else None
        except Exception:
            return None

    def _signed_session_username(self, session_id: Optional[str]) -> Optional[str]:
        session_data = self._signed_session_data(session_id)
        username = session_data.get("u") if session_data else None
        return str(username).strip() if username else None

    def _signed_session_allows_access(
        self,
        session_id: Optional[str],
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> bool:
        """Verify a FastAPI cookie against current DB user state.

        Signed cookies contain only a username for backward compatibility, so the
        reset-required flag must be checked against the current database row.
        """
        session_data = self._signed_session_data(session_id)
        username = session_data.get("u") if session_data else None
        if not username:
            return False
        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("User database is unavailable")
            return False
        try:
            from ...memory.models import User

            with self._db_manager.get_sync_session() as session:
                user = session.query(User).filter(User.username == username).first()
                if user is None or not user.is_active:
                    return False
                if not self._session_version_matches(
                    session_data.get("v", 1), getattr(user, "session_version", 1)
                ):
                    return False
                return allow_password_reset or not user.is_password_reset_required
        except Exception as exc:
            logger.warning("FastAPI session user verification failed: %s", exc)
            if raise_on_db_error:
                raise
            return False

    def _jwt_payload_allows_access(
        self,
        payload: Any,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> bool:
        """Check a FastAPI JWT against the current user row as well as its claim."""
        # FastAPI access JWTs are issued by the Personal mobile login flow.
        # Enterprise supports browser sessions and long-lived API tokens only,
        # including for tokens that were issued before the profile changed.
        if Features.is_enterprise():
            return False
        if not payload:
            return False
        if (
            getattr(payload, "is_password_reset_required", False)
            and not allow_password_reset
        ):
            return False
        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("User database is unavailable")
            return False
        try:
            from uuid import UUID
            from ...memory.models import User

            with self._db_manager.get_sync_session() as session:
                user = session.get(User, UUID(str(payload.user_id)))
                if user is None or not user.is_active:
                    return False
                if not self._session_version_matches(
                    getattr(payload, "session_version", 1),
                    getattr(user, "session_version", 1),
                ):
                    return False
                return allow_password_reset or not user.is_password_reset_required
        except Exception as exc:
            logger.warning("FastAPI JWT user verification failed: %s", exc)
            if raise_on_db_error:
                raise
            return False

    def _next_payload_allows_access(
        self,
        payload: Optional[Dict[str, Any]],
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> bool:
        """Check a Next.js session payload against current DB user state."""
        if not payload or not payload.get("sub"):
            return False
        if (
            payload.get("password_reset_required")
            and not allow_password_reset
        ):
            return False
        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("User database is unavailable")
            return False
        try:
            from uuid import UUID
            from ...memory.models import User

            with self._db_manager.get_sync_session() as session:
                user = session.get(User, UUID(str(payload["sub"])))
                if user is None or not user.is_active:
                    return False
                if not self._session_version_matches(
                    payload.get("session_version", 1),
                    getattr(user, "session_version", 1),
                ):
                    return False
                return allow_password_reset or not user.is_password_reset_required
        except Exception as exc:
            logger.warning("Next.js session user verification failed: %s", exc)
            if raise_on_db_error:
                raise
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

    def _verify_cookie_auth(
        self, request: Request, *, allow_password_reset: bool = False
    ) -> bool:
        session_id = self._get_request_cookie(request, self.cookie_name)
        if self._signed_session_allows_access(
            session_id, allow_password_reset=allow_password_reset
        ):
            return True

        legacy_session_id = self._get_request_cookie(request, self.legacy_cookie_name)
        if self._signed_session_allows_access(
            legacy_session_id, allow_password_reset=allow_password_reset
        ):
            return True

        next_payload = self._decode_next_session_cookie(
            self._get_request_cookie(request, self.next_cookie_name)
        )
        return self._next_payload_allows_access(
            next_payload, allow_password_reset=allow_password_reset
        )

    def _get_cookie_from_header(
        self, cookie_header: Optional[str], name: str
    ) -> Optional[str]:
        if not cookie_header:
            return None
        matches: list[str] = []
        parts = cookie_header.split(";")
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                matches.append(value)
        if len(matches) > 1:
            # The legacy FastAPI cookie and Next.js cookie historically share
            # a name.  If a client sends duplicate values, selecting one
            # would make the authenticated principal ambiguous, so fail
            # closed for every credential path.
            logger.warning("Rejecting duplicate cookie values for %s", name)
            return None
        return matches[0] if matches else None

    def _get_cookie_header(self, headers: Any) -> Optional[str]:
        """Return one Cookie field, rejecting repeated Cookie headers."""
        values = headers.getlist("cookie")
        if len(values) > 1:
            logger.warning("Rejecting repeated Cookie headers")
            return None
        return values[0] if values else None

    def _get_request_cookie(self, request: Request, name: str) -> Optional[str]:
        """Read one cookie while rejecting ambiguous duplicate values."""
        return self._get_cookie_from_header(
            self._get_cookie_header(request.headers), name
        )

    def _get_authorization_header(
        self, headers: Any
    ) -> tuple[bool, Optional[str]]:
        """Read one Authorization header and reject ambiguous forwarding.

        ASGI header containers preserve repeated fields via ``getlist``.  A
        proxy may also coalesce repeated fields into a comma-separated value.
        Neither form can be safely assigned to one principal, so callers get
        an explicit-but-invalid credential result and must fail closed rather
        than falling back to a browser cookie.
        """
        values = headers.getlist("authorization")
        if not values:
            return False, None
        if len(values) != 1 or "," in values[0]:
            logger.warning("Rejecting duplicate or coalesced Authorization headers")
            return True, None
        return True, values[0]

    def _get_bearer_token_from_request(
        self, request: Request
    ) -> tuple[bool, Optional[str]]:
        """Return whether Authorization is explicit and its bearer value.

        An explicit but malformed/unsupported Authorization header must not be
        allowed to fall through to a browser cookie from another principal.
        """
        auth_header_present, auth_header = self._get_authorization_header(
            request.headers
        )
        if not auth_header_present:
            return False, None
        normalized = (auth_header or "").strip()
        if not normalized:
            return True, None
        scheme, separator, value = normalized.partition(" ")
        if scheme.lower() != "bearer":
            return True, None
        return True, value.strip() if separator else ""

    def _is_request_authenticated(
        self, request: Request, *, allow_password_reset: bool = False
    ) -> bool:
        if not self.auth_enabled:
            return True
        # Next.js内部プロキシからのリクエストを許可
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            return bool(internal_key)  # 空文字の場合はFalse
        # Bearer token認証（モバイルアプリ / サーバー間アクセス）
        bearer_supplied, token = self._get_bearer_token_from_request(request)
        if bearer_supplied:
            if not token:
                return False
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                return self._get_username_from_long_lived_token(token) is not None
            # JWTアクセストークン（モバイルアプリ用）
            if AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                return self._jwt_payload_allows_access(
                    payload, allow_password_reset=allow_password_reset
                )
            return False
        # Cookie認証（Webブラウザ用）
        return self._verify_cookie_auth(
            request, allow_password_reset=allow_password_reset
        )

    def _enforce_cookie_auth(
        self, request: Request, *, allow_password_reset: bool = False
    ) -> None:
        if not self.auth_enabled:
            return
        if not self._is_request_authenticated(
            request, allow_password_reset=allow_password_reset
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _get_username_from_long_lived_token(
        self,
        token: str,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> Optional[str]:
        """長期APIトークンを検証し、有効ならユーザー名を返す（同期）。

        同期セッションを使うため、同期の認証依存（スレッドプール実行）から呼ぶ。
        """
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("Long-lived token database is unavailable")
            return None
        try:
            from ...memory.models import User

            with self._db_manager.get_sync_session() as session:
                record = ApiTokenRepository.verify_token_sync(session, token)
                if record is None:
                    return None
                user = session.get(User, record.user_id)
                if (
                    user is None
                    or not user.is_active
                    or (
                        user.is_password_reset_required
                        and not allow_password_reset
                    )
                    or not self._session_version_matches(
                        getattr(record, "session_version", 1),
                        getattr(user, "session_version", 1),
                    )
                ):
                    return None
                return user.username
        except Exception as exc:
            logger.warning(f"Long-lived token verification failed: {exc}")
            if raise_on_db_error:
                raise
            return None

    async def _get_user_info_from_long_lived_token(
        self,
        token: str,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """長期APIトークンを検証し、有効ならユーザー情報dictを返す（非同期）。"""
        if not token or not token.startswith(LONG_LIVED_TOKEN_PREFIX):
            return None
        if not API_TOKEN_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("Long-lived token database is unavailable")
            return None
        if not USER_REPOSITORY_AVAILABLE:
            if raise_on_db_error:
                raise RuntimeError("User repository is unavailable")
            return None
        try:
            session = await self._db_manager.get_session()
            try:
                record = await ApiTokenRepository.verify_token(session, token)
                if record is None:
                    return None
                user = await UserRepository.get_by_id(session, record.user_id)
                if (
                    user is None
                    or not user.is_active
                    or (
                        user.is_password_reset_required
                        and not allow_password_reset
                    )
                    or not self._session_version_matches(
                        getattr(record, "session_version", 1),
                        getattr(user, "session_version", 1),
                    )
                ):
                    return None
                return user.to_dict()
            finally:
                await session.close()
        except Exception as exc:
            logger.error(f"Long-lived token user lookup failed: {exc}")
            if raise_on_db_error:
                raise
            return None

    def _get_username_from_request(
        self,
        request: Request,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> Optional[str]:
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
        bearer_supplied, token = self._get_bearer_token_from_request(request)
        if bearer_supplied:
            if not token:
                return None
            # 長期APIトークン（サーバー間アクセス用）
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                username = self._get_username_from_long_lived_token(
                    token,
                    allow_password_reset=allow_password_reset,
                    raise_on_db_error=raise_on_db_error,
                )
                if username:
                    return username
            elif AUTH_SERVICE_AVAILABLE:
                payload = get_auth_service().verify_token(token)
                if self._jwt_payload_allows_access(
                    payload,
                    allow_password_reset=allow_password_reset,
                    raise_on_db_error=raise_on_db_error,
                ):
                    return payload.username
            return None

        # Cookieから取得（FastAPI直接ログイン/旧Cookie）
        session_id = self._get_request_cookie(request, self.cookie_name)
        if not session_id:
            session_id = self._get_request_cookie(request, self.legacy_cookie_name)
        if not session_id:
            return None

        serializer = self._get_serializer()
        if not serializer:
            return None

        try:
            session_data = serializer.loads(
                session_id, max_age=self.session_ttl_seconds
            )
            if not self._signed_session_allows_access(
                session_id,
                allow_password_reset=allow_password_reset,
                raise_on_db_error=raise_on_db_error,
            ):
                return None
            return session_data.get("u")
        except (BadSignature, SignatureExpired):
            return None

    def _get_next_user_id_from_request(
        self,
        request: Request,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> Optional[str]:
        internal_key = request.headers.get("x-internal-auth")
        if internal_key and internal_key == os.environ.get("INTERNAL_API_KEY", ""):
            forwarded_user_id = request.headers.get("x-forwarded-user-id")
            if forwarded_user_id:
                return str(forwarded_user_id)

        payload = self._decode_next_session_cookie(
            self._get_request_cookie(request, self.next_cookie_name)
        )
        if not payload or (
            payload.get("password_reset_required") and not allow_password_reset
        ):
            return None
        if not self._next_payload_allows_access(
            payload,
            allow_password_reset=allow_password_reset,
            raise_on_db_error=raise_on_db_error,
        ):
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
        self,
        request: Request,
        *,
        allow_password_reset: bool = False,
        raise_on_db_error: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Get full user info from request session.

        Returns:
            User info dict with id, username, role, etc. or None if not authenticated
        """
        # 長期APIトークン（サーバー間アクセス）は非同期経路で先に解決し、
        # 同期DBアクセスでイベントループをブロックしないようにする。
        bearer_supplied, bearer = self._get_bearer_token_from_request(request)
        if bearer_supplied:
            if not bearer:
                return None
            if bearer.startswith(LONG_LIVED_TOKEN_PREFIX):
                return await self._get_user_info_from_long_lived_token(
                    bearer,
                    allow_password_reset=allow_password_reset,
                    raise_on_db_error=raise_on_db_error,
                )
            if not AUTH_SERVICE_AVAILABLE:
                return None
            payload = get_auth_service().verify_token(bearer)
            if not self._jwt_payload_allows_access(
                payload,
                allow_password_reset=allow_password_reset,
                raise_on_db_error=raise_on_db_error,
            ):
                # An invalid Bearer credential must not fall through to a
                # browser cookie that happened to be sent on the same request.
                return None
            bearer_username = payload.username
        else:
            bearer_username = None

        if bearer_supplied:
            next_user_id = None
            username = bearer_username
        else:
            # Keep principal resolution in the same order as
            # ``_verify_cookie_auth``.  A valid FastAPI cookie must not be
            # shadowed by a different Next.js cookie sent on the same request;
            # otherwise authentication and authorization could refer to
            # different users.
            username = self._get_username_from_request(
                request,
                allow_password_reset=allow_password_reset,
                raise_on_db_error=raise_on_db_error,
            )
            next_user_id = None
            if not username:
                next_user_id = self._get_next_user_id_from_request(
                    request,
                    allow_password_reset=allow_password_reset,
                    raise_on_db_error=raise_on_db_error,
                )
        if not username and not next_user_id:
            return None

        if not USER_REPOSITORY_AVAILABLE or self._db_manager is None:
            if raise_on_db_error:
                raise RuntimeError("User database is unavailable")
            return None

        try:
            session = await self._db_manager.get_session()
            try:
                if next_user_id:
                    from uuid import UUID

                    user = await UserRepository.get_by_id(session, UUID(next_user_id))
                else:
                    user = await UserRepository.get_by_username(session, username)
                if user and user.is_active and (
                    allow_password_reset or not user.is_password_reset_required
                ):
                    return user.to_dict()
                return None
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
            if raise_on_db_error:
                raise
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

    async def _authorize_websocket(self, websocket: WebSocket) -> bool:
        if self.auth_enabled is False:
            return True
        # Bearer token認証（モバイルアプリ用 — クエリパラメータ）
        auth_header_present, auth_header = self._get_authorization_header(
            websocket.headers
        )
        auth_header = auth_header or ""
        normalized_auth_header = auth_header.strip()
        token_supplied = False
        token = None
        if auth_header_present:
            if not normalized_auth_header:
                token_supplied = True
            else:
                scheme, separator, value = normalized_auth_header.partition(" ")
                if scheme.lower() == "bearer":
                    token = value.strip() if separator else ""
                token_supplied = True
        else:
            query_token = websocket.query_params.get("token")
            if query_token is not None:
                token = query_token.strip()
                token_supplied = True
        if token_supplied and token and AUTH_SERVICE_AVAILABLE:
            if token.startswith(LONG_LIVED_TOKEN_PREFIX):
                if await self._get_user_info_from_long_lived_token(token):
                    return True
                return False
            payload = get_auth_service().verify_token(token)
            return payload is not None and await asyncio.to_thread(
                self._jwt_payload_allows_access, payload
            )
        if token_supplied:
            return False
        # Cookie認証（Webブラウザ用）
        cookie_header = self._get_cookie_header(websocket.headers)
        session_id = self._get_cookie_from_header(cookie_header, self.cookie_name)
        if await asyncio.to_thread(self._signed_session_allows_access, session_id):
            return True
        legacy_session_id = self._get_cookie_from_header(
            cookie_header, self.legacy_cookie_name
        )
        if await asyncio.to_thread(
            self._signed_session_allows_access, legacy_session_id
        ):
            return True
        next_session = self._get_cookie_from_header(cookie_header, self.next_cookie_name)
        next_payload = self._decode_next_session_cookie(next_session)
        return await asyncio.to_thread(self._next_payload_allows_access, next_payload)

    def _verify_api_key(self, request: Request) -> bool:
        """Verify Bearer token for crawler API access"""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return False
        token = auth_header[7:]  # Remove "Bearer " prefix
        expected_key = os.getenv("CRAWLER_API_KEY")
        return token == expected_key if expected_key else False
