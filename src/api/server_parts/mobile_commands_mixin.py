"""モバイル UI 設定・クイックコマンド実行・スクリプト/システムコマンド実行関連の Mixin。

server.py から移設。ロジックは一切変更していない。
"""

from ..server_shared import *  # noqa: F401,F403


class MobileCommandsMixin:
    """WebChatServer のモバイルコマンド系メソッド群。"""

    def _extract_mobile_ui_config(self) -> Dict[str, Any]:
        """Safely extract mobile UI configuration"""
        try:
            if hasattr(self.config, "get_mobile_ui_config"):
                return self.config.get_mobile_ui_config()
            if hasattr(self.config, "get"):
                return self.config.get("mobile_ui", {})
            if isinstance(self.config, dict):
                return self.config.get("mobile_ui", {})
        except Exception as exc:
            logger.warning(f"モバイルUI設定の取得に失敗しました: {exc}")
        return {}

    def _mobile_commands_enabled(self) -> bool:
        return bool(self.mobile_ui_config.get("enabled", True))

    def _serialize_mobile_commands(self) -> List[Dict[str, Any]]:
        commands: List[Dict[str, Any]] = []
        for cmd in self.mobile_ui_config.get("quick_commands", []):
            if not isinstance(cmd, dict):
                continue
            commands.append(
                {
                    "id": cmd.get("id"),
                    "label": cmd.get("label", "コマンド"),
                    "hint": cmd.get("hint", ""),
                    "icon": cmd.get("icon", "sparkles"),
                    "accent": cmd.get("accent", "slate"),
                    "category": cmd.get("category", "その他"),
                    "action": cmd.get("action", "send_message"),
                    "requires_confirmation": cmd.get("requires_confirmation", False),
                    "confirmation_text": cmd.get("confirmation_text", ""),
                }
            )
        return commands

    def _get_mobile_command_by_id(self, command_id: str) -> Optional[Dict[str, Any]]:
        for cmd in self.mobile_ui_config.get("quick_commands", []):
            if isinstance(cmd, dict) and cmd.get("id") == command_id:
                return cmd
        return None

    async def _execute_mobile_command(self, command_id: str) -> Dict[str, Any]:
        command = self._get_mobile_command_by_id(command_id)
        if not command:
            raise HTTPException(
                status_code=404, detail=f"Command not found: {command_id}"
            )

        action = command.get("action", "send_message")
        label = command.get("label", command_id)
        logger.info(f"Executing mobile command: %s (%s)", label, action)

        if action == "send_message":
            payload = (command.get("payload") or "").strip()
            if not payload:
                raise HTTPException(status_code=400, detail="Command payload is empty")
            await self._handle_user_message(
                {
                    "message": payload,
                    "metadata": {"source": "mobile_command", "command_id": command_id},
                }
            )
            result = "user_message_sent"
        elif action == "clear_chat":
            await self._handle_clear_chat()
            result = "chat_cleared"
        elif action == "system_message":
            payload = (command.get("payload") or "").strip()
            if payload:
                await self.add_system_message(payload)
            result = "system_message_added"
        elif action == "run_script":
            # Check if progress streaming is enabled
            stream_progress = command.get("stream_progress", False)
            if stream_progress:
                result = await self._run_script_with_progress(command, command_id)
            else:
                result = await self._run_script_command(command)
        elif action == "run_system_command":
            result = await self._run_system_command(command)
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported command action: {action}"
            )

        return {
            "success": True,
            "result": result,
            "command": {"id": command_id, "label": label, "action": action},
        }

    async def _run_script_command(self, command: Dict[str, Any]) -> str:
        """Execute a Python script with optional venv support"""
        import asyncio

        script_path = command.get("script_path", "").strip()
        if not script_path:
            raise HTTPException(
                status_code=400, detail="script_path is required for run_script action"
            )

        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Script not found: {script_path}"
            )

        # Determine Python executable
        python_executable_override = command.get("python_executable", "").strip()
        use_venv = command.get("venv_python", False)

        if python_executable_override:
            # Use specified Python executable
            python_exe = python_executable_override
        elif use_venv:
            # Use venv Python from AoiTalk project
            venv_python = (
                Path(__file__).parent.parent.parent / "venv" / "Scripts" / "python.exe"
            )
            if not venv_python.exists():
                logger.warning(
                    f"Venv python not found at {venv_python}, falling back to system python"
                )
                python_exe = "python"
            else:
                python_exe = str(venv_python)
        else:
            python_exe = "python"

        # Determine working directory
        working_dir = command.get("working_directory", "").strip()
        if working_dir:
            cwd = working_dir
        else:
            cwd = str(script_file.parent)

        logger.info(f"Executing script: {script_path} with {python_exe} in {cwd}")

        try:
            # Execute script with timeout
            process = await asyncio.create_subprocess_exec(
                python_exe,
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            # Wait with timeout (5 minutes)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=300.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Script execution timed out (5 minutes)"
                )

            # Log output
            if stdout:
                logger.info(
                    f"Script stdout: {stdout.decode('utf-8', errors='ignore')[:500]}"
                )
            if stderr:
                logger.warning(
                    f"Script stderr: {stderr.decode('utf-8', errors='ignore')[:500]}"
                )

            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}",
                )

            return "script_executed"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script: {e}")
            raise HTTPException(
                status_code=500, detail=f"Script execution failed: {str(e)}"
            )

    async def _run_script_with_progress(
        self, command: Dict[str, Any], command_id: str
    ) -> str:
        """Execute a script with real-time progress streaming via WebSocket"""
        import asyncio
        import json as json_lib

        script_path = command.get("script_path", "").strip()
        if not script_path:
            raise HTTPException(
                status_code=400, detail="script_path is required for run_script action"
            )

        # Validate script path exists
        script_file = Path(script_path)
        if not script_file.exists():
            raise HTTPException(
                status_code=404, detail=f"Script not found: {script_path}"
            )

        # Determine Python executable or script type
        use_venv = command.get("venv_python", False)
        python_executable_override = command.get("python_executable", "").strip()

        # Check if it's a .bat file
        is_bat = script_path.lower().endswith(".bat")

        if is_bat:
            # For .bat files, execute directly
            cmd = [str(script_file)]
        else:
            # For Python scripts
            if python_executable_override:
                # Use specified Python executable
                python_exe = python_executable_override
            elif use_venv:
                venv_python = (
                    Path(__file__).parent.parent.parent
                    / "venv"
                    / "Scripts"
                    / "python.exe"
                )
                if not venv_python.exists():
                    logger.warning(
                        f"Venv python not found at {venv_python}, falling back to system python"
                    )
                    python_exe = "python"
                else:
                    python_exe = str(venv_python)
            else:
                python_exe = "python"
            cmd = [python_exe, str(script_path)]

        logger.info(f"Executing script with progress: {' '.join(cmd)}")

        try:
            # Set environment variable to enable progress reporting
            env = os.environ.copy()
            env["REPORT_PROGRESS"] = "true"

            # Execute script
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script_file.parent),
                env=env,
            )

            # Read stdout line by line and broadcast progress
            async def read_and_broadcast():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break

                    line_text = line.decode("utf-8", errors="ignore").strip()
                    logger.debug(f"Script output: {line_text}")

                    # Check for progress messages
                    if line_text.startswith("PROGRESS:"):
                        try:
                            # Parse JSON progress data
                            json_str = line_text[
                                9:
                            ].strip()  # Remove "PROGRESS: " prefix
                            progress_data = json_lib.loads(json_str)

                            # Broadcast to all WebSocket clients
                            await self.manager.broadcast(
                                {
                                    "type": "command_progress",
                                    "command_id": command_id,
                                    "data": progress_data,
                                }
                            )
                        except json_lib.JSONDecodeError as e:
                            logger.warning(f"Failed to parse progress JSON: {e}")

            # Start reading in background
            read_task = asyncio.create_task(read_and_broadcast())

            # Wait for process to complete (with timeout - 30 minutes for backup)
            try:
                await asyncio.wait_for(process.wait(), timeout=1800.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Script execution timed out (30 minutes)"
                )

            # Wait for reading to complete
            await read_task

            # Check return code
            if process.returncode != 0:
                # Read stderr
                stderr = await process.stderr.read()
                error_msg = stderr.decode("utf-8", errors="ignore")[:500]
                logger.error(f"Script failed: {error_msg}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Script failed with exit code {process.returncode}",
                )

            return "script_executed_with_progress"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute script with progress: {e}")
            raise HTTPException(
                status_code=500, detail=f"Script execution failed: {str(e)}"
            )

    async def _run_system_command(self, command: Dict[str, Any]) -> str:
        """Execute a system command (Windows-only)"""
        import asyncio

        command_line = command.get("command_line", "").strip()
        if not command_line:
            raise HTTPException(
                status_code=400,
                detail="command_line is required for run_system_command action",
            )

        logger.info(f"Executing system command: {command_line}")

        try:
            # Execute command with timeout
            process = await asyncio.create_subprocess_shell(
                command_line,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True,
            )

            # Wait with timeout (30 seconds for system commands)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=30.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=504, detail="Command execution timed out (30 seconds)"
                )

            # Log output
            if stdout:
                logger.info(
                    f"Command stdout: {stdout.decode('utf-8', errors='ignore')[:500]}"
                )
            if stderr:
                logger.warning(
                    f"Command stderr: {stderr.decode('utf-8', errors='ignore')[:500]}"
                )

            if process.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Command failed with exit code {process.returncode}",
                )

            return "system_command_executed"

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to execute system command: {e}")
            raise HTTPException(
                status_code=500, detail=f"Command execution failed: {str(e)}"
            )
