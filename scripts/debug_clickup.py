#!/usr/bin/env python
"""ClickUp APIデバッグスクリプト: Sigotoフォルダのタスク取得テスト"""
import os
import sys
import httpx
from pathlib import Path
from dotenv import load_dotenv

# .env読み込み
project_root = Path(__file__).resolve().parents[1]
load_dotenv(project_root / ".env")

API_KEY = os.getenv("CLICKUP_API_KEY", "")
TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")
BASE_URL = "https://api.clickup.com/api/v2"

headers = {
    "Authorization": API_KEY,
    "Content-Type": "application/json",
}

def main():
    print(f"=== ClickUp API デバッグ ===")
    print(f"API_KEY: {API_KEY[:10]}...{API_KEY[-4:]}" if API_KEY else "API_KEY: 未設定")
    print(f"TEAM_ID: {TEAM_ID}")
    print()

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30.0) as client:
        # Step 1: スペース一覧
        print("--- Step 1: スペース一覧 ---")
        resp = client.get(f"/team/{TEAM_ID}/space")
        print(f"Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            return
        spaces = resp.json().get("spaces", [])
        for s in spaces:
            print(f"  Space: {s['name']} (ID: {s['id']})")
        print()

        # Step 2: 各スペースのフォルダ一覧 → Sigotoを探す
        print("--- Step 2: フォルダ一覧（Sigoto探索） ---")
        sigoto_folder_id = None
        sigoto_lists = []
        for space in spaces:
            resp = client.get(f"/space/{space['id']}/folder")
            if resp.status_code != 200:
                print(f"  Space {space['name']}: Error {resp.status_code}")
                continue
            folders = resp.json().get("folders", [])
            for f in folders:
                print(f"  Space={space['name']}, Folder: {f['name']} (ID: {f['id']})")
                folder_lists = f.get("lists", [])
                for lst in folder_lists:
                    print(f"    List: {lst['name']} (ID: {lst['id']})")
                if "sigoto" in f["name"].lower() or "仕事" in f["name"]:
                    sigoto_folder_id = f["id"]
                    sigoto_lists = folder_lists
                    print(f"  >>> Sigotoフォルダ発見! ID={sigoto_folder_id}")
        print()

        if not sigoto_folder_id:
            print("Sigotoフォルダが見つかりませんでした。")
            print("全フォルダ名を確認してください。")
            return

        # Step 3: Sigotoフォルダのリストからタスク取得
        print(f"--- Step 3: Sigotoフォルダ (ID={sigoto_folder_id}) のタスク取得 ---")
        for lst in sigoto_lists:
            print(f"\n  リスト: {lst['name']} (ID: {lst['id']})")
            resp = client.get(
                f"/list/{lst['id']}/task",
                params={"archived": "false", "include_closed": "false"},
            )
            print(f"  Status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"  Error: {resp.text[:500]}")
                continue
            tasks = resp.json().get("tasks", [])
            print(f"  タスク数: {len(tasks)}")
            for t in tasks[:10]:
                status = t.get("status", {}).get("status", "?")
                name = t.get("name", "?")
                print(f"    [{status}] {name} (ID: {t.get('id')})")

        # Step 4: search_tasks API (team level) でも試す
        print(f"\n--- Step 4: Team-level search (exclude closed) ---")
        params = [("include_closed", "false")]
        resp = client.get(f"/team/{TEAM_ID}/task", params=params)
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            tasks = resp.json().get("tasks", [])
            print(f"全タスク数: {len(tasks)}")
            # Sigotoフォルダのタスクをフィルタ
            sigoto_tasks = [t for t in tasks if t.get("folder", {}).get("id") == sigoto_folder_id]
            print(f"Sigotoフォルダのタスク数: {len(sigoto_tasks)}")
            for t in sigoto_tasks[:10]:
                status = t.get("status", {}).get("status", "?")
                name = t.get("name", "?")
                lst_name = t.get("list", {}).get("name", "?")
                print(f"  [{status}] {name} (リスト: {lst_name})")
        else:
            print(f"Error: {resp.text[:500]}")


if __name__ == "__main__":
    main()
