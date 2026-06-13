"""Rewrite screenplay scenario context as self-contained story metadata.

Revision ID: 20260429_0013
Revises: 20260429_0012
Create Date: 2026-04-29 00:00:00
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "20260429_0013"
down_revision = "20260429_0012"
branch_labels = None
depends_on = None


SCENARIOS = {
    "f01_main": {
        "id": "1b08cbef-1434-5c9c-8bd9-4508d74e0d44",
        "description": (
            "現代日本で悪魔の侵入が進む中、無気力と怒りを抱えた琴葉茜が、"
            "黒魔術を背負う東北きりたんと出会い、ギルドと悪魔界の脅威に"
            "巻き込まれていくダークファンタジー本編。抑圧された怒り、"
            "自己保身の後悔、他者を守ることの暴力性と限界を、茜ときりたんの"
            "視点を交互に置きながら描く。"
        ),
        "setting": (
            "地球では数年前から悪魔の出現が始まっており、黒魔術組織であるギルドが"
            "原因調査と隠蔽、対処を進めている。ギルドは悪魔界との接続や魂素の過剰流入を"
            "把握しており、生態系崩壊を避けるための夜明け止めの儀を準備している。\n\n"
            "悪魔界は赤い空、人のいない未来都市、異形化した動植物、多種多様な悪魔が存在する"
            "地球とは異なる世界。茜はこの悪魔界に一人で送り込まれる予定で、きりたんは"
            "過去に助けられた恩義から茜を助けに行くことを選ぶ。\n\n"
            "物語の核は、茜の無気力な日常にきりたんが割り込み、茜が自分の怒りと"
            "見捨てた後悔に向き合わされること。茜は黒魔術師ではなく一般人だが、"
            "自分のルールを破られた時には暴力も辞さない。きりたんは黒魔術、錬金術、"
            "死霊術の知識を持つが、孤独と罪悪感を抱える子供として扱う。"
        ),
        "gm_instructions": (
            "茜視点ときりたん視点を中心に、暗いが過激一辺倒にしない空気で進める。"
            "茜は冷淡なだけの人物にせず、相手のペースを待ってそばにいる態度を出す。"
            "きりたんは有能な黒魔術師である前に、傷つきやすく、友人を失うことを恐れる子供として描く。"
            "ギルド、悪魔、黒魔術、錬金術の設定はCanonと設定シーンを優先し、"
            "未確定メモは採用済み事実と混同しない。"
        ),
        "voice_tone": (
            "日本語のダークファンタジー脚本として、会話は淡々とした地の文と感情のにじむ台詞を"
            "組み合わせる。茜はぶっきらぼうだが過剰に荒くしない。きりたんは丁寧で弱さを隠しきれない。"
        ),
        "canon_entries": [
            ("世界観", "f01-world", "現代の地球には悪魔の侵入が進行しており、ギルドは黒魔術と隠蔽を用いて対処している。"),
            ("主題", "f01-theme", "本編の核は、抑圧された怒り、自己保身の後悔、他者を守ることの暴力性と限界である。"),
            ("人物", "f01-akane-kiritan", "茜は無気力な元SEで黒魔術を使えない一般人、きりたんは黒魔術と錬金術を扱うが孤独と罪悪感を抱える子供である。"),
            ("筋", "f01-plot-core", "茜はきりたんを守ろうとして失敗し、自己保身で逃げた後悔を抱えたまま、ギルドと悪魔界の脅威へ巻き込まれる。"),
        ],
    },
    "f01_hibernation": {
        "id": "fada1570-6b09-5ff5-a002-7404886b1416",
        "description": (
            "秩父盆地を舞台に、鳴花ヒメの種子が生む静域によって生体反応が減速し、"
            "交通、通信、医療、避難秩序が崩壊していく災害サバイバル編。"
            "茜、きりたん、ウナ、葵、トバリ、ゆかり、ささらが、それぞれの知識、"
            "暴力、交渉、科学、黒魔術を持ち寄り、殺種毒で種子を止めるまでを描く。"
        ),
        "setting": (
            "舞台は埼玉県秩父盆地。東の正丸峠、北東の長瀞、西の雁坂方面、北西の志賀坂峠、"
            "南の林道が順に封鎖され、約5.7万人が盆地に閉じ込められる。秩父太平洋セメント工場、"
            "荒川の橋、秩父三十四観音霊場の巡礼路、武甲山の白い山肌を主要な舞台装置として使う。\n\n"
            "脅威は鳴花ヒメの種子が形成する静域。静域では生体内の化学反応が減速し、"
            "倦怠感、体温低下、思考鈍化、意識喪失、心停止へ進む。外気温や電子機器は通常通りで、"
            "機械は動くが生物だけが影響を受ける。距離と暴露時間の積が効き、複数の種子が"
            "同時に静域を広げる。静域が接触すると種子が合流し、ヒメ本体が形成される。\n\n"
            "対抗手段は殺種毒。きりたんの錬金術による反応促進偏向を濃縮し、種子の核へ注入して"
            "魂素吸収機構を過負荷で焼き切る。核は核心部にあるため、きりたんと護衛が短時間で"
            "危険域へ入る必要がある。通常兵器は核を完全に止められず、自衛隊の掘削と殺種毒の"
            "合わせ技だけが有効。\n\n"
            "主要ラインは、茜ときりたんの現場サバイバル、ゆかりとささらの物資・交渉・銃による"
            "生存戦略、葵とトバリの法人追跡・GIS・薬理分析による外部支援。全員が足りないものを"
            "抱えており、完全な勝利ではなく、甚大な被害の後に種子を止める結末へ向かう。"
        ),
        "gm_instructions": (
            "環境脅威のルールを段階的に発見する災害サバイバルとして進める。"
            "脅威は操り人形や洗脳ではなく、静域、インフラ崩壊、情報途絶、人間同士の判断衝突から作る。"
            "茜は荒事で事態を動かすが、暴力では守れない局面にぶつける。"
            "きりたんの知識は有効だが、11歳の体とウナの稼働時間制限が常に足かせになる。"
            "トバリは未知の現象を科学の言葉へ翻訳し、葵は遠くから合理的に全体像を掴む。"
            "ささらの監禁黒魔術師解放と自首、ウナの稼働限界、茜の入院、ずん子の撤退までを"
            "きれいな勝利にしない。"
        ),
        "voice_tone": (
            "災害サバイバルの緊迫感を保ち、観察、仮説、対策、失敗のサイクルで進める。"
            "感情的な場面でも説明過多にせず、行動と判断の衝突で見せる。"
        ),
        "canon_entries": [
            ("静域", "hibernation-static-zone", "静域は鳴花ヒメの種子が生む領域で、生体の化学反応を減速させるが、機械や電子機器には直接作用しない。"),
            ("種子", "hibernation-seeds", "種子は雲取山北面、正丸峠、長瀞、さいたま市南部に発芽し、秩父盆地の出口と外部支援を段階的に潰す。"),
            ("対抗手段", "hibernation-poison", "殺種毒はきりたんの錬金術で作る種子用の毒で、核心部の核へ直接注入しなければ効果がない。"),
            ("結末", "hibernation-ending", "種子は全滅し静域は崩壊するが、茜は累積暴露で入院し、ウナは稼働限界に追い込まれ、ささらは自首し、関東の被害は甚大なまま残る。"),
            ("主題", "hibernation-theme", "全員が一人では足りず、知識、暴力、交渉、科学、黒魔術を持ち寄っても、犠牲なしには生き延びられない。"),
        ],
    },
    "f02": {
        "id": "3f2a3aeb-ef89-54c5-94c7-2ca36626f900",
        "description": (
            "琴葉茜、東北きりたん、音街ウナを中心に、依存、罪悪感、保護欲、黒魔術による"
            "死者の復活が絡む暗いキャラクター劇を、単発エピソード形式で扱うシリーズ。"
            "葵、ゆかり、ナースロボ＿タイプT、鳴花姉妹などの周辺人物を通じて、"
            "救済になりきらない寄り添いと破綻寸前の日常を描く。"
        ),
        "setting": (
            "茜は24歳の元SEで、現在は無気力と自暴自棄を抱えながら暮らしている。"
            "他人や社会のルールより自分の中のルールを重視し、きりたんには冷淡ではなく、"
            "淡々とそばにいる形で寄り添う。\n\n"
            "きりたんは気弱で傷つきやすい子供だが、黒魔術、錬金術、死霊術を扱える。"
            "親友のウナを不完全な知能ゾンビとして復活させており、その罪悪感と、"
            "ウナを完全に救いたい使命感を抱えている。ウナは理性的で規律を重んじ、"
            "週に限られた時間だけ活動できる存在として、きりたんを守ることを支えにしている。\n\n"
            "葵は明るくフレンドリーに振る舞うが、自分の感情を徹底的に制御し、合理的に他者へ介入する。"
            "ゆかりは対話の継続と率直さを重視し、社会的な善行や自己犠牲に懐疑的。"
            "ナースロボ＿タイプTは穏やかな医療者の態度で相手の意思を無視し、保護と治療を強制する。"
            "鳴花ヒメと鳴花ミコトは強い呪力と不均衡な保護関係を持つ精霊として扱う。\n\n"
            "各エピソードは独立した短編として扱い、別エピソードへ勝手に継続・統合しない。"
            "人物の依存、保護、支配、罪悪感、救済の失敗を心理劇として描く。"
        ),
        "gm_instructions": (
            "このシナリオは単発短編群として扱い、エピソードごとの状況を勝手に接続しない。"
            "キャラクター設定、口調、関係性を優先し、茜を説教役や過剰な保護者にしない。"
            "きりたんとウナは互いを支えにするが、黒魔術と復活の罪悪感を軽く扱わない。"
            "ナースロボ＿タイプTは悪意ではなく善意と医療的正しさの独自解釈で怖さを出す。"
            "未成年キャラクターを含む性的描写や露骨な成人向け描写は行わず、危うさは心理的距離、"
            "支配、保護、拒否、依存の緊張として処理する。"
        ),
        "voice_tone": (
            "暗い日常劇として、過度に説明せず、淡々とした会話と内面のズレで不穏さを出す。"
            "単発エピソードごとの温度感を保ち、人物の口調を設定資料に合わせる。"
        ),
        "canon_entries": [
            ("中心人物", "f02-core-cast", "中心は茜、きりたん、ウナであり、無気力な大人、黒魔術を背負う子供、不完全に復活した親友の三者関係を軸にする。"),
            ("黒魔術", "f02-necromancy", "きりたんはウナを記憶を保った知能ゾンビとして復活させたが、術は不完全で、ウナは週に限られた時間しか活動できない。"),
            ("形式", "f02-standalone", "各outputsは単発エピソードとして扱い、明示がない限り別エピソードの続きとして統合しない。"),
            ("描写方針", "f02-safety", "未成年キャラクターを含む性的描写は行わず、依存、保護、支配、罪悪感の緊張を心理劇として扱う。"),
        ],
    },
}


MANAGEMENT_FACT_PATTERNS = (
    "%D:\\Screenplay%",
    "%旧D:%",
    "%旧F01%",
    "%旧F02%",
    "%旧資料%",
    "%旧outputs%",
    "%移行%",
    "%AoiTalkのシナリオDB%",
    "%RAGコレクション%",
    "%正史と混ざらない%",
    "%参考資料として扱う%",
)


def stable_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk-screenplay:{key}")


def upgrade() -> None:
    bind = op.get_bind()

    for scenario in SCENARIOS.values():
        bind.execute(
            sa.text(
                """
                UPDATE scenarios
                SET
                    description = :description,
                    setting = :setting,
                    gm_instructions = :gm_instructions,
                    voice_tone = :voice_tone,
                    updated_at = now()
                WHERE id = :scenario_id
                """
            ),
            {
                "scenario_id": scenario["id"],
                "description": scenario["description"],
                "setting": scenario["setting"],
                "gm_instructions": scenario["gm_instructions"],
                "voice_tone": scenario["voice_tone"],
            },
        )

        for idx, (category, key, fact) in enumerate(scenario["canon_entries"]):
            canon_id = stable_uuid(f"canon:{scenario['id']}:{key}:{idx}")
            bind.execute(
                sa.text(
                    """
                    INSERT INTO scenario_canon_entries (
                        id, scenario_id, category, fact, created_at
                    )
                    VALUES (:id, :scenario_id, :category, :fact, now())
                    ON CONFLICT (id) DO UPDATE SET
                        category = EXCLUDED.category,
                        fact = EXCLUDED.fact
                    """
                ),
                {
                    "id": str(canon_id),
                    "scenario_id": scenario["id"],
                    "category": category,
                    "fact": fact,
                },
            )

    management_where = " OR ".join(
        f"fact LIKE :pattern_{idx}" for idx, _ in enumerate(MANAGEMENT_FACT_PATTERNS)
    )
    for scenario in SCENARIOS.values():
        params = {
            f"pattern_{idx}": pattern
            for idx, pattern in enumerate(MANAGEMENT_FACT_PATTERNS)
        }
        params["scenario_id"] = scenario["id"]
        bind.execute(
            sa.text(
                f"""
                DELETE FROM scenario_canon_entries
                WHERE scenario_id = :scenario_id
                  AND (
                    category IN ('管理', '移行', 'RAG', '分離', '正史', '参考', '範囲')
                    OR {management_where}
                  )
                """
            ),
            params,
        )


def downgrade() -> None:
    # The previous minimal descriptions and management notes are intentionally not restored.
    pass
