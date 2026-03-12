"""
キャラクター切り替え検出器
会話終了系のキーワードでキャラクター選択モードに移行し、
キャラクター名の発話で切り替えを実行する
"""

import re
from typing import Optional, Dict, Any, List, Tuple
import logging
import os
import yaml
from ..base import KeywordDetectorBase, KeywordDetectionResult, KeywordAction
from ..character_manager import get_character_manager
from ..selection_mode_state import get_selection_mode_state

logger = logging.getLogger(__name__)


class CharacterSwitchDetector(KeywordDetectorBase):
    """キャラクター切り替えキーワード検出器"""
    
    def __init__(self, enabled: bool = True, config: Optional[Any] = None):
        """初期化
        
        Args:
            enabled: 検出器の有効/無効
            config: 設定オブジェクト
        """
        super().__init__(tool_name="character_switch", enabled=enabled)
        self.config = config
        
        # グローバル選択モード状態を使用
        self.selection_mode_state = get_selection_mode_state()
        self.character_manager = get_character_manager()
        self.character_manager.register_callback(self._on_character_switch)
        
        # デバッグ用: インスタンスIDをログに出力
        logger.info(f"[CharacterSwitchDetector] 新しいインスタンスを作成しました (ID: {id(self)})")
        logger.info(f"[CharacterSwitchDetector] SelectionModeState インスタンスID: {id(self.selection_mode_state)}")
        
        # 現在のキャラクター名
        if config and config.get('default_character'):
            self.current_character = config.get('default_character')
        else:
            self.current_character = self.character_manager.get_current_character()
        
        # 利用可能なキャラクターとエイリアスのマッピング
        self.character_map: Dict[str, Tuple[str, str]] = {}  # {alias: (character_name, yaml_filename)}
        self.available_characters: List[str] = []
        
        # 会話終了系のキーワードパターン（音声認識のブレに対応）
        self.end_conversation_patterns = [
            r"会話を?終了",           # 会話を終了、会話終了
            r"会話の終了",
            r"エンド",
        ]
        
        # 正規表現パターンをコンパイル
        self.end_pattern = re.compile(
            '|'.join(f'({p})' for p in self.end_conversation_patterns),
            re.IGNORECASE
        )
        
        # 利用可能なキャラクターとエイリアスを読み込む
        self._load_character_aliases()
    
    def _load_character_aliases(self):
        """キャラクター設定ファイルからエイリアスを読み込む"""
        characters_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../../../../config/characters'
        )
        
        if not os.path.exists(characters_dir):
            logger.error(f"キャラクターディレクトリが見つかりません: {characters_dir}")
            return
        
        # 各YAMLファイルを読み込む
        for filename in os.listdir(characters_dir):
            if not filename.endswith('.yaml'):
                continue
            
            filepath = os.path.join(characters_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    char_config = yaml.safe_load(f)
                
                if not char_config:
                    continue
                
                char_name = char_config.get('name')
                aliases = char_config.get('recognition_aliases', [])
                
                if char_name and aliases:
                    self.available_characters.append(char_name)
                    
                    # エイリアスをマッピングに登録
                    for alias in aliases:
                        # エイリアスを小文字に正規化して登録
                        normalized_alias = alias.lower()
                        self.character_map[normalized_alias] = (char_name, filename[:-5])  # .yaml を除く
                        
                        logger.debug(f"キャラクターエイリアス登録: {alias} -> {char_name}")
                
            except Exception as e:
                logger.error(f"キャラクター設定ファイル {filename} の読み込みエラー: {e}")
        
        logger.info(f"登録されたキャラクター数: {len(self.available_characters)}")
        logger.info(f"登録されたエイリアス数: {len(self.character_map)}")
    
    def detect(self, text: str) -> KeywordDetectionResult:
        """テキストからキーワードを検出
        
        Args:
            text: 検出対象のテキスト
            
        Returns:
            検出結果
        """
        if not self.enabled:
            return KeywordDetectionResult(detected=False)

        # キャラクター選択モードの場合
        if self.selection_mode_state.active:
            logger.info(f"[CharacterSwitchDetector] 選択モード中のテキスト検出: '{text}' (選択モード: {self.selection_mode_state.active}, インスタンスID: {id(self)})")
            return self._detect_character_name(text)

        # 通常モードで会話終了キーワードを検出
        match = self.end_pattern.search(text)
        if match:
            logger.info(f"[CharacterSwitchDetector] 会話終了キーワード検出: '{match.group()}' (選択モード: {self.selection_mode_state.active}, インスタンスID: {id(self)})")
            return KeywordDetectionResult(
                detected=True,
                action=KeywordAction.PROCESS,
                parameters={
                    'action': 'enter_selection_mode',
                    'matched_text': match.group()
                },
                bypass_llm=True
            )

        return KeywordDetectionResult(detected=False)
    
    def _detect_character_name(self, text: str) -> KeywordDetectionResult:
        """キャラクター名を検出（選択モード時）
        
        Args:
            text: 検出対象のテキスト
            
        Returns:
            検出結果
        """
        # テキストを正規化
        normalized_text = text.lower().strip()
        
        # 完全一致を優先的にチェック
        for alias, (char_name, yaml_name) in self.character_map.items():
            if normalized_text == alias:
                return KeywordDetectionResult(
                    detected=True,
                    action=KeywordAction.PROCESS,
                    parameters={
                        'action': 'switch_character',
                        'character_name': char_name,
                        'yaml_filename': yaml_name,
                        'matched_alias': alias
                    },
                    bypass_llm=True
                )
        
        # 部分一致もチェック
        for alias, (char_name, yaml_name) in self.character_map.items():
            if alias in normalized_text or normalized_text in alias:
                return KeywordDetectionResult(
                    detected=True,
                    action=KeywordAction.PROCESS,
                    parameters={
                        'action': 'switch_character',
                        'character_name': char_name,
                        'yaml_filename': yaml_name,
                        'matched_alias': alias
                    },
                    bypass_llm=True
                )
        
        # キャラクター名が見つからない場合も、選択モード中は全てバイパス
        return KeywordDetectionResult(
            detected=True,
            action=KeywordAction.PROCESS,
            parameters={
                'action': 'selection_mode_no_match'
            },
            bypass_llm=True
        )
    
    def process(self, result: KeywordDetectionResult) -> Optional[str]:
        """検出結果を処理
        
        Args:
            result: 検出結果
            
        Returns:
            処理結果のメッセージ
        """
        if not result.parameters:
            return None
        
        action = result.parameters.get('action')
        
        if action == 'enter_selection_mode':
            # キャラクター選択モードに移行
            logger.info(f"[CharacterSwitchDetector] 選択モード移行前の状態: {self.selection_mode_state.active} (インスタンスID: {id(self)})")
            self.selection_mode_state.activate()
            logger.info(f"[CharacterSwitchDetector] 選択モードをアクティブにしました: {self.selection_mode_state.active} (インスタンスID: {id(self)})")
            
            # 現在のキャラクターのgoodbyeReplyを取得
            goodbye_message = self._get_character_goodbye(self.current_character)
            
            # 利用可能なキャラクターのリストを作成
            character_list = "\n".join([f"• {char}" for char in self.available_characters])
            
            # goodbyeReplyを含めたメッセージを返す
            return {
                'message': (
                    f"キャラクター選択モードです。\n"
                    f"以下のキャラクターから選んでください：\n"
                    f"{character_list}\n"
                    f"（キャラクター名を言ってください）"
                ),
                'goodbye_reply': goodbye_message,
                'mode': 'selection_mode'
            }
        
        elif action == 'switch_character':
            # キャラクターを切り替え
            char_name = result.parameters['character_name']
            yaml_filename = result.parameters['yaml_filename']
            
            # 実際の切り替え処理を実行
            logger.info(f"[CharacterSwitchDetector] キャラクター切り替えを実行: {char_name} ({yaml_filename})")
            success = self._apply_character_switch(char_name, yaml_filename)
            logger.info(f"[CharacterSwitchDetector] キャラクター切り替え結果: {success}")
            
            if success:
                # 選択モードを終了
                logger.info(f"[CharacterSwitchDetector] 選択モード終了前の状態: {self.selection_mode_state.active} (インスタンスID: {id(self)})")
                self.selection_mode_state.deactivate()
                logger.info(f"[CharacterSwitchDetector] 選択モードを終了しました: {self.selection_mode_state.active} (インスタンスID: {id(self)})")
                self.current_character = char_name
                
                # 新しいキャラクターのgreetingを取得
                greeting_message = self._get_character_greeting(char_name)
                
                return {
                    'message': f"わかりました。{char_name}として話します。",
                    'greeting': greeting_message,
                    'mode': 'character_switched'
                }
            else:
                # 失敗時も選択モードは継続
                return {
                    'message': "キャラクターの切り替えに失敗しました。もう一度キャラクター名を言ってください。",
                    'mode': 'selection_mode_failed'
                }
        
        elif action == 'selection_mode_no_match':
            # 選択モード中でマッチしなかった場合
            logger.info(f"[CharacterSwitchDetector] 選択モード中でキャラクターが見つかりませんでした。選択モード継続: {self.selection_mode_state.active}")
            return {
                'message': "そのキャラクターは見つかりませんでした。もう一度キャラクター名を言ってください。",
                'mode': 'selection_mode_no_match'
            }
        
        return None
    
    def _apply_character_switch(self, character_name: str, yaml_filename: str) -> bool:
        """キャラクター切り替えを適用
        
        Args:
            character_name: キャラクター名
            yaml_filename: YAMLファイル名（拡張子なし）
            
        Returns:
            成功したかどうか
        """
        try:
            # CharacterSwitchManagerを使用してキャラクターを切り替え
            manager = get_character_manager()
            success = manager.switch_character(character_name, yaml_filename)
            
            if success:
                logger.info(f"キャラクター切り替え成功: {self.current_character} -> {character_name} ({yaml_filename})")
            else:
                logger.warning(f"キャラクター切り替えに一部失敗: {character_name}")
            
            return success
            
        except Exception as e:
            logger.error(f"キャラクター切り替えエラー: {e}")
            return False
    
    def is_in_selection_mode(self) -> bool:
        """キャラクター選択モードかどうかを返す
        
        Returns:
            選択モード中ならTrue
        """
        return self.selection_mode_state.active
    
    def get_current_character(self) -> str:
        """現在のキャラクター名を取得
        
        Returns:
            現在のキャラクター名
        """
        return self.current_character

    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """CharacterSwitchManager callback to keep detector state in sync."""
        self.current_character = character_name
    
    def _get_character_config(self, character_name: str) -> Optional[Dict[str, Any]]:
        """キャラクター設定を取得
        
        Args:
            character_name: キャラクター名
            
        Returns:
            キャラクター設定辞書
        """
        # キャラクター名からYAMLファイルを特定
        yaml_filename = None
        for alias, (char_name, yaml_name) in self.character_map.items():
            if char_name == character_name:
                yaml_filename = yaml_name
                break
        
        if not yaml_filename:
            return None
        
        # YAMLファイルを読み込む
        characters_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '../../../../config/characters'
        )
        filepath = os.path.join(characters_dir, f"{yaml_filename}.yaml")
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"キャラクター設定読み込みエラー: {e}")
            return None
    
    def _get_character_goodbye(self, character_name: str) -> str:
        """キャラクターのgoodbyeReplyを取得
        
        Args:
            character_name: キャラクター名
            
        Returns:
            goodbyeReplyメッセージ
        """
        config = self._get_character_config(character_name)
        if config:
            personality = config.get('personality', {})
            return personality.get('goodbyeReply', 'さようなら')
        return 'さようなら'
    
    def _get_character_greeting(self, character_name: str) -> str:
        """キャラクターのgreetingを取得
        
        Args:
            character_name: キャラクター名
            
        Returns:
            greetingメッセージ
        """
        config = self._get_character_config(character_name)
        if config:
            personality = config.get('personality', {})
            return personality.get('greeting', 'こんにちは')
        return 'こんにちは'
