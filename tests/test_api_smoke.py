import asyncio

from fastapi import FastAPI

from src.config import Config
from src.api.server import create_web_interface


def test_fastapi_app_instantiates():
    config = Config()

    async def build_app():
        server = create_web_interface(config, config.default_character)
        return server.get_app()

    app = asyncio.run(build_app())
    assert isinstance(app, FastAPI)
