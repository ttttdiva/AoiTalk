"""Authenticated PC registration, explicit selection and outbound-WS relay."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ...services.pc_bridge_service import get_pc_bridge_hub, decode_screenshot
from ...services.pc_bridge_store import PcBridgeError


class BridgeRegistration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)


class BridgeSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device_id: str | None = None
    session_id: str | None = None


class BridgeCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: Literal['browser', 'computer']
    action: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


class BridgeReply(BaseModel):
    success: bool = True
    result: dict[str, Any] = Field(default_factory=dict)


def register_pc_bridge_routes(app: FastAPI, server) -> None:
    hub = get_pc_bridge_hub()

    async def owner(request):
        user = await server._get_user_info_from_request(request)
        if not user:
            raise HTTPException(401, 'Not authenticated')
        return str(user['id'])

    @app.get('/api/pc-bridge/devices', response_model=BridgeReply, operation_id='pc_bridge_list_devices')
    async def devices(request: Request):
        return BridgeReply(result=await hub.devices(await owner(request)))

    @app.post('/api/pc-bridge/devices', response_model=BridgeReply, operation_id='pc_bridge_register_device')
    async def register(body: BridgeRegistration, request: Request):
        return BridgeReply(result=await hub.store.register(await owner(request), body.name.strip()))

    @app.put('/api/pc-bridge/selection', response_model=BridgeReply, operation_id='pc_bridge_select_device')
    async def select(body: BridgeSelection, request: Request):
        user_id = await owner(request)
        if body.session_id and not await server._websocket_session_allowed(body.session_id, user_id):
            raise HTTPException(403, 'Conversation access denied')
        try:
            await hub.store.select(user_id, body.device_id, body.session_id)
        except PcBridgeError as exc:
            raise HTTPException(404, str(exc)) from None
        return BridgeReply(result={'selected_device_id': body.device_id})

    @app.delete('/api/pc-bridge/devices/{device_id}', response_model=BridgeReply, operation_id='pc_bridge_revoke_device')
    async def revoke(device_id: str, request: Request):
        try:
            await hub.revoke(await owner(request), device_id)
        except PcBridgeError as exc:
            raise HTTPException(404, str(exc)) from None
        return BridgeReply()

    @app.post('/api/pc-bridge/devices/{device_id}/command', response_model=BridgeReply, operation_id='pc_bridge_command')
    async def command(device_id: str, body: BridgeCommand, request: Request):
        user_id = await owner(request)
        try:
            await hub.store.resolve(user_id, device_id)
            result = await hub.call(user_id, device_id, body.channel, body.action, body.params)
        except PcBridgeError as exc:
            raise HTTPException(409, str(exc)) from None
        return BridgeReply(result=result)

    @app.get('/api/pc-bridge/devices/{device_id}/screen', operation_id='pc_bridge_screenshot',
             responses={200: {'content': {'image/jpeg': {}}}})
    async def screenshot(device_id: str, request: Request):
        user_id = await owner(request)
        try:
            await hub.store.resolve(user_id, device_id)
            result = await hub.call(user_id, device_id, 'computer', 'screenshot')
            data = decode_screenshot(result)
        except PcBridgeError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @app.get('/api/pc-bridge/download', operation_id='pc_bridge_download')
    async def download(request: Request):
        from pathlib import Path
        await owner(request)
        binary = Path(__file__).resolve().parents[3] / 'AoiTalk-PC-Bridge.exe'
        if not binary.is_file():
            raise HTTPException(404, 'Build-PC-Bridge.batを実行してexeを作成してください')
        return FileResponse(binary, filename=binary.name, media_type='application/octet-stream')

    @app.websocket('/api/pc-bridge/connect')
    async def connect(socket: WebSocket):
        import asyncio
        connection = None
        try:
            header = socket.headers.get('authorization', '')
            if not header.startswith('Bearer '):
                await socket.close(code=4401)
                return
            user_id, device = await hub.store.authenticate(header[7:])
            await socket.accept()
            hello = await asyncio.wait_for(socket.receive_json(), 10)
            if (not isinstance(hello, dict) or hello.get('type') != 'hello' or hello.get('protocol') != 1
                    or not isinstance(hello.get('capabilities'), list)
                    or any(item not in {'computer', 'browser'} for item in hello.get('capabilities', []))):
                await socket.close(code=4400)
                return
            connection = hub.attach(user_id, device['id'], socket, hello)
            await socket.send_json({'type': 'ready', 'device_id': device['id'], 'name': device['name']})
            while True:
                message = await asyncio.wait_for(socket.receive_json(), 45)
                if not isinstance(message, dict):
                    break
                hub.receive(connection, message)
        except PcBridgeError:
            await socket.close(code=4403)
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError, TypeError):
            pass
        finally:
            if connection:
                hub.detach(connection)
