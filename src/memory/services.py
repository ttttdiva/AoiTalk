"""
Service layer for conversation memory management
"""

import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from .config import MemoryConfig
from .repository import ConversationRepository
from .summary_validation import validate_generated_summary
from .models import ConversationMessage, ConversationArchive
from .cross_session_memory import get_cross_session_memory
import os

# pgvector search removed - using Qdrant RAG for vector search instead


class SummarizationService:
    """Service for conversation summarization"""
    
    def __init__(self, config: MemoryConfig):
        self.config = config
        # Pass enable_search to repository to avoid loading embedding model when search is disabled
        self.repository = ConversationRepository(enable_search=config.enable_search)
    
    async def create_summary(
        self,
        messages: List[ConversationMessage],
        llm_client=None,
        previous_summary: Optional[str] = None,
    ) -> Optional[str]:
        """Create summary from conversation messages
        
        Args:
            messages: List of conversation messages
            llm_client: LLM client for generating summary
            
        Returns:
            Optional[str]: Generated summary or None if failed
        """
        if not messages:
            return None
        
        # Use progressive summarization for long conversations
        new_conversation_text = self._build_conversation_text(messages)
        conversation_text = new_conversation_text
        if previous_summary:
            conversation_text = (
                "既存の正本要約（必ず保持・統合すること）:\n"
                f"{previous_summary.strip()}\n\n"
                "今回追加する会話:\n"
                f"{new_conversation_text}"
            )
        if len(messages) > 10:
            summary = await self.create_progressive_summary(
                messages,
                llm_client,
                previous_summary=previous_summary,
            )
        else:
            # Generate summary using LLM
            summary = await self._generate_summary_with_llm(conversation_text, llm_client)

        validation = validate_generated_summary(
            summary,
            source_text=conversation_text,
            previous_summary=previous_summary or "",
        )
        if not validation.accepted:
            if summary:
                print(
                    "[SummarizationService] Summary candidate discarded: "
                    f"{validation.reason}"
                )
            # A lossy deterministic fallback would still delete the source
            # messages at checkpoint time.  Fail open by retaining the source.
            return None

        return str(summary or "").strip() or None
    
    async def create_progressive_summary(
        self,
        messages: List[ConversationMessage],
        llm_client=None,
        previous_summary: Optional[str] = None,
    ) -> Optional[str]:
        """Create progressive summary for long conversations
        
        Args:
            messages: List of conversation messages
            llm_client: LLM client for generating summary
            
        Returns:
            Optional[str]: Generated summary or None if failed
        """
        # 5件ずつ段階的に要約
        chunk_size = 5
        chunks = [messages[i:i+chunk_size] for i in range(0, len(messages), chunk_size)]
        
        summaries = []
        for i, chunk in enumerate(chunks):
            chunk_text = self._build_conversation_text(chunk)
            chunk_prompt = f"""以下の会話の一部（パート{i+1}/{len(chunks)}）を簡潔に要約してください。

会話:
{chunk_text}

要約:"""
            
            try:
                if hasattr(llm_client, 'generate_response_async'):
                    chunk_summary = await llm_client.generate_response_async(chunk_prompt)
                else:
                    # Use sync version in executor
                    loop = asyncio.get_event_loop()
                    chunk_summary = await loop.run_in_executor(
                        None, lambda: llm_client.generate_response(chunk_prompt, stream=False)
                    )
                
                if chunk_summary and chunk_summary.strip():
                    summaries.append(chunk_summary.strip())
                else:
                    return None
            except Exception as e:
                print(f"[SummarizationService] Chunk summarization error: {e}")
                return None

        # 最終統合要約
        if summaries:
            if previous_summary:
                summaries.insert(
                    0,
                    "既存の正本要約（保持必須）: " + previous_summary.strip(),
                )
            return await self._create_final_summary(summaries, llm_client)
        
        return None
    
    async def _create_final_summary(self, summaries: List[str], llm_client = None) -> Optional[str]:
        """Create final summary from chunk summaries
        
        Args:
            summaries: List of chunk summaries
            llm_client: LLM client
            
        Returns:
            Optional[str]: Final integrated summary
        """
        combined_text = "\n\n".join([f"パート{i+1}: {s}" for i, s in enumerate(summaries)])
        
        final_prompt = f"""以下の会話の要約パートを統合して、全体の会話を{self.config.summary_max_tokens}トークン以内で要約してください。

要約パート:
{combined_text}

統合要約:"""
        
        try:
            if hasattr(llm_client, 'generate_response_async'):
                final_summary = await llm_client.generate_response_async(final_prompt)
            else:
                # Use sync version in executor
                loop = asyncio.get_event_loop()
                final_summary = await loop.run_in_executor(
                    None, lambda: llm_client.generate_response(final_prompt, stream=False)
                )
            
            if final_summary and final_summary.strip():
                return final_summary.strip()
        except Exception as e:
            print(f"[SummarizationService] Final summarization error: {e}")
        
        # Fallback: concatenate chunk summaries
        return " / ".join(summaries)
    
    def _build_conversation_text(self, messages: List[ConversationMessage]) -> str:
        """Build formatted conversation text from messages
        
        Args:
            messages: List of messages
            
        Returns:
            str: Formatted conversation text
        """
        lines = []
        for msg in messages:
            role_name = "ユーザー" if msg.role == "user" else "アシスタント"
            lines.append(f"{role_name}: {msg.content}")
        
        return "\n".join(lines)
    
    async def _generate_summary_with_llm(self, conversation_text: str, 
                                       llm_client = None) -> Optional[str]:
        """Generate summary using LLM client
        
        Args:
            conversation_text: Formatted conversation text
            llm_client: LLM client
            
        Returns:
            Optional[str]: Generated summary
        """
        if not llm_client:
            return None
        
        prompt = f"""以下の会話を簡潔に要約してください。重要な情報や話題を含めて、{self.config.summary_max_tokens}トークン以内で要約してください。

会話:
{conversation_text}

要約:"""
        
        try:
            # Try multiple times if summarization fails
            for attempt in range(self.config.max_summary_retries):
                try:
                    if hasattr(llm_client, 'generate_response_async'):
                        summary = await llm_client.generate_response_async(prompt)
                    else:
                        # Use sync version in executor
                        loop = asyncio.get_event_loop()
                        summary = await loop.run_in_executor(
                            None, lambda: llm_client.generate_response(prompt, stream=False)
                        )
                    
                    if summary and summary.strip():
                        return summary.strip()
                    
                except Exception as e:
                    print(f"[SummarizationService] Attempt {attempt + 1} failed: {e}")
                    if attempt == self.config.max_summary_retries - 1:
                        break
                    await asyncio.sleep(1)  # Wait before retry
            
            return None
            
        except Exception as e:
            print(f"[SummarizationService] Summary generation failed: {e}")
            return None
    
    def _create_fallback_summary(self, messages: List[ConversationMessage]) -> str:
        """Create fallback summary when LLM summarization fails
        
        Args:
            messages: List of messages
            
        Returns:
            str: Fallback summary
        """
        if not messages:
            return "空の会話"
        
        # Simple fallback: combine first and last messages
        first_msg = messages[0]
        last_msg = messages[-1]
        
        summary_parts = []
        
        if first_msg.role == "user":
            summary_parts.append(f"ユーザーの質問: {first_msg.content[:100]}")
        
        if last_msg != first_msg and last_msg.role == "assistant":
            summary_parts.append(f"最終回答: {last_msg.content[:100]}")
        
        summary = " / ".join(summary_parts)
        return summary if summary else f"{len(messages)}件のメッセージ"


class MemorySearchService:
    """Service for searching conversation memory"""
    
    def __init__(self, config: MemoryConfig):
        self.config = config
        # Pass enable_search to repository to avoid loading embedding model when search is disabled
        self.repository = ConversationRepository(enable_search=config.enable_search)
        

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
            time_range: Time range filter ("recent", "this_week", "this_month", "all")
            max_results: Maximum results to return
            project_id: Project scope. None searches only non-project conversations.
            
        Returns:
            List[Dict[str, Any]]: Search results with relevance scores
        """
        if not query or not query.strip():
            return []
        
        # Check if search is enabled
        if not hasattr(self.config, 'enable_search') or not self.config.enable_search:
            print("[MemorySearchService] Memory search is disabled")
            return []

        max_results = max_results or self.config.max_search_results

        try:
            cross_session_memory = get_cross_session_memory({
                "min_relevance_score": self.config.similarity_threshold,
                "max_results": max_results,
            })
            results = await asyncio.wait_for(
                cross_session_memory.search_relevant_conversations(
                    user_id=user_id,
                    query=query,
                    limit=max_results,
                    character_name=character_name,
                    project_id=project_id,
                ),
                timeout=self.config.search_timeout,
            )
            return [
                {
                    "type": "active_message",
                    "content": result.get("content", ""),
                    "role": result.get("role"),
                    "relevance_score": result.get("relevance_score", 0.0),
                    "timestamp": result.get("timestamp"),
                    "session_id": result.get("session_id"),
                    "character_name": result.get("character_name") or character_name,
                }
                for result in results
            ]

        except Exception as e:
            print(f"[MemorySearchService] Search failed: {e}")
            return []
    
    def _filter_by_user_character(self, message, user_id: str, character_name: str) -> bool:
        """Filter message by user and character"""
        # This would need session lookup - for now return True
        # TODO: Implement proper filtering based on session
        return True
    
    async def _search_active_messages(self, user_id: str, character_name: str,
                                    query: str, query_embedding: List[float]) -> List[Dict[str, Any]]:
        """Search active conversation messages
        
        Args:
            user_id: User identifier
            character_name: Character name
            query: Search query
            query_embedding: Query embedding vector
            
        Returns:
            List[Dict[str, Any]]: Active message search results
        """
        return []
    
    async def _search_archives(self, user_id: str, character_name: str,
                             query_embedding: List[float]) -> List[Dict[str, Any]]:
        """Search archived conversation summaries
        
        Args:
            user_id: User identifier
            character_name: Character name
            query_embedding: Query embedding vector
            
        Returns:
            List[Dict[str, Any]]: Archive search results
        """
        archive_results = await self.repository.search_archives(
            user_id, character_name, query_embedding, 
            self.config.similarity_threshold, self.config.max_search_results
        )
        
        results = []
        for archive, similarity in archive_results:
            results.append({
                'type': 'archived_summary',
                'content': archive.summary,
                'timestamp': archive.end_time.isoformat() if archive.end_time else None,
                'relevance_score': similarity,
                'message_count': archive.message_count,
                'metadata': archive.message_metadata
            })
        
        return results


class ConversationHistoryService:
    """Service for managing complete conversation history"""
    
    def __init__(self, config: MemoryConfig):
        self.config = config
        # Pass enable_search to repository to avoid loading embedding model when search is disabled
        self.repository = ConversationRepository(enable_search=config.enable_search)
    
    async def log_message(self, user_id: str, session_id: str, character_name: str,
                         role: str, content: str, metadata: Optional[Dict[str, Any]] = None,
                         function_call_data: Optional[Dict[str, Any]] = None):
        """Log message to conversation history
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            character_name: Character name
            role: Message role
            content: Message content
            metadata: Optional metadata
            function_call_data: Optional function call data
        """
        if not self.config.enable_history_logging:
            return
        
        try:
            await self.repository.add_to_history(
                user_id=user_id,
                session_id=session_id,
                character_name=character_name,
                role=role,
                content=content,
                metadata=metadata,
                function_call_data=function_call_data
            )
        except Exception as e:
            print(f"[ConversationHistoryService] Failed to log message: {e}")
    
    async def cleanup_old_history(self) -> int:
        """Clean up old conversation history
        
        Returns:
            int: Number of records deleted
        """
        try:
            return await self.repository.cleanup_old_history(self.config.history_retention_days)
        except Exception as e:
            print(f"[ConversationHistoryService] Failed to cleanup history: {e}")
            return 0
