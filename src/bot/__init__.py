"""
Discord Bot package

This module provides Discord bot functionality.
Feature Flag: FEATURE_DISCORD_BOT controls whether these components are loaded.
"""
from src.features import Features

# Conditional imports based on Feature Flags
if Features.discord_bot():
    from .discord_bot import run_bot
    
    __all__ = ['run_bot']
else:
    # Feature disabled - provide None stub
    run_bot = None
    
    __all__ = ['run_bot']