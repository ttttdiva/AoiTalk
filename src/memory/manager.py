"""
Main conversation memory manager
"""

import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
from .config import MemoryConfig
from .database import init_database
from .repository import ConversationRepository
from .services import SummarizationService, MemorySearchService, ConversationHistoryService
from .models import ConversationSession, ConversationMessage
from .cross_session_memory import get_cross_session_memory


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
            
            # Apply other memory settings
            if 'embedding_model' in memory_config:
                self.config.embedding_model = memory_config['embedding_model']
            if 'preload_embedding_model' in memory_config:
                self.config.preload_embedding_model = memory_config['preload_embedding_model']
                
            # If search is disabled, also disable embedding model preloading
            if not self.config.enable_search:
                self.config.preload_embedding_model = False
            
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
        
        # Pass enable_search to repository to avoid loading embedding model when search is disabled
        self.repository = ConversationRepository(enable_search=self.config.enable_search)
        self.summarization_service = SummarizationService(self.config)
        # Lazy initialization of search service to avoid loading embedding model when search is disabled
        self._search_service = None
        self.history_service = ConversationHistoryService(self.config)
        
        self._initialized = False
        self._current_sessions: Dict[str, ConversationSession] = {}
        self._summarization_tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_done = False
    
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
                print("[ConversationMemoryManager] Memory system initialized")
                
                # Always preload embedding model if search is enabled
                if hasattr(self.config, 'enable_search') and self.config.enable_search:
                    try:
                        from .embedding import get_embedding_manager
                        embedding_manager = get_embedding_manager(self.config.embedding_model)
                        await embedding_manager.preload_model()
                        print("[ConversationMemoryManager] Embedding model preloaded (enable_search: true)")
                    except Exception as e:
                        print(f"[ConversationMemoryManager] Failed to preload embedding model: {e}")
                else:
                    print("[ConversationMemoryManager] Memory search is disabled, skipping embedding model loading")
            else:
                print("[ConversationMemoryManager] Database initialization failed, continuing without memory")
                self._initialized = False
            return success
        except Exception as e:
            print(f"[ConversationMemoryManager] Initialization failed: {e}, continuing without memory")
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
                    await self.repository.deactivate_session(str(old_session.id))
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
                success=success
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
        
        # Log to history if history logging is enabled
        if self.config.enable_history_logging:
            await self.history_service.log_message(
                user_id=user_id,
                session_id=session.id,
                character_name=character_name,
                role=role,
                content=content,
                metadata=metadata
            )
        
        # Check if summarization is needed
        if session.message_count >= self.config.max_active_messages:
            # Start summarization in background
            await self._trigger_summarization(session, llm_client)
        
        return message
    
    async def add_message_to_session(self, session_id: str, role: str, content: str,
                                     metadata: Optional[Dict[str, Any]] = None, 
                                     success: bool = True) -> ConversationMessage:
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
        message = await self.repository.add_message(
            session_id, role, content, metadata
        )
        
        # Get session info for history logging
        try:
            from .database import get_db_session
            from .models import ConversationSession
            from sqlalchemy import select
            
            async with await get_db_session() as db_session:
                result = await db_session.execute(
                    select(ConversationSession).where(ConversationSession.id == session_id)
                )
                session = result.scalar_one_or_none()
                
                if session and self.config.enable_history_logging:
                    await self.history_service.log_message(
                        user_id=session.user_id,
                        session_id=session.id,
                        character_name=session.character_name,
                        role=role,
                        content=content,
                        metadata=metadata
                    )
        except Exception as e:
            print(f"[ConversationMemoryManager] Warning: Could not log to history: {e}")
        
        # Index message in cross-session memory for future retrieval (fire-and-forget)
        if message:
            async def _index_in_background():
                try:
                    cross_session_memory = get_cross_session_memory()
                    # Get session info for indexing
                    async with await get_db_session() as db_session:
                        result = await db_session.execute(
                            select(ConversationSession).where(ConversationSession.id == session_id)
                        )
                        session = result.scalar_one_or_none()
                        
                        if session:
                            await cross_session_memory.index_message(
                                message_id=str(message.id),
                                session_id=session_id,
                                user_id=session.user_id,
                                role=role,
                                content=content,
                                character_name=session.character_name,
                                timestamp=message.created_at
                            )
                except Exception as e:
                    # Indexing failure should not affect main flow
                    print(f"[ConversationMemoryManager] Warning: Cross-session indexing failed: {e}")
            
            # Fire-and-forget: don't await, let it run in background
            import asyncio
            asyncio.create_task(_index_in_background())
        
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
        # Temporarily disable summarization to avoid cleanup errors
        # TODO: Re-enable with proper async task management
        return
        
        # Skip if cleanup is in progress
        if self._cleanup_done:
            return
            
        session_key = f"{session.user_id}:{session.character_name}"
        
        # Cancel existing summarization task if running
        if session_key in self._summarization_tasks:
            existing_task = self._summarization_tasks[session_key]
            if not existing_task.done():
                try:
                    existing_task.cancel()
                    # Wait briefly for cancellation to complete
                    try:
                        await asyncio.wait_for(existing_task, timeout=0.1)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                except Exception:
                    pass
        
        # Start new summarization task with proper error handling
        try:
            task = asyncio.create_task(
                self._summarize_session_with_cleanup(session, llm_client)
            )
            self._summarization_tasks[session_key] = task
            # Set task name for better debugging
            task.set_name(f"summarization-{session_key}")
            print(f"[ConversationMemoryManager] Started summarization for session: {session.id}")
        except Exception as e:
            print(f"[ConversationMemoryManager] Failed to start summarization: {e}")
    
    async def _summarize_session_with_cleanup(self, session: ConversationSession, llm_client = None):
        """Wrapper for summarization with proper cleanup and error handling
        
        Args:
            session: Conversation session
            llm_client: LLM client for summarization
        """
        session_key = f"{session.user_id}:{session.character_name}"
        
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
            if session_key in self._summarization_tasks:
                try:
                    del self._summarization_tasks[session_key]
                except KeyError:
                    pass
    
    async def _summarize_session(self, session: ConversationSession, llm_client = None):
        """Summarize conversation session and archive
        
        Args:
            session: Conversation session
            llm_client: LLM client for summarization
        """
        # Get messages to summarize (all except the most recent ones to keep)
        all_messages = await self.repository.get_session_messages(session.id)
        
        if len(all_messages) < self.config.max_active_messages:
            print(f"[ConversationMemoryManager] Not enough messages to summarize: {len(all_messages)}")
            return
        
        # Messages to summarize (exclude the most recent ones)
        messages_to_summarize = all_messages[:-self.config.summary_overlap]
        
        if not messages_to_summarize:
            print(f"[ConversationMemoryManager] No messages to summarize after overlap")
            return
        
        # Create summary
        summary = await self.summarization_service.create_summary(messages_to_summarize, llm_client)
        
        if not summary:
            print(f"[ConversationMemoryManager] Failed to create summary")
            return
        
        # Create archive
        start_time = messages_to_summarize[0].created_at
        end_time = messages_to_summarize[-1].created_at
        
        archive = await self.repository.create_archive(
            user_id=session.user_id,
            character_name=session.character_name,
            original_session_id=session.id,
            summary=summary,
            message_count=len(messages_to_summarize),
            start_time=start_time,
            end_time=end_time,
            metadata={"summarization_config": self.config.__dict__}
        )
        
        # Delete old messages (keep recent ones)
        deleted_count = await self.repository.delete_old_messages(
            session.id, self.config.summary_overlap
        )
        
        print(f"[ConversationMemoryManager] Summarization complete:")
        print(f"  - Archive ID: {archive.id}")
        print(f"  - Messages summarized: {len(messages_to_summarize)}")
        print(f"  - Messages deleted: {deleted_count}")
        print(f"  - Messages kept: {self.config.summary_overlap}")
    
    async def search_memory(self, user_id: str, character_name: str, query: str,
                          time_range: str = "all", max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """Search conversation memory
        
        Args:
            user_id: User identifier
            character_name: Character name
            query: Search query
            time_range: Time range filter
            max_results: Maximum results to return
            
        Returns:
            List[Dict[str, Any]]: Search results
        """
        if not self._initialized:
            await self.initialize()
        
        return await self.search_service.search_memory(
            user_id, character_name, query, time_range, max_results
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
        return [msg.to_dict() for msg in messages]
    
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
    
    async def cleanup(self):
        """Cleanup resources and pending tasks"""
        if self._cleanup_done:
            return
        
        self._cleanup_done = True
        print("[ConversationMemoryManager] Starting cleanup...")
        
        # Cancel all summarization tasks gracefully
        if self._summarization_tasks:
            print(f"[ConversationMemoryManager] Cancelling {len(self._summarization_tasks)} summarization tasks")
            
            # Cancel all tasks immediately without waiting
            for session_key, task in self._summarization_tasks.items():
                if not task.done():
                    task.cancel()
            
            # Clear the task dictionary immediately
            self._summarization_tasks.clear()
        
        # Close database connections with error suppression
        try:
            from .database import get_database_manager
            db_manager = get_database_manager()
            await db_manager.close()
        except Exception:
            # Silently ignore database cleanup errors during shutdown
            pass
        
        print("[ConversationMemoryManager] Cleanup complete")
    
    def is_initialized(self) -> bool:
        """Check if memory manager is initialized
        
        Returns:
            bool: True if initialized
        """
        return self._initialized