from src.services.screenplay_character_import import build_character_payloads


def test_character_warehouse_splits_characters_and_keeps_dialogues_structured():
    markdown = """---
title: キャラ倉庫
---

# メイン
## 東北きりたん(とうほく きりたん)
- 名前:東北きりたん(とうほく きりたん)
- 性別:女性
- 年齢:11歳
- 職業:小学生
- 性格:気弱
- 人間関係:東北ずん子、東北イタコの妹/ウナの親友
- 備考:
臆病で、なんとか声を絞り出すように話す
黒魔術がある程度使える
- 経歴:
親友のウナを知能ゾンビとして復活させる
- 台詞例:
「どうも……」
「いちいち構わないでください……」
[[20230413095613|きりたん 詳細]]

# サブ
## 水奈瀬コウ(みなせ こう)
- 名前:水奈瀬コウ(みなせ こう)
- 性別:男性
- 年齢:24歳
- 職業:小学校教諭
- 性格:偏見/冷笑的
- 人間関係:琴葉茜の友人
- 備考:
子供に対しても容赦なく厳しい態度を取る
- 台詞例:
「まぁ好きにしろよ」
"""

    payloads = build_character_payloads(markdown)

    assert [payload["name"] for payload in payloads] == [
        "東北きりたん(とうほく きりたん)",
        "水奈瀬コウ(みなせ こう)",
    ]

    kiritan = payloads[0]
    assert kiritan["importance"] == 0
    assert "臆病で、なんとか声を絞り出すように話す" in kiritan["speech_patterns"]
    assert "「どうも……」" in kiritan["example_dialogues"]
    assert "きりたん 詳細" not in kiritan["example_dialogues"]
    assert "親友のウナを知能ゾンビとして復活させる" in kiritan["backstory"]
    assert kiritan["relationships"] == [
        {
            "target": "",
            "type": "relation",
            "description": "東北ずん子、東北イタコの妹",
        },
        {
            "target": "",
            "type": "relation",
            "description": "ウナの親友",
        },
    ]

    assert payloads[1]["importance"] == 1


def test_character_warehouse_ignores_non_character_sections():
    markdown = """# メイン
## メモ
- 備考:
キャラクターではない

## 琴葉茜(ことのは あかね)
- 名前:琴葉茜(ことのは あかね)
- 性格:衝動的/無気力/不愛想
- 台詞例:
「今まで試されたことないだけの奴が偉そうに言うんよな」
"""

    payloads = build_character_payloads(markdown)

    assert len(payloads) == 1
    assert payloads[0]["name"] == "琴葉茜(ことのは あかね)"
    assert "衝動的/無気力/不愛想" in payloads[0]["speech_patterns"]
