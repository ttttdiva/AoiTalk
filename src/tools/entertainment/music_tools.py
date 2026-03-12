"""
音楽再生関連のツール
Audio Player MCP Serverとの連携を通じてローカル音楽ファイルを管理・再生
"""
import json
from ..core import tool as function_tool
from ..external.mcp_tools import use_mcp_tool

@function_tool
async def search_music_files(query: str = "") -> str:
    """ローカルの音楽ファイルを検索します
    
    Args:
        query: 検索クエリ（ファイル名の一部）。空文字列の場合は全ての音楽ファイルを表示
        
    Returns:
        見つかった音楽ファイルのリスト
    """
    print(f"[Tool] search_music_files が呼び出されました: query='{query}'")
    
    try:
        # 公式サーバーのlist_audio_filesツールを使用
        result = await use_mcp_tool("audio_player", "list_audio_files", "{}")
        
        # クエリがある場合は結果をフィルタリング
        if query and query.strip():
            import json
            result_data = json.loads(result) if isinstance(result, str) else result
            if isinstance(result_data, dict) and "files" in result_data:
                filtered_files = [f for f in result_data["files"] if query.lower() in f.lower()]
                return f"検索結果 ('{query}'):\n" + "\n".join(f"- {f}" for f in filtered_files)
        
        return f"利用可能な音楽ファイル:\n{result}"
    except Exception as e:
        return f"音楽ファイル検索エラー: {str(e)}"

@function_tool
async def play_music(filename: str) -> str:
    """指定された音楽ファイルを再生します
    
    Args:
        filename: 再生する音楽ファイル名（フルパスではなくファイル名のみ）
        
    Returns:
        再生結果のメッセージ
    """
    print(f"[Tool] play_music が呼び出されました: filename='{filename}'")
    
    try:
        # 公式サーバーのplay_audioツールを使用
        arguments = {"filename": filename}
        result = await use_mcp_tool("audio_player", "play_audio", json.dumps(arguments))
        return result
    except Exception as e:
        return f"音楽再生エラー: {str(e)}"

@function_tool
async def stop_music() -> str:
    """現在再生中の音楽を停止します
    
    Returns:
        停止結果のメッセージ
    """
    print(f"[Tool] stop_music が呼び出されました")
    
    try:
        # 公式サーバーのstop_playbackツールを使用
        result = await use_mcp_tool("audio_player", "stop_playback", "{}")
        return result
    except Exception as e:
        return f"音楽停止エラー: {str(e)}"

@function_tool
async def get_music_player_status() -> str:
    """音楽プレーヤーの現在の状態を取得します
    
    Returns:
        プレーヤーの状態情報
    """
    print(f"[Tool] get_music_player_status が呼び出されました")
    
    try:
        # 公式サーバーにはstatus toolがないので、ファイル一覧で代用
        result = await use_mcp_tool("audio_player", "list_audio_files", "{}")
        return f"音楽プレーヤー状態:\n利用可能ファイル:\n{result}"
    except Exception as e:
        return f"プレーヤー状態取得エラー: {str(e)}"

@function_tool
async def find_and_play_music(song_name: str) -> str:
    """曲名で音楽を検索して再生します
    
    Args:
        song_name: 検索・再生したい曲名
        
    Returns:
        検索・再生結果のメッセージ
    """
    print(f"[Tool] find_and_play_music が呼び出されました: song_name='{song_name}'")
    
    try:
        # まず全てのファイルを取得
        files_result = await use_mcp_tool("audio_player", "list_audio_files", "{}")
        
        # 結果をパースして検索
        import json
        files_data = json.loads(files_result) if isinstance(files_result, str) else files_result
        
        if isinstance(files_data, dict) and "files" in files_data:
            # 曲名で検索
            matching_files = [f for f in files_data["files"] if song_name.lower() in f.lower()]
            
            if not matching_files:
                return f"'{song_name}'に該当する音楽ファイルが見つかりませんでした。利用可能なファイル: {', '.join(files_data['files'])}"
            
            # 最初に見つかったファイルを再生
            filename = matching_files[0]
            play_arguments = {"filename": filename}
            play_result = await use_mcp_tool("audio_player", "play_audio", json.dumps(play_arguments))
            
            return f"検索結果: '{song_name}' → '{filename}'\n再生結果: {play_result}"
        else:
            return f"ファイル一覧の取得に失敗しました: {files_result}"
        
    except Exception as e:
        return f"音楽検索・再生エラー: {str(e)}"