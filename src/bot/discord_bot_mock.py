"""
Discord bot mock for debugging without token
"""

import asyncio
import logging
from typing import Dict, Optional

from ..config import Config
from .modes.discord_mode import DiscordMode

logger = logging.getLogger(__name__)


class MockDiscordBot:
    """Mock Discord Bot for debugging"""
    
    def __init__(self, config: Config):
        self.config = config
        self.running = True
        self.discord_mode = DiscordMode(config)
        logger.info("MockDiscordBot initialized")
        
    async def simulate_messages(self):
        """Simulate Discord messages for testing"""
        print("\n🤖 Discord Bot モック起動")
        print("Discord Botのシミュレーションモードで動作しています")
        print("実際のDiscord接続は行われません")
        print("-" * 50)
        
        # Simulate bot ready event
        print("✅ Bot準備完了（シミュレーション）")
        print("📢 /help でヘルプを表示")
        
        # Simulate some test interactions
        test_messages = [
            {"user": "TestUser", "content": "こんにちは"},
            {"user": "TestUser", "content": "今日の天気を教えて"},
            {"user": "TestUser", "content": "Spotifyで音楽を再生して"},
        ]
        
        await asyncio.sleep(2)  # Wait a bit before starting
        
        for i, msg in enumerate(test_messages):
            if not self.running:
                break
                
            print(f"\n[{msg['user']}]: {msg['content']}")
            
            # Process through Discord mode
            try:
                response = await self.discord_mode.process_text(msg['content'])
                print(f"[AoiTalk]: {response}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                print(f"[エラー]: メッセージ処理中にエラーが発生しました: {e}")
            
            await asyncio.sleep(3)  # Wait between messages
            
        print("\n📝 シミュレーション終了")
        
    async def run(self):
        """Run the mock bot"""
        try:
            await self.simulate_messages()
        except KeyboardInterrupt:
            print("\n🛑 シミュレーションを中断しました")
        except Exception as e:
            logger.error(f"Mock bot error: {e}")
            print(f"❌ モックボットエラー: {e}")
        finally:
            self.running = False


async def run_mock_bot(config: Config):
    """Run mock Discord bot for debugging"""
    print("\n⚠️  Discord Bot トークンが設定されていないため、モックモードで起動します")
    print("実際のDiscord接続は行われません")
    
    mock_bot = MockDiscordBot(config)
    await mock_bot.run()