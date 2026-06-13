"use client";

import { useCallback, useState } from "react";
import type { Scenario } from "@/lib/scenarios-page-utils";

// シナリオ編集ダイアログのフォーム state 群（基本情報 + 文体設定）をまとめたカスタムフック
export function useScenarioForm() {
  // Form state
  const [formTitle, setFormTitle] = useState("");
  const [formScenarioKind, setFormScenarioKind] = useState<"writing" | "trpg">("writing");
  const [formRuleset, setFormRuleset] = useState("generic");
  const [formDescription, setFormDescription] = useState("");
  const [formGenre, setFormGenre] = useState("fantasy");
  const [formPerspective, setFormPerspective] = useState<
    "first_person" | "third_person"
  >("first_person");
  const [formSetting, setFormSetting] = useState("");
  const [formOpeningText, setFormOpeningText] = useState("");
  const [formTags, setFormTags] = useState("");
  const [formDifficulty, setFormDifficulty] = useState("normal");

  // Voice form state
  const [voiceTone, setVoiceTone] = useState("");
  const [voiceTenseRules, setVoiceTenseRules] = useState("");
  const [voiceVocabulary, setVoiceVocabulary] = useState("");
  const [voiceBannedExpressions, setVoiceBannedExpressions] = useState("");
  const [voiceExamplePassages, setVoiceExamplePassages] = useState("");
  const [voiceExpanded, setVoiceExpanded] = useState(false);

  // 既存シナリオの値をフォームへ反映する
  const populateFromScenario = useCallback(
    (scenario: Scenario & Record<string, unknown>) => {
      setFormTitle(scenario.title);
      setFormScenarioKind(scenario.scenario_kind === "trpg" ? "trpg" : "writing");
      setFormRuleset(scenario.ruleset || "generic");
      setFormDescription(scenario.description);
      setFormGenre(scenario.genre);
      setFormPerspective(scenario.perspective);
      setFormSetting(scenario.setting);
      setFormOpeningText(scenario.opening_text);
      setFormTags(scenario.tags?.join(", ") ?? "");
      setFormDifficulty(scenario.difficulty);
      setVoiceTone((scenario.voice_tone as string) ?? "");
      setVoiceTenseRules((scenario.voice_tense_rules as string) ?? "");
      setVoiceVocabulary(
        (scenario.voice_vocabulary_register as string) ?? "",
      );
      setVoiceBannedExpressions(
        Array.isArray(scenario.voice_banned_expressions)
          ? (scenario.voice_banned_expressions as string[]).join(", ")
          : "",
      );
      setVoiceExamplePassages(
        (scenario.voice_example_passages as string) ?? "",
      );
    },
    [],
  );

  // 新規作成用にフォームを初期値へ戻す
  const resetToDefaults = useCallback(() => {
    setFormTitle("");
    setFormScenarioKind("writing");
    setFormRuleset("generic");
    setFormDescription("");
    setFormGenre("fantasy");
    setFormPerspective("first_person");
    setFormSetting("");
    setFormOpeningText("");
    setFormTags("");
    setFormDifficulty("normal");
    setVoiceTone("");
    setVoiceTenseRules("");
    setVoiceVocabulary("");
    setVoiceBannedExpressions("");
    setVoiceExamplePassages("");
  }, []);

  return {
    formTitle,
    setFormTitle,
    formScenarioKind,
    setFormScenarioKind,
    formRuleset,
    setFormRuleset,
    formDescription,
    setFormDescription,
    formGenre,
    setFormGenre,
    formPerspective,
    setFormPerspective,
    formSetting,
    setFormSetting,
    formOpeningText,
    setFormOpeningText,
    formTags,
    setFormTags,
    formDifficulty,
    setFormDifficulty,
    voiceTone,
    setVoiceTone,
    voiceTenseRules,
    setVoiceTenseRules,
    voiceVocabulary,
    setVoiceVocabulary,
    voiceBannedExpressions,
    setVoiceBannedExpressions,
    voiceExamplePassages,
    setVoiceExamplePassages,
    voiceExpanded,
    setVoiceExpanded,
    populateFromScenario,
    resetToDefaults,
  };
}
