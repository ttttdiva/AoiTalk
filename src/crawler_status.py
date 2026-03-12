#!/usr/bin/env python3
"""
Crawler Status Checker Module

Retrieves status from external crawlers:
- DiscordCrawler (local)
- EventMonitor (local)
- VideoCrawler (HuggingFace Space)
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)

# Crawler paths configuration
DISCORD_CRAWLER_PATH = Path(os.environ["DISCORD_CRAWLER_PATH"]) if os.environ.get("DISCORD_CRAWLER_PATH") else None
EVENT_MONITOR_PATH = Path(os.environ["EVENT_MONITOR_PATH"]) if os.environ.get("EVENT_MONITOR_PATH") else None
HYDRUS_EXECUTABLE = Path(os.environ["HYDRUS_EXECUTABLE"]) if os.environ.get("HYDRUS_EXECUTABLE") else None
VIDEO_CRAWLER_URL = "https://topgunm-as-partionpeek.hf.space"


class CrawlerStatusChecker:
    """Checks status of external crawlers"""
    
    def __init__(self):
        self.timeout = 10.0  # seconds

    def _is_process_running(self, script_name: str, target_cwd: Optional[Path]) -> bool:
        """Check if a python script is running in specific directory"""
        if not psutil or not target_cwd:
            return False
            
        try:
            target_cwd_str = str(target_cwd).lower()
            target_path_part = target_cwd.name.lower()
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    if proc.info['cmdline']:
                        cmdline = [arg.lower() for arg in proc.info['cmdline']]
                        cmd_str = " ".join(cmdline)
                        
                        if 'python' in proc.info['name'].lower() and \
                           script_name.lower() in cmd_str:
                            
                            # Verify directory
                            if proc.info['cwd'] and target_cwd_str in proc.info['cwd'].lower():
                                return True
                            
                            # Backup check
                            if target_path_part in cmd_str:
                                return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as e:
            logger.warn(f"Failed to check processes: {e}")
            
        return False
    
    async def check_alive(self, crawler_name: str) -> bool:
        """Check if a crawler process is alive (health check only, no detailed status)"""
        if crawler_name == "DiscordCrawler":
            return self._is_process_running("main.py", DISCORD_CRAWLER_PATH)
        elif crawler_name == "EventMonitor":
            return self._is_process_running("main.py", EVENT_MONITOR_PATH)
        elif crawler_name == "HydrusClient":
            # Hydrus Client API health check
            try:
                hydrus_url = os.environ.get('HYDRUS_API_URL', 'http://127.0.0.1:45869')
                hydrus_key = os.environ.get('HYDRUS_ACCESS_KEY')
                if not hydrus_key:
                    return False
                
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(
                        f"{hydrus_url}/api_version",
                        headers={"Hydrus-Client-API-Access-Key": hydrus_key}
                    )
                    return response.status_code == 200
            except:
                return False
        elif crawler_name == "VideoCrawler":
            # HuggingFace Space health check
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    runtime_url = "https://huggingface.co/api/spaces/TopgunM/as-partionpeek/runtime"
                    response = await client.get(runtime_url)
                    if response.status_code == 200:
                        stage = response.json().get("stage", "")
                        return stage == "RUNNING"
            except:
                return False
        return False

    async def get_video_crawler_detailed_status(self) -> Dict[str, Any]:
        """Get detailed VideoCrawler status including sleeping, paused, building states"""
        result = {
            "status": "unknown",
            "details": {},
            "error": None,
            "can_restart": False,
            "is_alive": False
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                runtime_url = "https://huggingface.co/api/spaces/TopgunM/as-partionpeek/runtime"
                response = await client.get(runtime_url)
                
                if response.status_code == 200:
                    runtime_data = response.json()
                    stage = runtime_data.get("stage", "UNKNOWN")
                    
                    # Map stage to status
                    if stage == "RUNNING":
                        result["status"] = "running"
                        result["is_alive"] = True
                    elif stage == "BUILDING":
                        result["status"] = "building"
                        result["is_alive"] = False
                    elif stage == "PAUSED":
                        result["status"] = "paused"
                        result["can_restart"] = True
                        result["is_alive"] = False
                    elif stage == "SLEEPING":
                        result["status"] = "sleeping"
                        result["can_restart"] = True
                        result["is_alive"] = False
                    elif stage == "STOPPED":
                        result["status"] = "stopped"
                        result["can_restart"] = True
                        result["is_alive"] = False
                    else:
                        result["status"] = stage.lower()
                        result["is_alive"] = False
                    
                    # Add details
                    result["details"] = {"stage": stage}
                    hardware = runtime_data.get("hardware", {})
                    current_hw = hardware.get("current")
                    requested_hw = hardware.get("requested")
                    result["details"]["hardware"] = current_hw or requested_hw or "unknown"
                    result["details"]["domain"] = VIDEO_CRAWLER_URL
                else:
                    result["status"] = "unreachable"
                    result["error"] = f"HuggingFace API returned {response.status_code}"
                    
        except httpx.TimeoutException:
            result["status"] = "timeout"
            result["error"] = "Request timed out"
        except httpx.RequestError as e:
            result["status"] = "unreachable"
            result["error"] = f"Network error: {e}"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"Failed to get VideoCrawler status: {e}")
        
        return result

    async def get_hydrus_client_detailed_status(self) -> Dict[str, Any]:
        """Get detailed HydrusClient status including API availability"""
        result = {
            "status": "unknown",
            "details": {},
            "error": None,
            "can_restart": False,
            "is_alive": False
        }
        
        try:
            hydrus_url = os.environ.get('HYDRUS_API_URL', 'http://127.0.0.1:45869')
            hydrus_key = os.environ.get('HYDRUS_ACCESS_KEY')
            
            if not hydrus_key:
                result["status"] = "not_configured"
                result["error"] = "HYDRUS_ACCESS_KEY not set"
                result["can_restart"] = True
                return result
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Try to get API version to check if service is running
                response = await client.get(
                    f"{hydrus_url}/api_version",
                    headers={"Hydrus-Client-API-Access-Key": hydrus_key}
                )
                
                if response.status_code == 200:
                    api_data = response.json()
                    result["status"] = "running"
                    result["is_alive"] = True
                    result["details"] = {
                        "api_version": api_data.get("version", "unknown"),
                        "hydrus_version": api_data.get("hydrus_version", "unknown"),
                        "api_url": hydrus_url
                    }
                elif response.status_code == 401 or response.status_code == 403:
                    result["status"] = "auth_error"
                    result["error"] = "Invalid API key"
                    result["is_alive"] = False
                else:
                    result["status"] = "error"
                    result["error"] = f"API returned status {response.status_code}"
                    result["can_restart"] = True
                    result["is_alive"] = False
                    
        except httpx.TimeoutException:
            result["status"] = "timeout"
            result["error"] = "Request timed out"
            result["can_restart"] = True
        except httpx.RequestError as e:
            result["status"] = "stopped"
            result["error"] = f"Connection failed: {e}"
            result["can_restart"] = True
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["can_restart"] = True
            logger.error(f"Failed to get HydrusClient status: {e}")
        
        return result

    

    def _get_next_run(self, jobs: list) -> Optional[str]:
        """Extract next run time from jobs list"""
        if not jobs:
            return None
        next_runs = [j.get("next_run") for j in jobs if j.get("next_run")]
        if next_runs:
            return min(next_runs)
        return None
    


    async def restart_video_crawler(self) -> Dict[str, Any]:
        """Restart the VideoCrawler HuggingFace Space using browser automation"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }
        
        # Get HuggingFace token from environment (optional for browser but good to have)
        hf_token = os.environ.get('HUGGINGFACE_API_KEY') or os.environ.get('HF_TOKEN')
        
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            result["error"] = "Playwright not installed"
            result["message"] = "ブラウザ自動化用のライブラリが見つかりません"
            return result
        
        space_url = "https://huggingface.co/spaces/TopgunM/as-partionpeek"
        
        try:
            async with async_playwright() as p:
                # Launch headless browser
                browser = await p.chromium.launch(headless=True)
                
                # Create context with English locale to ensure text matches "Restart this Space"
                context = await browser.new_context(
                    locale='en-US',
                    extra_http_headers={
                        "Authorization": f"Bearer {hf_token}"
                    } if hf_token else {}
                )
                page = await context.new_page()
                
                logger.info(f"Opening HuggingFace Space page: {space_url}")
                
                # Navigate to Space page
                await page.goto(space_url, wait_until="domcontentloaded", timeout=60000)
                
                # Wait for potential iframes and dynamic content loading
                await page.wait_for_timeout(5000)
                
                restart_button = None
                
                # Strategy 1: Look in the main page for specific text "Restart this Space"
                # The user's screenshot shows a button with exactly this text
                selectors = [
                    'button:has-text("Restart this Space")',
                    'a:has-text("Restart this Space")', # Sometimes buttons are links
                    'div:has-text("Restart this Space")[role="button"]',
                    'button:has-text("Restart")',
                ]
                
                logger.info("Searching for 'Restart this Space' button...")
                
                # Check main page first
                for selector in selectors:
                    locator = page.locator(selector).first
                    if await locator.count() > 0 and await locator.is_visible():
                        restart_button = locator
                        logger.info(f"Found button in main page with selector: {selector}")
                        break
                
                # Strategy 2: Check inside all iframes
                if not restart_button:
                    logger.info("Checking iframes...")
                    for frame in page.frames:
                        try:
                            # Skip detached frames
                            if frame.is_detached:
                                continue
                                
                            for selector in selectors:
                                locator = frame.locator(selector).first
                                if await locator.count() > 0 and await locator.is_visible():
                                    restart_button = locator
                                    logger.info(f"Found button in iframe ({frame.url}) with selector: {selector}")
                                    break
                            if restart_button:
                                break
                        except Exception as e:
                            logger.warn(f"Error checking frame: {e}")
                            continue

                if not restart_button:
                    await browser.close()
                    # Fallback to manual URL opening
                    result["error"] = "Restart button not found"
                    result["message"] = "自動再起動ボタンが見つかりませんでした。開いたタブで再起動してください"
                    result["url"] = space_url
                    return result
                
                # Click the restart button
                logger.info("Clicking restart button...")
                await restart_button.click()
                
                # Wait for reaction (page reload or confirmation)
                await page.wait_for_timeout(3000)
                
                # Check for any confirmation dialogs that might appear
                try:
                    confirm_btn = page.locator('button:has-text("Confirm")').or_(page.locator('button:has-text("Yes")'))
                    if await confirm_btn.count() > 0 and await confirm_btn.is_visible():
                        await confirm_btn.first.click()
                        logger.info("Clicked confirmation")
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass
                
                await browser.close()
                
                result["success"] = True
                result["message"] = "VideoCrawlerを再起動しました。起動まで1-2分お待ちください。"
                logger.info("VideoCrawler Space restart initiated via browser automation")
                    
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"ブラウザ操作エラー: {e}"
            # Fallback URL on error
            result["url"] = space_url
            logger.error(f"Failed to restart VideoCrawler via browser: {e}")
        
        return result

    

    async def restart_discord_crawler(self) -> Dict[str, Any]:
        """Restart DiscordCrawler (stop if running, then start)"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }

        if not DISCORD_CRAWLER_PATH:
            result["error"] = "DISCORD_CRAWLER_PATH is not configured"
            result["message"] = "DiscordCrawler のパスが設定されていません"
            return result
        
        try:
            # Check if running
            is_running = self._is_process_running("main.py", DISCORD_CRAWLER_PATH)
            
            if is_running:
                # Terminate existing process
                logger.info("Stopping DiscordCrawler...")
                for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                    try:
                        if proc.info['cmdline'] and 'main.py' in " ".join(proc.info['cmdline']).lower():
                            if proc.info['cwd'] and str(DISCORD_CRAWLER_PATH).lower() in proc.info['cwd'].lower():
                                proc.terminate()
                                try:
                                    proc.wait(timeout=5)
                                except psutil.TimeoutExpired:
                                    proc.kill()
                                logger.info(f"Terminated DiscordCrawler process (PID: {proc.info['pid']})")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                
                # Wait a bit for cleanup
                await asyncio.sleep(1)
            
            # Start new process
            venv_python = DISCORD_CRAWLER_PATH / "venv" / "Scripts" / "python.exe"
            if not venv_python.exists():
                raise FileNotFoundError(f"Python venv not found: {venv_python}")
            
            logger.info("Starting DiscordCrawler (run-once mode)...")
            subprocess.Popen(
                [str(venv_python), "main.py", "run-once"],
                cwd=str(DISCORD_CRAWLER_PATH),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            result["success"] = True
            result["message"] = "DiscordCrawler restarted successfully" if is_running else "DiscordCrawler started successfully"
            
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to restart DiscordCrawler: {e}"
            logger.error(f"Failed to restart DiscordCrawler: {e}")
        
        return result
    
    async def restart_event_monitor(self) -> Dict[str, Any]:
        """Restart EventMonitor (stop if running, then start)"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }

        if not EVENT_MONITOR_PATH:
            result["error"] = "EVENT_MONITOR_PATH is not configured"
            result["message"] = "EventMonitor のパスが設定されていません"
            return result
        
        try:
            # Check if running
            is_running = self._is_process_running("main.py", EVENT_MONITOR_PATH)
            
            if is_running:
                # Terminate existing process
                logger.info("Stopping EventMonitor...")
                for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                    try:
                        if proc.info['cmdline'] and 'main.py' in " ".join(proc.info['cmdline']).lower():
                            if proc.info['cwd'] and str(EVENT_MONITOR_PATH).lower() in proc.info['cwd'].lower():
                                proc.terminate()
                                try:
                                    proc.wait(timeout=5)
                                except psutil.TimeoutExpired:
                                    proc.kill()
                                logger.info(f"Terminated EventMonitor process (PID: {proc.info['pid']})")
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                
                # Wait a bit for cleanup
                await asyncio.sleep(1)
            
            # Start new process
            venv_python = EVENT_MONITOR_PATH / "venv" / "Scripts" / "python.exe"
            if not venv_python.exists():
                raise FileNotFoundError(f"Python venv not found: {venv_python}")
            
            logger.info("Starting EventMonitor...")
            subprocess.Popen(
                [str(venv_python), "main.py"],
                cwd=str(EVENT_MONITOR_PATH),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            result["success"] = True
            result["message"] = "EventMonitor restarted successfully" if is_running else "EventMonitor started successfully"
            
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to restart EventMonitor: {e}"
            logger.error(f"Failed to restart EventMonitor: {e}")
        
        return result
    
    async def launch_hydrus_client(self) -> Dict[str, Any]:
        """Launch HydrusClient (only if not already running)"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }
        
        try:
            # Check if already running
            if psutil:
                for proc in psutil.process_iter(['name', 'exe']):
                    try:
                        if proc.info['name'] and 'hydrus' in proc.info['name'].lower():
                            result["success"] = False
                            result["message"] = "HydrusClient is already running"
                            return result
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            
            # Launch HydrusClient
            hydrus_exe = HYDRUS_EXECUTABLE
            if not hydrus_exe:
                raise FileNotFoundError("HYDRUS_EXECUTABLE is not configured")
            if not hydrus_exe.exists():
                raise FileNotFoundError(f"HydrusClient not found: {hydrus_exe}")
            
            logger.info("Launching HydrusClient...")
            subprocess.Popen(
                [str(hydrus_exe)],
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
            )
            
            result["success"] = True
            result["message"] = "HydrusClient launched successfully"
            
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to launch HydrusClient: {e}"
            logger.error(f"Failed to launch HydrusClient: {e}")
        
        return result
    
    async def stop_discord_crawler(self) -> Dict[str, Any]:
        """Stop DiscordCrawler if running"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }

        if not DISCORD_CRAWLER_PATH:
            result["error"] = "DISCORD_CRAWLER_PATH is not configured"
            result["message"] = "DiscordCrawler のパスが設定されていません"
            return result
        
        try:
            # Check if running
            is_running = self._is_process_running("main.py", DISCORD_CRAWLER_PATH)
            
            if not is_running:
                result["success"] = False
                result["message"] = "DiscordCrawler is not running"
                return result
            
            # Terminate process
            logger.info("Stopping DiscordCrawler...")
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    if proc.info['cmdline'] and 'main.py' in " ".join(proc.info['cmdline']).lower():
                        if proc.info['cwd'] and str(DISCORD_CRAWLER_PATH).lower() in proc.info['cwd'].lower():
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except psutil.TimeoutExpired:
                                proc.kill()
                            logger.info(f"Stopped DiscordCrawler process (PID: {proc.info['pid']})")
                            result["success"] = True
                            result["message"] = "DiscordCrawler stopped successfully"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            if not result["success"]:
                result["message"] = "Failed to stop DiscordCrawler (process not found)"
                
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to stop DiscordCrawler: {e}"
            logger.error(f"Failed to stop DiscordCrawler: {e}")
        
        return result
    
    async def stop_event_monitor(self) -> Dict[str, Any]:
        """Stop EventMonitor if running"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }

        if not EVENT_MONITOR_PATH:
            result["error"] = "EVENT_MONITOR_PATH is not configured"
            result["message"] = "EventMonitor のパスが設定されていません"
            return result
        
        try:
            # Check if running
            is_running = self._is_process_running("main.py", EVENT_MONITOR_PATH)
            
            if not is_running:
                result["success"] = False
                result["message"] = "EventMonitor is not running"
                return result
            
            # Terminate process
            logger.info("Stopping EventMonitor...")
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    if proc.info['cmdline'] and 'main.py' in " ".join(proc.info['cmdline']).lower():
                        if proc.info['cwd'] and str(EVENT_MONITOR_PATH).lower() in proc.info['cwd'].lower():
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except psutil.TimeoutExpired:
                                proc.kill()
                            logger.info(f"Stopped EventMonitor process (PID: {proc.info['pid']})")
                            result["success"] = True
                            result["message"] = "EventMonitor stopped successfully"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            if not result["success"]:
                result["message"] = "Failed to stop EventMonitor (process not found)"
                
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to stop EventMonitor: {e}"
            logger.error(f"Failed to stop EventMonitor: {e}")
        
        return result
    
    async def stop_hydrus_client(self) -> Dict[str, Any]:
        """Stop HydrusClient if running"""
        result = {
            "success": False,
            "message": "",
            "error": None
        }
        
        try:
            # Check if running
            found = False
            if psutil:
                for proc in psutil.process_iter(['pid', 'name', 'exe']):
                    try:
                        if proc.info['name'] and 'hydrus' in proc.info['name'].lower():
                            found = True
                            logger.info(f"Stopping HydrusClient (PID: {proc.info['pid']})...")
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except psutil.TimeoutExpired:
                                proc.kill()
                            logger.info(f"Stopped HydrusClient process (PID: {proc.info['pid']})")
                            result["success"] = True
                            result["message"] = "HydrusClient stopped successfully"
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            
            if not found:
                result["success"] = False
                result["message"] = "HydrusClient is not running"
                
        except Exception as e:
            result["error"] = str(e)
            result["message"] = f"Failed to stop HydrusClient: {e}"
            logger.error(f"Failed to stop HydrusClient: {e}")
        
        return result
