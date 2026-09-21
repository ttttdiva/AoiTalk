"""
Main conversation memory manager
"""

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from .config import MemoryConfig
from .database import init_database
from .repository import ConversationRepository
from .services import SummarizationService, MemorySearchService, ConversationHistoryService
from .models import ConversationSession, ConversationMessage
from .cross_session_memory import get_cross_session_memory
from ..services.privacy_masking_projection import (
    is_privacy_masking_source,
    public_model_message_metadata,
)
from ..runtime import AsyncResourceScope
from ..utils.logging_config import FILE_ONLY_LOG_EXTRA


logger = logging.getLogger(__name__)


class ConversationMemoryManager:
    """Main manager for conversation memory and summarization"""
    
    def __init__(self, config: Optional[MemoryConfig] = None, app_config = None):
        """Initialize memory manager
        
        Args:
            config: Memory configuration. If None, uses default config.
            app_config: Application configuration for logging settings
        """
        self.config = config or MemoryConfig()
        self.app_config = app_config
        
        # Load conversation logging settings from app config if available
        if app_config:
            # Load memory configuration
            memory_config = app_config.get_memory_config()
            
            # Apply search settings
            if 'enable_search' in memory_config:
                self.config.enable_search = memory_config['enable_search']
            
            logging_config = app_config.get_conversation_logging_config()
            self.config.conversation_logging_enabled = logging_config.get('enabled', True)
            self.config.save_user_messages = logging_config.get('save_user_messages', True)
            self.config.save_assistant_messages = logging_config.get('save_assistant_messages', True)
            self.config.save_system_messages = logging_config.get('save_system_messages', False)
            self.config.save_function_calls = logging_config.get('save_function_calls', True)
            self.config.save_successful_only = logging_config.get('save_successful_only', False)
            self.config.log_retention_days = logging_config.get('log_retention_days', 365)
            self.config.auto_cleanup_enabled = logging_config.get('auto_cleanup_enabled', True)
            self.config.exclude_patterns = logging_config.get('exclude_patterns', [])
        
        # Keep the repository aware of whether semantic search is enabled.
        self.repository = ConversationRepository(enable_search=self.config.enable_search)
        self.summarization_service = SummarizationService(self.config)
        # Keep cross-session search dependencies lazy until a search is requested.
        self._search_service = None
        self.history_service = ConversationHistoryService(self.config)
        
        self._initialized = False
        self._current_sessions: Dict[str, ConversationSession] = {}
        self._summarization_tasks: Dict[str, asyncio.Task] = {}
        self._indexing_tasks: set[asyncio.Task] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._resource_scope = AsyncResourceScope("conversation-memory")
        self._cleanup_done = False
        self._cleanup_task: Optional[asyncio.Task] = None

    @staticmethod
    def _close_unawaited(coro: Any) -> None:
        """Close a coroutine that could not be scheduled.

        ``_spawn_background`` accepts a coroutine object so callers can build
        the operation inline.  If cleanup has already started (or task
        creation fails), simply dropping that object would emit Python's
        ``coroutine was never awaited`` warning.  Futures/tasks are not closed
        here because they may be owned by another component.
        """

        if inspect.iscoroutine(coro):
            coro.close()

    def _background_task_done(self, task: asyncio.Task) -> None:
        """Forget a completed task and retrieve any unhandled exception."""

        self._background_tasks.discard(task)
        self._indexing_tasks.discard(task)

        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            # Calling ``exception()`` above still retrieves the task result;
            # do not allow unusual BaseException subclasses to become an
            # unobserved task failure during interpreter shutdown.
            print(
                "[ConversationMemoryManager] Background task result retrieval "
                f"failed: {exc}"
            )
            return
        if exception is not None:
            print(
                "[ConversationMemoryManager] Background task failed: "
                f"{exception}"
            )

    def _summarization_task_done(
        self,
        session_key: str,
        task: asyncio.Task,
    ) -> None:
        """Remove a summary task only if it is still the current task."""

        if self._summarization_tasks.get(session_key) is task:
            self._summarization_tasks.pop(session_key, None)

    def _spawn_background(
        self,
        coro,
        *,
        name: str,
    ) -> Optional[asyncio.Task]:
        """Spawn and own a manager background operation.

        Every manager-owned background task goes through the resource scope so
        ``cleanup`` can cancel and await it before closing the database.  The
        coroutine is explicitly closed when creation is refused, preventing a
        dropped coroutine warning during shutdown races.
        """

        if self._cleanup_done:
            self._close_unawaited(coro)
            return None

        try:
            task = self._resource_scope.spawn(coro, name=name)
        except Exception as exc:
            self._close_unawaited(coro)
            print(
                f"[ConversationMemoryManager] Failed to start background task "
                f"{name}: {exc}"
            )
            return None

        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task
    
    @property
    def search_service(self):
        """Lazy initialization of search service"""
        if self._search_service is None and self.config.enable_search:
            self._search_service = MemorySearchService(self.config)
        return self._search_service
    
    async def initialize(self) -> bool:
        """Initialize memory system
        
        Returns:
            bool: True if initialization succeeded
        """
        try:
            success = await init_database(self.config.database_path)
            if success:
                self._initialized = True
                logger.info(
                    "Conversation memory system initialized",
                    extra=FILE_ONLY_LOG_EXTRA,
                )

            else:
                logger.warning(
                    "Conversation memory database initialization failed; continuing without memory",
                    extra=FILE_ONLY_LOG_EXTRA,
                )
                self._initialized = False
            return success
        except Exception as e:
            logger.warning(
                "Conversation memory initialization failed; continuing without memory: %s",
                e,
                extra=FILE_ONLY_LOG_EXTRA,
            )
            self._initialized = False
            return False
    
    async def get_or_create_session(self, user_id: str, character_name: str, project_id: Optional[str] = None) -> ConversationSession:
        """Get or create conversation session for user and character
        
        Args:
            user_id: User identifier
            character_name: Character name
            project_id: Optional project ID for new session creation
            
        Returns:
            ConversationSession: Active session or None if memory not initialized
        """
        if not self._initialized:
            success = await self.initialize()
            if not success:
                return None
        
        session_key = f"{user_id}:{character_name}"
        
        # Check if we have an active session in memory
        if session_key in self._current_sessions:
            session = self._current_sessions[session_key]
            if session.is_active:
                return session
        
        # Try to get existing active session from database
        session = await self.repository.get_active_session(user_id, character_name)
        
        if not session:
            # Create new session with project_id if provided
            session = await self.repository.create_session(user_id, character_name, project_id=project_id)
            if project_id:
                print(f"[ConversationMemoryManager] Created new session: {session.id} with project_id: {project_id}")
            else:
                print(f"[ConversationMemoryManager] Created new session: {session.id}")
        else:
            print(f"[ConversationMemoryManager] Using existing session: {session.id}")
        
        self._current_sessions[session_key] = session
        return session
    
    async def start_new_session(self, user_id: str, character_name: str) -> ConversationSession:
        """Force start a new conversation session, deactivating the current one
        
        Args:
            user_id: User identifier
            character_name: Character name
            
        Returns:
            ConversationSession: Newly created session
        """
        if not self._initialized:
            success = await self.initialize()
            if not success:
                return None
        
        session_key = f"{user_id}:{character_name}"
        
        # Deactivate current session if exists
        if session_key in self._current_sessions:
            old_session = self._current_sessions[session_key]
            if old_session and old_session.is_active:
                try:
                    # Starting a new session must not make the old session
                    # look recently active in the history list.
                    await self.repository.deactivate_session(
                        str(old_session.id),
                        touch_activity=False,
                    )
                    print(f"[ConversationMemoryManager] Deactivated old session: {old_session.id}")
                except Exception as e:
                    print(f"[ConversationMemoryManager] Failed to deactivate old session: {e}")
        
        # Remove from cache
        if session_key in self._current_sessions:
            del self._current_sessions[session_key]
        
        # Create new session
        session = await self.repository.create_session(user_id, character_name)
        print(f"[ConversationMemoryManager] Created new session: {session.id}")
        
        self._current_sessions[session_key] = session
        return session
    
    async def add_message(self, user_id: str, character_name: str, role: str, content: str,
                         metadata: Optional[Dict[str, Any]] = None, llm_client = None, 
                         success: bool = True) -> ConversationMessage:
        """Add message to conversation and handle summarization
        
        Args:
            user_id: User identifier
            character_name: Character name
            role: Message role ('user', 'assistant', or 'system')
            content: Message content
            metadata: Optional metadata
            llm_client: LLM client for summarization
            success: Whether this is a successful interaction
            
        Returns:
            ConversationMessage: Created message or None if memory not available
        """
        # Check if we should save this message type
        if not self._should_save_message(role, success):
            return None
        
        # Check exclude patterns
        if self._should_exclude_content(content):
            return None
        
        # Check if LLM client has a session_id set (from WebSocket)
        # If so, use add_message_to_session instead to avoid creating duplicate sessions
        session_id = None
        if llm_client and hasattr(llm_client, 'current_session_id'):
            session_id = llm_client.current_session_id
            
        if session_id:
            # Use the session ID provided by the client (from chat.js)
            print(f"[ConversationMemoryManager] Using provided session ID: {session_id}")
            return await self.add_message_to_session(
                session_id=session_id,
                role=role,
                content=content,
                metadata=metadata,
                success=success,
                llm_client=llm_client,
            )
        
        # No session_id provided, use get_or_create_session
        # Check if LLM client has a project_id set (for new session creation)
        project_id = None
        if llm_client and hasattr(llm_client, 'current_project_id'):
            project_id = llm_client.current_project_id
            if project_id:
                print(f"[ConversationMemoryManager] Project ID detected from LLM client: {project_id}")
        
        # Get or create session with project_id if available
        session = await self.get_or_create_session(user_id, character_name, project_id=project_id)
        if not session:
            return None
        
        # Add message to session
        message = await self.repository.add_message(
            str(session.id), role, content, metadata
        )
        updated_session = await self.repository.get_session_by_id(session.id)
        summary_session = updated_session or session
        
        # Log to history if history logging is enabled
        if self.config.enable_history_logging:
            await self.history_service.log_message(
                user_id=user_id,
                session_id=summary_session.id,
                character_name=summary_session.character_name,
                role=role,
                content=content,
                metadata=metadata
            )

        # Check if summarization is needed
        if (summary_session.message_count or 0) >= self.config.max_active_messages:
            # Start summarization in background
            await self._trigger_summarization(summary_session, llm_client)

        return message
    
    async def add_message_to_session(self, session_id: str, role: str, content: str,
                                     metadata: Optional[Dict[str, Any]] = None,
                                     success: bool = True,
                                     branch_from_message_id: Optional[str] = None,
                                     sender_type: Optional[str] = None,
                                     sender_id: Optional[str] = None,
                                     sender_display_name: Optional[str] = None,
                                     llm_client = None,
                                     message_id: Optional[str] = None) -> ConversationMessage:
        """Add message to a specific conversation session by ID
        
        Args:
            session_id: Session identifier (UUID string)
            role: Message role ('user', 'assistant', or 'system')
            content: Message content
            metadata: Optional metadata
            success: Whether this is a successful interaction
            
        Returns:
            ConversationMessage: Created message or None if memory not available
        """
        # Check if we should save this message type
        if not self._should_save_message(role, success):
            return None
        
        # Check exclude patterns
        if self._should_exclude_content(content):
            return None
        
        if not self._initialized:
            success = await self.initialize()
            if not success:
                return None

        # Add message directly to the specified session
        message_identity = {"message_id": message_id} if message_id else {}
        message = await self.repository.add_message(
            session_id,
            role,
            content,
            metadata,
            branch_from_message_id=branch_from_message_id,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_display_name=sender_display_name,
            **message_identity,
        )
        
        session_info = None
        # Get session info once for history logging, background indexing and summarization.
        try:
            session_info = await self.repository.get_session_by_id(session_id)
            if session_info and self.config.enable_history_logging:
                await self.history_service.log_message(
                    user_id=session_info.user_id,
                    session_id=session_info.id,
                    character_name=session_info.character_name,
                    role=role,
                    content=content,
                    metadata=metadata
                )
        except Exception as e:
            print(f"[ConversationMemoryManager] Warning: Could not log to history: {e}")

        # Index message in cross-session memory for future retrieval.  This is
        # intentionally background work: a Qdrant/indexing failure must never
        # fail the durable message write.
        # ``/masking`` source rows remain in the canonical transcript for
        # audit/UI purposes, but must never become reusable cross-session
        # retrieval/indexing material.  The marker is server-issued metadata;
        # do not infer it from slash text or prompt content.
        is_masking_source = bool(message and is_privacy_masking_source(message))
        if message and self.config.enable_search and session_info and not is_masking_source:
            message_id = str(message.id)
            message_created_at = message.created_at
            index_user_id = session_info.user_id
            index_character_name = session_info.character_name
            session_project_id = getattr(session_info, "project_id", None)
            index_project_id = (
                str(session_project_id) if session_project_id else None
            )

            async def _index_in_background():
                try:
                    cross_session_memory = get_cross_session_memory()
                    await cross_session_memory.index_message(
                        message_id=message_id,
                        session_id=session_id,
                        user_id=index_user_id,
                        role=role,
                        content=content,
                        character_name=index_character_name,
                        project_id=index_project_id,
                        timestamp=message_created_at,
                    )
                except Exception as e:
                    # Indexing failure should not affect main flow
                    print(f"[ConversationMemoryManager] Warning: Cross-session indexing failed: {e}")
            
            task = self._spawn_background(
                _index_in_background(),
                name=f"cross-session-index-{message_id}",
            )
            if task is not None:
                self._indexing_tasks.add(task)

        if session_info and (session_info.message_count or 0) >= self.config.max_active_messages:
            await self._trigger_summarization(session_info, llm_client)

        return message
    
    def _should_save_message(self, role: str, success: bool) -> bool:
        """Check if message should be saved based on configuration
        
        Args:
            role: Message role
            success: Whether this is a successful interaction
            
        Returns:
            bool: True if message should be saved
        """
        # Memory system is now unified - if disabled, don't save anything
        # (conversation_logging_enabled now simply returns memory_enabled)
        if not self.config.conversation_logging_enabled:
            return False
        
        # Check if only successful interactions should be saved
        if self.config.save_successful_only and not success:
            return False
        
        # Check role-specific settings
        if role == 'user' and not self.config.save_user_messages:
            return False
        elif role == 'assistant' and not self.config.save_assistant_messages:
            return False
        elif role == 'system' and not self.config.save_system_messages:
            return False
        
        return True
    
    def _should_exclude_content(self, content: str) -> bool:
        """Check if content should be excluded based on patterns
        
        Args:
            content: Content to check
            
        Returns:
            bool: True if content should be excluded
        """
        import re
        
        for pattern in self.config.exclude_patterns:
            try:
                if re.search(pattern, content, re.IGNORECASE):
                    return True
            except re.error:
                # Invalid regex pattern, skip
                continue
        
        return False
    
    async def _trigger_summarization(self, session: ConversationSession, llm_client = None):
        """Trigger background summarization for session
        
        Args:
            session: Conversation session
            llm_client: LLM client for summarization
        """
        # Avoid even constructing a summary coroutine once shutdown has
        # started.  The helper below remains defensive for direct callers that
        # hand it an already-created coroutine.
        if self._cleanup_done:
            return

        session_key = str(session.id)

        # Do not block the response path waiting for an existing summary task.
        existing_task = self._summarization_tasks.get(session_key)
        if existing_task is not None:
            if not existing_task.done():
                return
        
        # Start new summarization task with proper error handling
        task = self._spawn_background(
            self._summarize_session_with_cleanup(session, llm_client),
            name=f"summarization-{session_key}",
        )
        if task is None:
            return
        self._summarization_tasks[session_key] = task
        task.add_done_callback(
            lambda completed, key=session_key: self._summarization_task_done(
                key, completed
            )
        )
        print(f"[ConversationMemoryManager] Started summarization for session: {session.id}")
    
    async def _summarize_session_with_cleanup(self, session: ConversationSession, llm_client = None):
        """Wrapper for summarization with proper cleanup and error handling
        
        Args:
            session: Conversation session
            llm_client: LLM client for summarization
        """
        session_key = str(session.id)
        
        try:
            await self._summarize_session(session, llm_client)
        except asyncio.CancelledError:
            # Handle cancellation gracefully without error messages during normal shutdown
            pass
        except GeneratorExit:
            # Handle GeneratorExit specifically (occurs during Python shutdown)
            pass
        except Exception as e:
            # Only log unexpected errors, not cancellation or shutdown related ones
            if not str(e).startswith('coroutine ignored GeneratorExit'):
                print(f"[ConversationMemoryManager] Summarization error for session {session.id}: {e}")
        finally:
            # Remove task from tracking
            current_task = self._summarization_tasks.get(session_key)
            # A previous task may finish after a replacement task was
            # installed.  Never remove the replacement from the tracking map.
            if current_task is asyncio.current_task():
                self._summarization_tasks.pop(session_key, None)
    
    async def _summarize_session(self, session: ConversationSession, llm_client = None):
        """Summarize conversation session and archive
        
        Args:
            session: Conversation session
            llm_client: LLM client for summarization
        """
        # Get messages to summarize (all except the most recent ones to keep)
        get_active = getattr(self.repository, "get_active_branch_messages", None)
        all_messages = await (
            get_active(session.id)
            if callable(get_active)
            else self.repository.get_session_messages(session.id)
        )

        # A masking source is intentionally retained in the user-visible
        # transcript, but summarising it would copy raw confidential text into
        # ``current_summary``/archives and make it provider-visible later.
        all_messages = [
            message
            for message in all_messages
            if not is_privacy_masking_source(message)
        ]
        
        if len(all_messages) < self.config.max_active_messages:
            print(f"[ConversationMemoryManager] Not enough messages to summarize: {len(all_messages)}")
            return
        
        # Messages to summarize (exclude the most recent ones)
        messages_to_summarize = all_messages[:-self.config.summary_overlap]
        
        if not messages_to_summarize:
            print(f"[ConversationMemoryManager] No messages to summarize after overlap")
            return
        
        # Create summary
        summary = await self.summarization_service.create_summary(
            messages_to_summarize,
            llm_client,
            previous_summary=getattr(session, "current_summary", None),
        )

        if not summary:
            print(f"[ConversationMemoryManager] Failed to create summary")
            return

        start_time = messages_to_summarize[0].created_at
        end_time = messages_to_summarize[-1].created_at
        checkpoint = await self.repository.apply_summary_checkpoint(
            session_id=session.id,
            message_ids=[message.id for message in messages_to_summarize],
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            metadata={"summarization_config": self.config.__dict__},
            expected_previous_summary=getattr(
                session, "current_summary", None
            )
            or "",
        )
        if checkpoint is None:
            print(
                "[ConversationMemoryManager] Summary checkpoint discarded; "
                "active branch changed"
            )
            return
        archive, deleted_count = checkpoint

        print(f"[ConversationMemoryManager] Summarization complete:")
        print(f"  - Archive ID: {archive.id}")
        print(f"  - Messages summarized: {len(messages_to_summarize)}")
        print(f"  - Messages deleted: {deleted_count}")
        print(f"  - Messages kept: {self.config.summary_overlap}")
    
    async def search_conversation_memory(
        self,
        user_id: str,
        character_name: str,
        query: str,
        time_range: str = "all",
        max_results: Optional[int] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search conversation memory
        
        Args:
            user_id: User identifier
            character_name: Character name
            query: Search query
            time_range: Time range filter
            max_results: Maximum results to return
            project_id: Project scope. None searches only non-project conversations.
            
        Returns:
            List[Dict[str, Any]]: Search results
        """
        if not self._initialized:
            await self.initialize()

        if self.search_service is None:
            return []

        return await self.search_service.search_conversation_memory(
            user_id,
            character_name,
            query,
            time_range,
            max_results,
            project_id=project_id,
        )
    
    async def get_recent_messages(self, user_id: str, character_name: str, count: int = 10) -> List[Dict[str, Any]]:
        """Get recent messages from active session
        
        Args:
            user_id: User identifier
            character_name: Character name
            count: Number of messages to retrieve
            
        Returns:
            List[Dict[str, Any]]: Recent messages
        """
        if not self._initialized:
            await self.initialize()
        
        session = await self.repository.get_active_session(user_id, character_name)
        if not session:
            return []

        messages = await self.repository.get_recent_messages(session.id, count)
        # This method feeds legacy memory-prefill consumers (for example the
        # Discord adapter), not the user-facing conversation-history API.  A
        # masking source remains durable in the latter but must never become
        # future provider context through this compatibility projection.
        projected: list[dict[str, Any]] = []
        for msg in messages:
            if is_privacy_masking_source(msg):
                continue
            payload = msg.to_dict()
            if isinstance(payload, dict):
                payload["metadata"] = public_model_message_metadata(
                    payload.get("metadata") or {}
                )
            projected.append(payload)
        return projected
    
    async def add_function_call(self, user_id: str, character_name: str, function_name: str, 
                               function_args: Dict[str, Any], function_result: Any, 
                               success: bool = True, error_message: str = None) -> None:
        """Add function call information to conversation history
        
        Args:
            user_id: User identifier
            character_name: Character name
            function_name: Name of the function called
            function_args: Arguments passed to the function
            function_result: Result returned by the function
            success: Whether the function call was successful
            error_message: Error message if function call failed
        """
        # Check if we should save function calls
        if not self.config.save_function_calls or not self.config.conversation_logging_enabled:
            return
        
        # Check if only successful interactions should be saved
        if self.config.save_successful_only and not success:
            return
        
        # Prepare function call data
        function_call_data = {
            'function_name': function_name,
            'function_args': function_args,
            'function_result': str(function_result) if function_result is not None else None,
            'success': success,
            'error_message': error_message
        }
        
        # Log function call to history if history logging is enabled
        if self.config.enable_history_logging:
            await self.history_service.log_message(
                user_id=user_id,
                session_id=None,  # Function calls may not be tied to a specific session
                character_name=character_name,
                role='function',
                content=f"Function call: {function_name}",
                metadata={'function_call_data': function_call_data}
            )
    
    async def _cleanup_impl(self) -> None:
        """Run the one-and-only cleanup sequence for this manager."""

        print("[ConversationMemoryManager] Starting cleanup...")

        # The resource scope owns both summarization and indexing tasks.  It
        # cancels unfinished tasks and awaits every one before we release the
        # database they may still be using.
        try:
            await self._resource_scope.aclose()
        except Exception as exc:
            # One background cleanup failure must not prevent the database
            # manager from being closed.  AsyncResourceScope itself continues
            # through all registered cleanups before reporting failures.
            print(f"[ConversationMemoryManager] Background cleanup error: {exc}")
        finally:
            self._summarization_tasks.clear()
            self._indexing_tasks.clear()
            self._background_tasks.clear()

        # Close database connections only after all owned tasks have stopped.
        try:
            from .database import get_database_manager

            db_manager = get_database_manager()
            await db_manager.close()
        except Exception as exc:
            # Preserve the existing shutdown behavior: database cleanup errors
            # are logged/suppressed rather than escaping process teardown.
            print(f"[ConversationMemoryManager] Database cleanup error: {exc}")

        print("[ConversationMemoryManager] Cleanup complete")

    async def cleanup(self):
        """Cleanup resources and pending tasks exactly once.

        Concurrent callers share a single cleanup task.  ``_cleanup_done`` is
        set before that task is scheduled so no new background operation can
        race with shutdown.
        """

        cleanup_task = self._cleanup_task
        if cleanup_task is None:
            self._cleanup_done = True
            cleanup_task = asyncio.create_task(
                self._cleanup_impl(),
                name="conversation-memory-cleanup",
            )
            self._cleanup_task = cleanup_task

        # ``cleanup`` itself is never run as the implementation task, but keep
        # this guard defensive for tests/custom callers that invoke the helper
        # directly.
        if cleanup_task is asyncio.current_task():
            return
        await asyncio.shield(cleanup_task)
    
    def is_initialized(self) -> bool:
        """Check if memory manager is initialized
        
        Returns:
            bool: True if initialized
        """
        return self._initialized
