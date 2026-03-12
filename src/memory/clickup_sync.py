"""
ClickUp task synchronization service for context awareness
"""

import asyncio
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import aiohttp
from sqlalchemy.orm import Session
from sqlalchemy import select, delete
from sqlalchemy.exc import OperationalError

from .database import DatabaseManager
from .models import ClickUpTask

logger = logging.getLogger(__name__)


class ClickUpSyncService:
    """Service for synchronizing ClickUp tasks to local database"""
    
    def __init__(self, db_manager: DatabaseManager, api_key: str, team_id: str, sync_interval_minutes: int = 15):
        """Initialize ClickUp sync service
        
        Args:
            db_manager: Database manager instance
            api_key: ClickUp API key
            team_id: ClickUp team ID
            sync_interval_minutes: Sync interval in minutes (default: 15)
        """
        self.db_manager = db_manager
        self.api_key = api_key
        self.team_id = team_id
        self.base_url = "https://api.clickup.com/api/v2"
        self.headers = {
            "Authorization": api_key,
            "Content-Type": "application/json"
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_sync: Optional[datetime] = None
        self._sync_interval = timedelta(minutes=sync_interval_minutes)
        
        # Get target folder IDs from environment
        target_folders = os.getenv("CLICKUP_TARGET_FOLDER_IDS", "")
        self.target_folder_ids = [f.strip() for f in target_folders.split(",") if f.strip()]
        
        if self.target_folder_ids:
            logger.info(f"ClickUp sync will be limited to folders: {self.target_folder_ids}")
        
    async def __aenter__(self):
        """Async context manager entry"""
        self._session = aiohttp.ClientSession(headers=self.headers)
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self._session:
            await self._session.close()
            
    async def get_weekly_tasks(self) -> List[Dict[str, Any]]:
        """Get tasks from the last week based on start/due dates
        
        Returns:
            List of task dictionaries
        """
        if not self._session:
            self._session = aiohttp.ClientSession(headers=self.headers)
            
        try:
            # Calculate date range (1 week before and after today)
            now = datetime.now(timezone.utc)
            week_ago = now - timedelta(days=7)
            week_later = now + timedelta(days=7)
            
            # Convert to milliseconds timestamp for ClickUp API
            date_from = int(week_ago.timestamp() * 1000)
            date_to = int(week_later.timestamp() * 1000)
            
            # Get all lists in the team
            lists = await self._get_all_lists()
            
            # Count API calls from _get_all_lists
            if self.target_folder_ids:
                api_call_count = len(self.target_folder_ids) * 2  # folder details + lists per folder
            else:
                # Complex calculation for all lists (estimate)
                api_call_count = 10  # Rough estimate for spaces + folders
            
            logger.info(f"Initial API calls for list retrieval: ~{api_call_count}")
            all_tasks = []
            
            logger.info(f"Fetching tasks from {len(lists)} lists...")
            
            for list_info in lists:
                list_id = list_info['id']
                list_name = list_info['name']
                folder_name = list_info.get('folder', {}).get('name', '')
                space_name = list_info.get('space', {}).get('name', '')
                
                # Get tasks from this list with date filter
                params = {
                    'include_closed': 'true',
                    'date_created_gt': date_from,
                    'date_created_lt': date_to,
                    'subtasks': 'false',
                    'page': 0
                }
                
                while True:
                    url = f"{self.base_url}/list/{list_id}/task"
                    async with self._session.get(url, params=params) as response:
                        api_call_count += 1
                        
                        if response.status == 200:
                            data = await response.json()
                            tasks = data.get('tasks', [])
                            
                            # Add list/folder/space info to each task
                            for task in tasks:
                                task['list_name'] = list_name
                                task['folder_name'] = folder_name
                                task['space_name'] = space_name
                                
                                # Check if task is within our date range
                                if self._is_task_in_date_range(task, week_ago, week_later):
                                    all_tasks.append(task)
                            
                            # Check if there are more pages
                            if len(tasks) < 100:  # ClickUp returns max 100 per page
                                break
                            params['page'] += 1
                        else:
                            logger.warning(f"Failed to get tasks from list {list_id}: {response.status}")
                            break
            
            logger.info(f"Completed task fetch. API calls: {api_call_count}, Tasks found: {len(all_tasks)}")
            return all_tasks
            
        except Exception as e:
            logger.error(f"Error getting weekly tasks: {e}")
            return []
            
    def _is_task_in_date_range(self, task: Dict[str, Any], start: datetime, end: datetime) -> bool:
        """Check if task falls within date range based on start_date or due_date
        
        Args:
            task: Task dictionary from ClickUp API
            start: Start of date range
            end: End of date range
            
        Returns:
            True if task is within range
        """
        # Check start_date
        if task.get('start_date'):
            start_date = datetime.fromtimestamp(int(task['start_date']) / 1000, tz=timezone.utc)
            if start <= start_date <= end:
                return True
                
        # Check due_date
        if task.get('due_date'):
            due_date = datetime.fromtimestamp(int(task['due_date']) / 1000, tz=timezone.utc)
            if start <= due_date <= end:
                return True
                
        # Check date_created (already filtered by API, but double-check)
        if task.get('date_created'):
            created_date = datetime.fromtimestamp(int(task['date_created']) / 1000, tz=timezone.utc)
            if start <= created_date <= end:
                return True
                
        return False
        
    async def _get_all_lists(self) -> List[Dict[str, Any]]:
        """Get all lists in the team (or specific folders if configured)
        
        Returns:
            List of list information dictionaries
        """
        lists = []
        
        try:
            # If target folders are specified, get lists from those folders only
            if self.target_folder_ids:
                for folder_id in self.target_folder_ids:
                    # Get folder details
                    folder_url = f"{self.base_url}/folder/{folder_id}"
                    async with self._session.get(folder_url) as folder_response:
                        if folder_response.status == 200:
                            folder_data = await folder_response.json()
                            folder_name = folder_data.get('name', 'Unknown')
                            space_info = folder_data.get('space', {})
                            
                            # Get lists in this folder
                            lists_url = f"{self.base_url}/folder/{folder_id}/list"
                            async with self._session.get(lists_url) as lists_response:
                                if lists_response.status == 200:
                                    lists_data = await lists_response.json()
                                    folder_lists = lists_data.get('lists', [])
                                    
                                    logger.info(f"Found {len(folder_lists)} lists in folder '{folder_name}' (ID: {folder_id})")
                                    
                                    for list_item in folder_lists:
                                        list_item['folder'] = {'id': folder_id, 'name': folder_name}
                                        list_item['space'] = space_info
                                        lists.append(list_item)
                                else:
                                    logger.warning(f"Failed to get lists from folder {folder_id}: {lists_response.status}")
                        else:
                            logger.warning(f"Failed to get folder details for {folder_id}: {folder_response.status}")
                
                logger.info(f"Total lists found in target folders: {len(lists)}")
                
            else:
                # Original behavior: get all lists in the team
                url = f"{self.base_url}/team/{self.team_id}/space"
                async with self._session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        spaces = data.get('spaces', [])
                        
                        for space in spaces:
                            space_id = space['id']
                            space_name = space['name']
                            
                            # Get folders in space
                            folder_url = f"{self.base_url}/space/{space_id}/folder"
                            async with self._session.get(folder_url) as folder_response:
                                if folder_response.status == 200:
                                    folder_data = await folder_response.json()
                                    folders = folder_data.get('folders', [])
                                    
                                    # Add folderless lists
                                    for list_item in space.get('lists', []):
                                        list_item['space'] = {'id': space_id, 'name': space_name}
                                        lists.append(list_item)
                                    
                                    # Add lists from folders
                                    for folder in folders:
                                        folder_id = folder['id']
                                        folder_name = folder['name']
                                        
                                        for list_item in folder.get('lists', []):
                                            list_item['folder'] = {'id': folder_id, 'name': folder_name}
                                            list_item['space'] = {'id': space_id, 'name': space_name}
                                            lists.append(list_item)
                                            
        except Exception as e:
            logger.error(f"Error getting lists: {e}")
            
        return lists
        
    async def sync_tasks(self) -> Dict[str, Any]:
        """Sync tasks from ClickUp to local database
        
        Returns:
            Sync statistics
        """
        logger.info("Starting ClickUp task sync...")
        
        # Check if database is initialized
        if not self.db_manager.is_initialized():
            logger.warning("Database not initialized, skipping sync")
            return {'error': 'Database not initialized'}
        
        try:
            # Get tasks from ClickUp
            tasks = await self.get_weekly_tasks()
            logger.info(f"Retrieved {len(tasks)} tasks from ClickUp")
            
            # Convert and store tasks
            with self.db_manager.get_sync_session() as session:
                # Clear existing tasks (simple approach for now)
                session.execute(delete(ClickUpTask))
                
                # Add new tasks
                added_count = 0
                for task_data in tasks:
                    task = self._convert_to_db_model(task_data)
                    if task:
                        session.add(task)
                        added_count += 1
                        
                session.commit()
                
            self._last_sync = datetime.now(timezone.utc)
            
            stats = {
                'total_retrieved': len(tasks),
                'added': added_count,
                'last_sync': self._last_sync.isoformat()
            }
            
            logger.info(f"ClickUp sync completed: {stats}")
            return stats
            
        except OperationalError as e:
            # Log as warning for connection errors to avoid spam
            logger.warning(f"Database connection error during sync: {e}")
            return {'error': f"Database connection failed: {e}"}
        except Exception as e:
            logger.error(f"Error during sync: {e}")
            return {'error': str(e)}
            
    def _convert_to_db_model(self, task_data: Dict[str, Any]) -> Optional[ClickUpTask]:
        """Convert ClickUp API response to database model
        
        Args:
            task_data: Task data from ClickUp API
            
        Returns:
            ClickUpTask model instance or None
        """
        try:
            # Extract assignee information
            assignees = task_data.get('assignees', [])
            assignee_ids = [a.get('id') for a in assignees if a.get('id')]
            assignee_names = [a.get('username', '') for a in assignees]
            
            # Extract creator info
            creator = task_data.get('creator', {})
            
            # Convert timestamps
            def parse_timestamp(ts):
                if ts:
                    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                return None
                
            # Extract priority
            priority_obj = task_data.get('priority')
            priority = None
            if priority_obj:
                priority_map = {
                    1: 'urgent',
                    2: 'high', 
                    3: 'normal',
                    4: 'low'
                }
                priority = priority_map.get(priority_obj.get('id'), 'normal')
                
            task = ClickUpTask(
                id=task_data['id'],
                name=task_data['name'],
                description=task_data.get('description', ''),
                status=task_data.get('status', {}).get('status', 'unknown'),
                priority=priority,
                
                # Dates
                start_date=parse_timestamp(task_data.get('start_date')),
                due_date=parse_timestamp(task_data.get('due_date')),
                date_created=parse_timestamp(task_data.get('date_created')),
                date_updated=parse_timestamp(task_data.get('date_updated')),
                date_closed=parse_timestamp(task_data.get('date_closed')),
                
                # User info
                creator_id=creator.get('id'),
                creator_name=creator.get('username'),
                assignee_ids=assignee_ids,
                assignee_names=assignee_names,
                
                # Organization
                list_id=task_data.get('list', {}).get('id'),
                list_name=task_data.get('list_name', ''),
                folder_id=task_data.get('folder', {}).get('id'),
                folder_name=task_data.get('folder_name', ''),
                space_id=task_data.get('space', {}).get('id'),
                space_name=task_data.get('space_name', ''),
                
                # Details
                tags=[tag.get('name') for tag in task_data.get('tags', [])],
                time_estimate=task_data.get('time_estimate'),
                time_spent=task_data.get('time_spent'),
                
                # Custom fields and metadata
                custom_fields={cf.get('id'): cf.get('value') for cf in task_data.get('custom_fields', [])},
                task_metadata={'url': task_data.get('url')}
            )
            
            return task
            
        except Exception as e:
            logger.error(f"Error converting task {task_data.get('id')}: {e}")
            return None
            
    async def get_summary_from_db(self) -> str:
        """Get a summary of tasks from the database
        
        Returns:
            Human-readable summary of tasks
        """
        with self.db_manager.get_sync_session() as session:
            # Get all tasks
            tasks = session.execute(select(ClickUpTask)).scalars().all()
            
            if not tasks:
                return "ClickUpタスクがまだ同期されていません。"
                
            # Group by status
            status_groups = {}
            for task in tasks:
                status = task.status
                if status not in status_groups:
                    status_groups[status] = []
                status_groups[status].append(task)
                
            # Build summary
            lines = [f"ClickUpタスク概要 (合計: {len(tasks)}件)"]
            
            # Add tasks by status
            status_order = ['in progress', 'to do', 'open', 'review', 'done', 'closed']
            for status in status_order:
                if status in status_groups:
                    lines.append(f"\n{status.upper()} ({len(status_groups[status])}件):")
                    for task in status_groups[status][:5]:  # Show max 5 per status
                        due_str = ""
                        if task.due_date:
                            due_str = f" (期限: {task.due_date.strftime('%m/%d')})"
                        lines.append(f"- {task.name}{due_str}")
                    if len(status_groups[status]) > 5:
                        lines.append(f"  ... 他 {len(status_groups[status]) - 5}件")
                        
            # Add remaining statuses
            for status, tasks_list in status_groups.items():
                if status not in status_order:
                    lines.append(f"\n{status.upper()} ({len(tasks_list)}件)")
                    
            # Add sync info
            if tasks:
                last_sync = max(task.last_synced for task in tasks)
                lines.append(f"\n最終同期: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}")
                
            return "\n".join(lines)
            
    async def start_background_sync(self):
        """Start background synchronization task"""
        while True:
            try:
                # Check if sync is needed
                if (self._last_sync is None or 
                    datetime.now(timezone.utc) - self._last_sync > self._sync_interval):
                    await self.sync_tasks()
                    
            except Exception as e:
                logger.error(f"Background sync error: {e}")
                
            # Wait before next check
            await asyncio.sleep(60)  # Check every minute


async def get_clickup_sync_service(db_manager: DatabaseManager, sync_interval_minutes: int = 15) -> Optional[ClickUpSyncService]:
    """Factory function to create ClickUp sync service
    
    Args:
        db_manager: Database manager instance
        sync_interval_minutes: Sync interval in minutes (default: 15)
        
    Returns:
        ClickUpSyncService instance or None if not configured
    """
    api_key = os.getenv('CLICKUP_API_KEY')
    team_id = os.getenv('CLICKUP_TEAM_ID')
    
    if not api_key or not team_id:
        logger.warning("ClickUp API key or team ID not configured")
        return None
        
    return ClickUpSyncService(db_manager, api_key, team_id, sync_interval_minutes)