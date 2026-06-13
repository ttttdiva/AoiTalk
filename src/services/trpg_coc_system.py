"""CoC runtime mechanics used by TRPG rooms.

This module contains executable mechanics only. Rulebook prose remains in
user-provided DB documents and is supplied to the AI GM as retrieved context.
"""

from __future__ import annotations

import math
import random
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from .trpg_rules import evaluate_coc6_d100


COC_CONDITION_UNCONSCIOUS = "意識不明"
COC_CONDITION_DEAD = "死亡"
COC_CONDITION_STUNNED = "スタン"
COC_CONDITION_MAJOR_WOUND = "重傷"
COC_CONDITION_TEMPORARY_INSANITY = "一時的狂気"
COC_CONDITION_INDEFINITE_INSANITY = "不定の狂気"
COC_CONDITION_PERMANENT_INSANITY = "永久的狂気"

_DICE_PATTERN = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+)\s*([+-]\s*\d+)?\s*$")
_DICE_TOKEN_RE = re.compile(r"([+-]?)\s*(?:(\d*)\s*[dD]\s*(\d+)|(\d+))", re.I)


CORE_WEAPON_PROFILES: Dict[str, Dict[str, Any]] = {
    "こぶし": {"name": "こぶし", "skill": "こぶし（パンチ）", "damage": "1d3", "damage_bonus": True, "range": "melee"},
    "パンチ": {"name": "パンチ", "skill": "こぶし（パンチ）", "damage": "1d3", "damage_bonus": True, "range": "melee"},
    "キック": {"name": "キック", "skill": "キック", "damage": "1d6", "damage_bonus": True, "range": "melee"},
    "頭突き": {"name": "頭突き", "skill": "頭突き", "damage": "1d4", "damage_bonus": True, "range": "melee"},
    "組み付き": {"name": "組み付き", "skill": "組み付き", "damage": "special", "damage_bonus": False, "range": "melee"},
    "ナイフ": {"name": "ナイフ", "skill": "ナイフ", "damage": "1d4", "damage_bonus": True, "range": "melee", "impale": True},
    "小型棍棒": {"name": "小型棍棒", "skill": "杖", "damage": "1d6", "damage_bonus": True, "range": "melee"},
    "大型棍棒": {"name": "大型棍棒", "skill": "杖", "damage": "1d8", "damage_bonus": True, "range": "melee"},
    "拳銃": {"name": "拳銃", "skill": "拳銃", "damage": "1d10", "damage_bonus": False, "range": "firearm", "impale": True},
    "ライフル": {"name": "ライフル", "skill": "ライフル", "damage": "2d6", "damage_bonus": False, "range": "firearm", "impale": True},
    "ショットガン": {"name": "ショットガン", "skill": "ショットガン", "damage": "4d6", "damage_bonus": False, "range": "firearm", "impale": True},
    "投擲": {"name": "投擲", "skill": "投擲", "damage": "1d4", "damage_bonus": True, "range": "thrown"},
}


GENERIC_INSANITY_EFFECTS = {
    "temporary": [
        "逃走",
        "硬直",
        "叫び",
        "攻撃衝動",
        "妄想",
        "幻覚",
        "反復行動",
        "記憶混乱",
    ],
    "indefinite": [
        "恐怖症",
        "強迫観念",
        "偏執",
        "解離",
        "悪夢",
        "暴力衝動",
        "依存",
        "失語/失認",
    ],
}


def roll_coc_dice_expression(expression: str) -> Dict[str, Any]:
    """Roll a dice expression with additive terms, e.g. 1d6+1+2d4."""
    expr = str(expression or "0").replace(" ", "")
    if not expr:
        expr = "0"
    total = 0
    rolls: List[Dict[str, Any]] = []
    pos = 0
    for match in _DICE_TOKEN_RE.finditer(expr):
        if match.start() != pos:
            raise ValueError(f"Unsupported dice expression: {expression}")
        pos = match.end()
        sign = -1 if match.group(1) == "-" else 1
        if match.group(3):
            count = int(match.group(2) or 1)
            faces = int(match.group(3))
            if count < 1 or count > 100 or faces < 1 or faces > 10000:
                raise ValueError(f"Invalid dice term: {match.group(0)}")
            values = [random.randint(1, faces) for _ in range(count)]
            subtotal = sum(values) * sign
            rolls.append({"count": count, "faces": faces, "rolls": values, "sign": sign, "subtotal": subtotal})
            total += subtotal
        else:
            value = int(match.group(4)) * sign
            rolls.append({"static": abs(value), "sign": sign, "subtotal": value})
            total += value
    if pos != len(expr):
        raise ValueError(f"Unsupported dice expression: {expression}")
    return {"expression": expression, "terms": rolls, "total": total}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _conditions(state: Dict[str, Any]) -> List[str]:
    conds = state.get("conditions")
    if not isinstance(conds, list):
        conds = []
    return [str(item) for item in conds if str(item)]


def set_condition(state: Dict[str, Any], condition: str, active: bool = True) -> Dict[str, Any]:
    next_state = deepcopy(state)
    conds = _conditions(next_state)
    if active and condition not in conds:
        conds.append(condition)
    if not active:
        conds = [item for item in conds if item != condition]
    next_state["conditions"] = conds
    return rebuild_coc_state_runtime(next_state)


def calculate_coc6_damage_bonus(str_value: int, siz_value: int) -> str:
    total = int(str_value) + int(siz_value)
    if total <= 12:
        return "-1d6"
    if total <= 16:
        return "-1d4"
    if total <= 24:
        return "0"
    if total <= 32:
        return "+1d4"
    if total <= 40:
        return "+1d6"
    extra = 2 + max(0, math.ceil((total - 56) / 16))
    if total <= 56:
        extra = 2
    return f"+{extra}d6"


def damage_bonus_roll_expression(damage_bonus: str) -> str:
    value = str(damage_bonus or "0").strip()
    if value in {"", "0", "+0", "-0"}:
        return "0"
    return value


def rebuild_coc_state_runtime(state: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild derived runtime fields without reparsing the whole sheet."""
    next_state = deepcopy(state)
    chars = next_state.get("characteristics") if isinstance(next_state.get("characteristics"), dict) else {}
    skills = next_state.get("skills") if isinstance(next_state.get("skills"), dict) else {}
    mythos = _clamp(_to_int(skills.get("クトゥルフ神話"), 0), 0, 99)
    next_state["max_sanity"] = min(_to_int(next_state.get("max_sanity"), 99), max(0, 99 - mythos))
    next_state["sanity"] = _clamp(_to_int(next_state.get("sanity"), 0), 0, next_state["max_sanity"])
    next_state["san"] = next_state["sanity"]
    if chars:
        next_state["damage_bonus"] = calculate_coc6_damage_bonus(
            _to_int(chars.get("STR"), 10),
            _to_int(chars.get("SIZ"), 10),
        )
    next_state["hp"] = _clamp(_to_int(next_state.get("hp"), 0), -99, _to_int(next_state.get("max_hp"), 1))
    next_state["mp"] = _clamp(_to_int(next_state.get("mp"), 0), 0, _to_int(next_state.get("max_mp"), 99))
    return next_state


def coc6_resistance_target(active: int, passive: int) -> int:
    return _clamp(50 + (int(active) - int(passive)) * 5, 5, 95)


def evaluate_coc6_resistance(total: int, active: int, passive: int) -> Dict[str, Any]:
    target = coc6_resistance_target(active, passive)
    details = evaluate_coc6_d100(total=total, target=target)
    details.update({"check_type": "resistance", "active": int(active), "passive": int(passive)})
    return details


def mark_coc_skill_experience(state: Dict[str, Any], skill_name: str) -> Dict[str, Any]:
    next_state = rebuild_coc_state_runtime(state)
    skill = str(skill_name or "").strip()
    if not skill or skill == "クトゥルフ神話":
        return next_state
    skills = next_state.get("skills") if isinstance(next_state.get("skills"), dict) else {}
    if skill not in skills:
        return next_state
    checks = next_state.get("skill_checks") if isinstance(next_state.get("skill_checks"), dict) else {}
    checks[skill] = True
    next_state["skill_checks"] = checks
    return next_state


def apply_coc_skill_development(
    state: Dict[str, Any],
    skill_name: str,
    development_roll_total: int,
    gain: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    skill = str(skill_name or "").strip()
    skills = next_state.get("skills") if isinstance(next_state.get("skills"), dict) else {}
    current = _clamp(_to_int(skills.get(skill), 0), 0, 99)
    improved = int(development_roll_total) > current
    gain_roll = None
    if improved:
        if gain is None:
            gain_roll = roll_coc_dice_expression("1d10")
            gain = int(gain_roll["total"])
        skills[skill] = _clamp(current + int(gain), 0, 99)
    checks = next_state.get("skill_checks") if isinstance(next_state.get("skill_checks"), dict) else {}
    checks[skill] = False
    next_state["skills"] = skills
    next_state["skill_checks"] = checks
    return rebuild_coc_state_runtime(next_state), {
        "skill": skill,
        "before": current,
        "after": skills.get(skill, current),
        "roll": int(development_roll_total),
        "improved": improved,
        "gain": int(gain or 0),
        "gain_roll": gain_roll,
    }


def checked_coc_development_skills(state: Dict[str, Any]) -> List[str]:
    """Return skills marked for post-session CoC development checks."""
    next_state = rebuild_coc_state_runtime(state)
    skills = next_state.get("skills") if isinstance(next_state.get("skills"), dict) else {}
    checks = next_state.get("skill_checks") if isinstance(next_state.get("skill_checks"), dict) else {}
    checked = [
        str(skill)
        for skill, marked in checks.items()
        if bool(marked) and str(skill) in skills and str(skill) != "クトゥルフ神話"
    ]
    return sorted(checked)


def apply_coc_checked_skill_development(
    state: Dict[str, Any],
    development_rolls: Optional[Dict[str, int]] = None,
    gains: Optional[Dict[str, int]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Apply CoC6 post-session development to all checked skills."""
    next_state = rebuild_coc_state_runtime(state)
    results: List[Dict[str, Any]] = []
    for skill in checked_coc_development_skills(next_state):
        roll_total = (
            int(development_rolls[skill])
            if development_rolls and skill in development_rolls
            else int(roll_coc_dice_expression("1d100")["total"])
        )
        gain = int(gains[skill]) if gains and skill in gains else None
        next_state, result = apply_coc_skill_development(
            next_state,
            skill,
            development_roll_total=roll_total,
            gain=gain,
        )
        results.append(result)
    return rebuild_coc_state_runtime(next_state), results


def apply_coc_damage(
    state: Dict[str, Any],
    amount: int,
    source: str = "",
    allow_death: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    damage = max(0, int(amount))
    before = _to_int(next_state.get("hp"), 0)
    max_hp = max(1, _to_int(next_state.get("max_hp"), 1))
    after = before - damage
    next_state["hp"] = after
    conds = _conditions(next_state)
    major_wound = damage >= math.ceil(max_hp / 2)
    if major_wound and COC_CONDITION_MAJOR_WOUND not in conds:
        conds.append(COC_CONDITION_MAJOR_WOUND)
    if after <= 2 and COC_CONDITION_UNCONSCIOUS not in conds:
        conds.append(COC_CONDITION_UNCONSCIOUS)
    if allow_death and after <= 0 and COC_CONDITION_DEAD not in conds:
        conds.append(COC_CONDITION_DEAD)
    next_state["conditions"] = conds
    return rebuild_coc_state_runtime(next_state), {
        "type": "damage",
        "source": source,
        "amount": damage,
        "before": before,
        "after": after,
        "major_wound": major_wound,
        "dead": COC_CONDITION_DEAD in conds,
        "unconscious": COC_CONDITION_UNCONSCIOUS in conds,
    }


def heal_coc_hp(state: Dict[str, Any], amount: int, source: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    healing = max(0, int(amount))
    before = _to_int(next_state.get("hp"), 0)
    max_hp = _to_int(next_state.get("max_hp"), 1)
    after = min(max_hp, before + healing)
    next_state["hp"] = after
    conds = _conditions(next_state)
    if after > 2:
        conds = [c for c in conds if c != COC_CONDITION_UNCONSCIOUS]
    if after > 0:
        conds = [c for c in conds if c != COC_CONDITION_DEAD]
    next_state["conditions"] = conds
    return rebuild_coc_state_runtime(next_state), {
        "type": "heal",
        "source": source,
        "amount": healing,
        "before": before,
        "after": after,
    }


def spend_coc_mp(state: Dict[str, Any], amount: int, source: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    cost = max(0, int(amount))
    before = _to_int(next_state.get("mp"), 0)
    after = max(0, before - cost)
    next_state["mp"] = after
    conds = _conditions(next_state)
    if after <= 0 and COC_CONDITION_UNCONSCIOUS not in conds:
        conds.append(COC_CONDITION_UNCONSCIOUS)
    next_state["conditions"] = conds
    return rebuild_coc_state_runtime(next_state), {
        "type": "mp_cost",
        "source": source,
        "amount": cost,
        "before": before,
        "after": after,
    }


def recover_coc_mp(state: Dict[str, Any], amount: int, source: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    value = max(0, int(amount))
    before = _to_int(next_state.get("mp"), 0)
    max_mp = _to_int(next_state.get("max_mp"), 1)
    after = min(max_mp, before + value)
    next_state["mp"] = after
    return rebuild_coc_state_runtime(next_state), {
        "type": "mp_recover",
        "source": source,
        "amount": value,
        "before": before,
        "after": after,
    }


def apply_coc_sanity_loss(
    state: Dict[str, Any],
    amount: int,
    source: str = "",
    window_key: str = "session",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    loss = max(0, int(amount))
    before = _to_int(next_state.get("sanity"), 0)
    after = max(0, before - loss)
    next_state["sanity"] = after
    next_state["san"] = after

    loss_windows = next_state.get("sanity_loss_windows") if isinstance(next_state.get("sanity_loss_windows"), dict) else {}
    previous_window_loss = _to_int(loss_windows.get(window_key), 0)
    loss_windows[window_key] = previous_window_loss + loss
    next_state["sanity_loss_windows"] = loss_windows

    conds = _conditions(next_state)
    temporary = loss >= 5
    threshold_base = max(1, before)
    indefinite = loss_windows[window_key] >= math.ceil(threshold_base / 5)
    if temporary and COC_CONDITION_TEMPORARY_INSANITY not in conds:
        conds.append(COC_CONDITION_TEMPORARY_INSANITY)
    if indefinite and COC_CONDITION_INDEFINITE_INSANITY not in conds:
        conds.append(COC_CONDITION_INDEFINITE_INSANITY)
    if after <= 0 and COC_CONDITION_PERMANENT_INSANITY not in conds:
        conds.append(COC_CONDITION_PERMANENT_INSANITY)
    next_state["conditions"] = conds
    return rebuild_coc_state_runtime(next_state), {
        "type": "sanity_loss",
        "source": source,
        "amount": loss,
        "before": before,
        "after": after,
        "temporary_insanity": temporary,
        "indefinite_insanity": indefinite,
        "permanent_insanity": after <= 0,
        "window_key": window_key,
        "window_loss": loss_windows[window_key],
    }


def apply_coc_sanity_recovery(
    state: Dict[str, Any],
    amount: int,
    source: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    recovery = max(0, int(amount))
    before = _to_int(next_state.get("sanity"), 0)
    after = min(_to_int(next_state.get("max_sanity"), 99), before + recovery)
    next_state["sanity"] = after
    next_state["san"] = after
    return rebuild_coc_state_runtime(next_state), {
        "type": "sanity_recovery",
        "source": source,
        "amount": recovery,
        "before": before,
        "after": after,
    }


def roll_coc_insanity_effect(kind: str = "temporary") -> Dict[str, Any]:
    key = "indefinite" if str(kind).lower().startswith("ind") or "不定" in str(kind) else "temporary"
    table = GENERIC_INSANITY_EFFECTS[key]
    index = random.randint(1, len(table))
    return {"kind": key, "roll": index, "effect": table[index - 1]}


def find_coc_weapon(state: Dict[str, Any], weapon_name: str) -> Dict[str, Any]:
    name = str(weapon_name or "").strip()
    weapons = state.get("weapons") if isinstance(state.get("weapons"), list) else []
    for raw in weapons:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("name") or "").strip() == name:
            merged = {**CORE_WEAPON_PROFILES.get(name, {}), **raw}
            merged.setdefault("skill", name)
            merged.setdefault("damage", "1d3")
            merged.setdefault("damage_bonus", False)
            return merged
    if name in CORE_WEAPON_PROFILES:
        return deepcopy(CORE_WEAPON_PROFILES[name])
    return {"name": name or "こぶし", "skill": name or "こぶし（パンチ）", "damage": "1d3", "damage_bonus": True, "range": "melee"}


def build_damage_expression(base_damage: str, damage_bonus: str = "0", include_bonus: bool = False) -> str:
    expr = str(base_damage or "0").strip()
    if expr == "special":
        return "0"
    if include_bonus:
        bonus = damage_bonus_roll_expression(damage_bonus)
        if bonus not in {"0", "+0", "-0"}:
            if bonus.startswith("-") or bonus.startswith("+"):
                expr += bonus
            else:
                expr += "+" + bonus
    return expr


def resolve_coc_attack(
    attacker_state: Dict[str, Any],
    weapon_name: str,
    attack_total: int,
    defender_state: Optional[Dict[str, Any]] = None,
    defense_total: Optional[int] = None,
    defense_type: str = "回避",
    damage_roll: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    attacker = rebuild_coc_state_runtime(attacker_state)
    defender = rebuild_coc_state_runtime(defender_state or {}) if defender_state else None
    weapon = find_coc_weapon(attacker, weapon_name)
    skills = attacker.get("skills") if isinstance(attacker.get("skills"), dict) else {}
    skill_name = str(weapon.get("skill") or weapon.get("name") or weapon_name)
    attack_target = _clamp(_to_int(skills.get(skill_name), 0), 0, 100)
    attack = evaluate_coc6_d100(total=int(attack_total), target=attack_target)

    defense: Optional[Dict[str, Any]] = None
    defended = False
    if defender and defense_total is not None:
        def_skills = defender.get("skills") if isinstance(defender.get("skills"), dict) else {}
        defense_target = _clamp(_to_int(def_skills.get(defense_type), 0), 0, 100)
        defense = evaluate_coc6_d100(total=int(defense_total), target=defense_target)
        defended = bool(defense.get("success"))

    result: Dict[str, Any] = {
        "type": "attack",
        "weapon": weapon,
        "skill": skill_name,
        "attack": attack,
        "defense_type": defense_type,
        "defense": defense,
        "hit": bool(attack.get("success")) and not defended,
        "damage": None,
    }

    updated_defender = defender
    if result["hit"] and defender is not None:
        if damage_roll is None:
            damage_expr = build_damage_expression(
                str(weapon.get("damage") or "0"),
                str(attacker.get("damage_bonus") or "0"),
                bool(weapon.get("damage_bonus")),
            )
            damage_roll = roll_coc_dice_expression(damage_expr)
        updated_defender, damage_result = apply_coc_damage(
            defender,
            max(0, int(damage_roll.get("total", 0))),
            source=str(weapon.get("name") or weapon_name),
        )
        result["damage"] = {"roll": damage_roll, "result": damage_result}
    return updated_defender, result


def apply_coc_spell_cost(
    state: Dict[str, Any],
    spell_name: str,
    mp_cost: int = 0,
    san_cost: int = 0,
    hp_cost: int = 0,
    pow_cost: int = 0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    next_state = rebuild_coc_state_runtime(state)
    events: List[Dict[str, Any]] = []
    if mp_cost:
        next_state, event = spend_coc_mp(next_state, mp_cost, source=spell_name)
        events.append(event)
    if san_cost:
        next_state, event = apply_coc_sanity_loss(next_state, san_cost, source=spell_name)
        events.append(event)
    if hp_cost:
        next_state, event = apply_coc_damage(next_state, hp_cost, source=spell_name, allow_death=True)
        events.append(event)
    if pow_cost:
        chars = next_state.get("characteristics") if isinstance(next_state.get("characteristics"), dict) else {}
        before = _to_int(chars.get("POW"), 0)
        chars["POW"] = max(0, before - max(0, int(pow_cost)))
        next_state["characteristics"] = chars
        events.append({"type": "pow_cost", "source": spell_name, "amount": pow_cost, "before": before, "after": chars["POW"]})
    cast_log = next_state.get("spell_casts") if isinstance(next_state.get("spell_casts"), list) else []
    cast_log.append({"spell": spell_name, "costs": events})
    next_state["spell_casts"] = cast_log
    return rebuild_coc_state_runtime(next_state), {"type": "spell_cost", "spell": spell_name, "events": events}


def create_coc_creature_state(raw: Dict[str, Any]) -> Dict[str, Any]:
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else raw
    chars = {
        key: _to_int(stats.get(key), 10)
        for key in ("STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU")
    }
    hp = _to_int(raw.get("hp"), max(1, math.ceil((chars["CON"] + chars["SIZ"]) / 2)))
    mp = _to_int(raw.get("mp"), max(0, chars["POW"]))
    state = {
        "sheet_format": "coc_creature_v1",
        "ruleset": "coc6",
        "name": str(raw.get("name") or "怪物"),
        "characteristics": chars,
        "stats": {key: _clamp(value * 5, 0, 100) for key, value in chars.items()},
        "hp": hp,
        "max_hp": _to_int(raw.get("max_hp"), hp),
        "mp": mp,
        "max_mp": _to_int(raw.get("max_mp"), mp),
        "san_loss": raw.get("san_loss") or raw.get("sanity_loss") or "",
        "attacks": raw.get("attacks") if isinstance(raw.get("attacks"), list) else [],
        "armor": raw.get("armor") or 0,
        "movement": raw.get("movement") or raw.get("move") or "",
        "special": raw.get("special") or "",
        "conditions": raw.get("conditions") if isinstance(raw.get("conditions"), list) else [],
    }
    state["damage_bonus"] = calculate_coc6_damage_bonus(chars["STR"], chars["SIZ"])
    return state
