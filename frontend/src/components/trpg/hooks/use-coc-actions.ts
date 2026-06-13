"use client";

import {
  useCallback,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  COC_CORE_WEAPONS,
  COC_KEY_SKILLS,
  checkedCocSkillNames,
  intValue,
  isRecord,
  py,
  type CocActionResult,
  type CocPostSessionResult,
  type Participant,
  type Room,
} from "@/lib/trpg-room-utils";

// CoC ルーム操作（技能判定・リソース・戦闘・呪文・後処理など）の state とハンドラをまとめたカスタムフック
export function useCocActions({
  room,
  setRoom,
  myParticipantId,
  myCocState,
  diceDifficulty,
  diceNote,
}: {
  room: Room | null;
  setRoom: Dispatch<SetStateAction<Room | null>>;
  myParticipantId: string;
  myCocState: Record<string, unknown> | null;
  diceDifficulty: "regular" | "hard" | "extreme";
  diceNote: string;
}) {
  const [cocBusy, setCocBusy] = useState(false);
  const [cocSelectedSkill, setCocSelectedSkill] = useState("");
  const [cocResourceAmount, setCocResourceAmount] = useState("1");
  const [cocResourceReason, setCocResourceReason] = useState("");
  const [cocResistanceActive, setCocResistanceActive] = useState("10");
  const [cocResistancePassive, setCocResistancePassive] = useState("10");
  const [cocResistanceNote, setCocResistanceNote] = useState("");
  const [cocDevelopmentSkill, setCocDevelopmentSkill] = useState("");
  const [cocCombatWeapon, setCocCombatWeapon] = useState("こぶし");
  const [cocDefenderId, setCocDefenderId] = useState("");
  const [cocDefenseType, setCocDefenseType] = useState("回避");
  const [cocSpellName, setCocSpellName] = useState("");
  const [cocSpellCosts, setCocSpellCosts] = useState({
    mp: "0",
    san: "0",
    hp: "0",
    pow: "0",
  });
  const [cocInsanityKind, setCocInsanityKind] = useState<"temporary" | "indefinite">("temporary");
  const [cocInsanityReason, setCocInsanityReason] = useState("");
  const [cocPostSessionSanExpression, setCocPostSessionSanExpression] = useState("");
  const [cocPostSessionOutcome, setCocPostSessionOutcome] = useState("生還");
  const [cocPostSessionBusy, setCocPostSessionBusy] = useState(false);

  const cocSkillMap = useMemo(
    () =>
      isRecord(myCocState?.skills)
        ? (myCocState.skills as Record<string, unknown>)
        : {},
    [myCocState],
  );
  const cocSkillNames = useMemo(() => {
    const names = Object.keys(cocSkillMap);
    const pinned = COC_KEY_SKILLS.filter((skill) => names.includes(skill));
    return [...pinned, ...names.filter((name) => !pinned.includes(name as typeof COC_KEY_SKILLS[number])).sort()];
  }, [cocSkillMap]);
  const selectedCocSkill = cocSelectedSkill || cocSkillNames[0] || "";
  const selectedDevelopmentSkill = cocDevelopmentSkill || selectedCocSkill;
  const cocCheckedSkills = useMemo(
    () => checkedCocSkillNames(myCocState),
    [myCocState],
  );
  const cocWeaponNames = useMemo(() => {
    const weaponNames = new Set<string>(COC_CORE_WEAPONS);
    const weapons = Array.isArray(myCocState?.weapons) ? myCocState.weapons : [];
    for (const weapon of weapons) {
      if (isRecord(weapon) && typeof weapon.name === "string" && weapon.name.trim()) {
        weaponNames.add(weapon.name.trim());
      }
    }
    return Array.from(weaponNames);
  }, [myCocState]);

  const mergeCocResult = useCallback((result: CocActionResult) => {
    setRoom((prev) => {
      if (!prev) return prev;
      let participants = prev.participants;
      const updateParticipant = (participant?: Participant | null) => {
        if (!participant) return;
        const idx = participants.findIndex((p) => p.id === participant.id);
        if (idx >= 0) {
          participants = participants.map((p) =>
            p.id === participant.id ? participant : p,
          );
        } else {
          participants = [...participants, participant];
        }
      };
      updateParticipant(result.participant);
      updateParticipant(result.defender);
      const logs = result.log && !prev.logs.some((log) => log.id === result.log?.id)
        ? [...prev.logs, result.log]
        : prev.logs;
      return { ...prev, participants, logs };
    });
  }, [setRoom]);

  const mergeCocPostSessionResult = useCallback((result: CocPostSessionResult) => {
    if (result.room) {
      setRoom(result.room);
      return;
    }
    setRoom((prev) => {
      if (!prev) return prev;
      let participants = prev.participants;
      for (const participant of result.participants ?? []) {
        const idx = participants.findIndex((p) => p.id === participant.id);
        participants = idx >= 0
          ? participants.map((p) => p.id === participant.id ? participant : p)
          : [...participants, participant];
      }
      let logs = prev.logs;
      for (const log of result.logs ?? []) {
        if (!logs.some((item) => item.id === log.id)) {
          logs = [...logs, log];
        }
      }
      return { ...prev, participants, logs };
    });
  }, [setRoom]);

  const runCocAction = useCallback(
    async (path: string, body: Record<string, unknown>) => {
      if (!room || !myParticipantId) return;
      setCocBusy(true);
      try {
        const result = await py<CocActionResult>(
          `/api/trpg/rooms/${room.id}/coc/${path}`,
          {
            method: "POST",
            body: JSON.stringify(body),
          },
        );
        mergeCocResult(result);
      } catch (e) {
        console.error(e);
        alert("CoC処理に失敗しました");
      } finally {
        setCocBusy(false);
      }
    },
    [mergeCocResult, myParticipantId, room],
  );

  const handleCocSkillCheck = useCallback(
    async (skillName?: string) => {
      const skill = skillName || selectedCocSkill;
      if (!skill) return;
      await runCocAction("skill-check", {
        participant_id: myParticipantId,
        skill,
        difficulty: diceDifficulty,
        note: diceNote,
        mark_experience: true,
      });
    },
    [diceDifficulty, diceNote, myParticipantId, runCocAction, selectedCocSkill],
  );

  const handleCocResource = useCallback(
    async (resource: "hp" | "mp" | "san", operation: string, amountOverride?: number) => {
      const amount = Math.max(0, intValue(amountOverride ?? cocResourceAmount, 0));
      if (amount <= 0) return;
      await runCocAction("resource", {
        participant_id: myParticipantId,
        resource,
        operation,
        amount,
        reason: cocResourceReason,
      });
    },
    [cocResourceAmount, cocResourceReason, myParticipantId, runCocAction],
  );

  const handleCocResistance = useCallback(async () => {
    await runCocAction("resistance", {
      participant_id: myParticipantId,
      active_value: intValue(cocResistanceActive, 10),
      passive_value: intValue(cocResistancePassive, 10),
      note: cocResistanceNote,
    });
  }, [
    cocResistanceActive,
    cocResistanceNote,
    cocResistancePassive,
    myParticipantId,
    runCocAction,
  ]);

  const handleCocDevelopment = useCallback(async () => {
    if (!selectedDevelopmentSkill) return;
    await runCocAction("development", {
      participant_id: myParticipantId,
      skill: selectedDevelopmentSkill,
    });
  }, [myParticipantId, runCocAction, selectedDevelopmentSkill]);

  const handleCocPostSession = useCallback(async () => {
    if (!room || !myParticipantId) return;
    setCocPostSessionBusy(true);
    try {
      const result = await py<CocPostSessionResult>(
        `/api/trpg/rooms/${room.id}/coc/post-session`,
        {
          method: "POST",
          body: JSON.stringify({
            participant_ids: [myParticipantId],
            sanity_recovery_expression: cocPostSessionSanExpression.trim(),
            outcome: cocPostSessionOutcome.trim(),
            close_room: false,
          }),
        },
      );
      mergeCocPostSessionResult(result);
    } catch (e) {
      console.error(e);
      alert("セッション後処理に失敗しました");
    } finally {
      setCocPostSessionBusy(false);
    }
  }, [
    cocPostSessionOutcome,
    cocPostSessionSanExpression,
    mergeCocPostSessionResult,
    myParticipantId,
    room,
  ]);

  const handleCocAttack = useCallback(async () => {
    await runCocAction("attack", {
      attacker_id: myParticipantId,
      defender_id: cocDefenderId || null,
      weapon: cocCombatWeapon,
      defense_type: cocDefenseType,
      note: diceNote,
    });
  }, [
    cocCombatWeapon,
    cocDefenderId,
    cocDefenseType,
    diceNote,
    myParticipantId,
    runCocAction,
  ]);

  const handleCocSpellCost = useCallback(async () => {
    if (!cocSpellName.trim()) return;
    await runCocAction("spell-cost", {
      participant_id: myParticipantId,
      spell_name: cocSpellName.trim(),
      mp_cost: intValue(cocSpellCosts.mp, 0),
      san_cost: intValue(cocSpellCosts.san, 0),
      hp_cost: intValue(cocSpellCosts.hp, 0),
      pow_cost: intValue(cocSpellCosts.pow, 0),
    });
  }, [cocSpellCosts, cocSpellName, myParticipantId, runCocAction]);

  const handleCocInsanity = useCallback(async () => {
    await runCocAction("insanity", {
      participant_id: myParticipantId,
      kind: cocInsanityKind,
      reason: cocInsanityReason,
    });
  }, [cocInsanityKind, cocInsanityReason, myParticipantId, runCocAction]);

  const handleUpdateMyPcState = useCallback(
    async (updates: Record<string, unknown>) => {
      if (!room || !myParticipantId) return;
      const participant = await py<Participant>(
        `/api/trpg/participants/${myParticipantId}`,
        {
          method: "PUT",
          body: JSON.stringify({ pc_state: updates }),
        },
      );
      setRoom((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          participants: prev.participants.map((p) =>
            p.id === participant.id ? participant : p,
          ),
        };
      });
    },
    [room, myParticipantId, setRoom],
  );

  const handleCocResourceStep = useCallback(
    async (field: "hp" | "mp" | "sanity" | "luck", delta: number) => {
      if (!myCocState) return;
      const maxField = field === "hp"
        ? "max_hp"
        : field === "mp"
          ? "max_mp"
          : field === "sanity"
            ? "max_sanity"
            : "";
      const current = intValue(myCocState[field], 0);
      const max = maxField ? intValue(myCocState[maxField], 100) : 100;
      await handleUpdateMyPcState({
        [field]: Math.max(0, Math.min(max, current + delta)),
      });
    },
    [handleUpdateMyPcState, myCocState],
  );

  return {
    cocBusy,
    cocSkillMap,
    cocSkillNames,
    selectedCocSkill,
    setCocSelectedSkill,
    selectedDevelopmentSkill,
    setCocDevelopmentSkill,
    cocCheckedSkills,
    cocWeaponNames,
    cocResourceAmount,
    setCocResourceAmount,
    cocResourceReason,
    setCocResourceReason,
    cocResistanceActive,
    setCocResistanceActive,
    cocResistancePassive,
    setCocResistancePassive,
    cocResistanceNote,
    setCocResistanceNote,
    cocCombatWeapon,
    setCocCombatWeapon,
    cocDefenderId,
    setCocDefenderId,
    cocDefenseType,
    setCocDefenseType,
    cocSpellName,
    setCocSpellName,
    cocSpellCosts,
    setCocSpellCosts,
    cocInsanityKind,
    setCocInsanityKind,
    cocInsanityReason,
    setCocInsanityReason,
    cocPostSessionSanExpression,
    setCocPostSessionSanExpression,
    cocPostSessionOutcome,
    setCocPostSessionOutcome,
    cocPostSessionBusy,
    handleCocSkillCheck,
    handleCocResource,
    handleCocResistance,
    handleCocDevelopment,
    handleCocPostSession,
    handleCocAttack,
    handleCocSpellCost,
    handleCocInsanity,
    handleCocResourceStep,
  };
}

export type CocActions = ReturnType<typeof useCocActions>;
