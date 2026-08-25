import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, ScrollView, Share, StyleSheet, View } from "react-native";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { goBackOrReplace } from "../../lib/navigation";
import { StoryBodyConflictError, storyApi } from "../../lib/story-api";
import type { StoryWorkCharacterInput, StoryWorkRulebookInput } from "../../lib/story-api";
import { storyRepo } from "../../repositories/story";
import {
  Button,
  Chip,
  Dialog,
  IconButton,
  Portal,
  ProgressBar,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import type {
  StoryCharacter,
  StoryEpisode,
  StoryGraph,
  StoryJob,
  StoryEpisodeRevision,
  StoryLocalDraft,
  StoryNote,
  StoryOverview,
  StoryRulebook,
  StoryContextPreview,
  StorySearchResponse,
  StoryWork,
} from "../../types/api";

type SectionKey = "episodes" | "map" | "review" | "library" | "settings" | "characters" | "rulebooks" | "notes" | "revisions" | "jobs";
type AttachedCharacter = StoryCharacter & { character_id?: string; role_note?: string | null; position?: number };
type AttachedRulebook = StoryRulebook & { rulebook_id?: string; enabled?: boolean; position?: number };
type ArchivedEpisode = { episode: StoryEpisode; restoreToken?: Record<string, unknown> | null };
type BodyConflict = {
  localBody: string;
  expectedEtag: string | null;
  serverSnapshot: StoryEpisode | null;
};

const toError = (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback;

function isBodyConflictError(error: unknown): error is StoryBodyConflictError & { serverSnapshot?: StoryEpisode | null } {
  return (typeof StoryBodyConflictError === "function" && error instanceof StoryBodyConflictError) || (error instanceof Error && error.name === "StoryBodyConflictError");
}

/** Canonical Story work editor. */
export default function StoryWorkScreen() {
  const router = useRouter();
  const { workId } = useLocalSearchParams<{ workId?: string }>();
  const [overview, setOverview] = useState<StoryOverview | null>(null);
  const [work, setWork] = useState<StoryWork | null>(null);
  const [graph, setGraph] = useState<StoryGraph | null>(null);
  const [episodes, setEpisodes] = useState<StoryEpisode[]>([]);
  const [archivedEpisodes, setArchivedEpisodes] = useState<ArchivedEpisode[]>([]);
  const [characters, setCharacters] = useState<StoryCharacter[]>([]);
  const [workCharacters, setWorkCharacters] = useState<AttachedCharacter[]>([]);
  const [rulebooks, setRulebooks] = useState<StoryRulebook[]>([]);
  const [workRulebooks, setWorkRulebooks] = useState<AttachedRulebook[]>([]);
  const [notes, setNotes] = useState<StoryNote[]>([]);
  const [section, setSection] = useState<SectionKey>("episodes");
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [bodyDraft, setBodyDraft] = useState("");
  const [bodySaving, setBodySaving] = useState(false);
  const [bodyConflict, setBodyConflict] = useState<BodyConflict | null>(null);
  const [localDraft, setLocalDraft] = useState<StoryLocalDraft | null>(null);
  const [revisions, setRevisions] = useState<StoryEpisodeRevision[]>([]);
  const [revisionDetail, setRevisionDetail] = useState<StoryEpisodeRevision | null>(null);
  const [revisionMessage, setRevisionMessage] = useState("");
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [characterEditorVisible, setCharacterEditorVisible] = useState(false);
  const [characterEditingId, setCharacterEditingId] = useState<string | null>(null);
  const [characterNameDraft, setCharacterNameDraft] = useState("");
  const [characterSummaryDraft, setCharacterSummaryDraft] = useState("");
  const [characterDescriptionDraft, setCharacterDescriptionDraft] = useState("");
  const [rulebookEditorVisible, setRulebookEditorVisible] = useState(false);
  const [rulebookEditingId, setRulebookEditingId] = useState<string | null>(null);
  const [rulebookNameDraft, setRulebookNameDraft] = useState("");
  const [rulebookContentDraft, setRulebookContentDraft] = useState("");
  const [job, setJob] = useState<StoryJob | null>(null);
  const [jobBusy, setJobBusy] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<StorySearchResponse | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [contextPreview, setContextPreview] = useState<StoryContextPreview | null>(null);
  const [contextBusy, setContextBusy] = useState(false);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [splitVisible, setSplitVisible] = useState(false);
  const [splitOffset, setSplitOffset] = useState("");
  const [splitTitle, setSplitTitle] = useState("");
  const [splitBusy, setSplitBusy] = useState(false);
  const [linkEditingId, setLinkEditingId] = useState<string | null>(null);
  const [linkLabelDraft, setLinkLabelDraft] = useState("");
  const [editor, setEditor] = useState<"episode" | "note" | null>(null);
  const [workEditorVisible, setWorkEditorVisible] = useState(false);
  const [workTitleDraft, setWorkTitleDraft] = useState("");
  const [workSynopsisDraft, setWorkSynopsisDraft] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const [plotDraft, setPlotDraft] = useState("");
  const [summaryDraft, setSummaryDraft] = useState("");
  const [statusDraft, setStatusDraft] = useState("unwritten");
  const [noteContentDraft, setNoteContentDraft] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!workId) return;
    setErrorMessage(null);
    try {
      const [nextOverview, nextCharacters, nextWorkCharacters, nextRulebooks, nextWorkRulebooks, nextNotes] = await Promise.all([
        storyApi.getOverview(workId),
        storyApi.listCharacters(),
        storyApi.listWorkCharacters(workId),
        storyApi.listRulebooks(),
        storyApi.listWorkRulebooks(workId),
        storyApi.listNotes(workId),
      ]);
      setOverview(nextOverview);
      setWork(nextOverview.work);
      setGraph(nextOverview.graph);
      setEpisodes(nextOverview.graph.episodes);
      setCharacters(nextCharacters);
      setWorkCharacters(nextWorkCharacters as AttachedCharacter[]);
      setRulebooks(nextRulebooks);
      setWorkRulebooks(nextWorkRulebooks as AttachedRulebook[]);
      setNotes(nextNotes);
      setSelectedEpisodeId((current) => current && nextOverview.graph.episodes.some((item) => item.id === current) ? current : (nextOverview.graph.episodes[0]?.id ?? null));
    } catch (error) {
      setErrorMessage(toError(error, "Storyを読み込めませんでした。"));
    }
  }, [workId]);

  useFocusEffect(useCallback(() => { void load(); }, [load]));

  // The overview intentionally omits manuscript bodies. Fetch the full
  // episode only for the selected editor so opening a work never downloads
  // every chapter body at once.
  useEffect(() => {
    if (!selectedEpisodeId) return;
    let active = true;
    setBodyConflict(null);
    setRevisionDetail(null);
    setRevisions([]);
    void (async () => {
      // Drafts are read before the network body. This is what makes a force
      // close/restart recover an unsent manuscript instead of overwriting it
      // with the graph's metadata-only episode.
      const draft = await storyRepo.getDraft(selectedEpisodeId).catch(() => null);
      if (!active) return;
      setLocalDraft(draft);
      if (draft?.body != null) setBodyDraft(draft.body);
      try {
        const full = await storyApi.getEpisode(selectedEpisodeId);
        if (!active) return;
        setEpisodes((items) => items.map((item) => item.id === full.id ? full : item));
        if (!draft) setBodyDraft(full.body ?? "");
        if (draft?.conflict_status === "conflict") {
          // Preserve the durable conflict metadata while refreshing its body/
          // ETag from the newest reachable server snapshot.
          const snapshot = draft.server_snapshot ? { ...draft.server_snapshot, ...full } : full;
          setBodyConflict({ localBody: draft.body, expectedEtag: draft.expected_etag ?? null, serverSnapshot: snapshot });
        }
      } catch {
        // The graph metadata and recovered draft remain usable offline.
      }
    })();
    return () => { active = false; };
  }, [selectedEpisodeId]);

  const selectedEpisode = useMemo(
    () => episodes.find((episode) => episode.id === selectedEpisodeId) ?? null,
    [episodes, selectedEpisodeId],
  );

  const openEpisodeEditor = (episode?: StoryEpisode) => {
    setEditor("episode");
    setEditingId(episode?.id ?? null);
    setTitleDraft(episode?.title ?? "");
    setPlotDraft(episode?.plot ?? "");
    setSummaryDraft(episode?.summary ?? "");
    setStatusDraft(episode?.status ?? "unwritten");
  };

  const openWorkEditor = () => {
    setWorkTitleDraft(work?.title ?? "");
    setWorkSynopsisDraft(work?.synopsis ?? "");
    setWorkEditorVisible(true);
  };

  const saveWork = async () => {
    if (!workId || !workTitleDraft.trim()) return;
    try {
      const updated = await storyApi.updateWork(workId, {
        title: workTitleDraft.trim(),
        synopsis: workSynopsisDraft.trim() || null,
      });
      setWork(updated);
      setOverview((current) => current ? { ...current, work: updated } : current);
      setWorkEditorVisible(false);
    } catch (error) {
      Alert.alert("Story", toError(error, "作品情報を保存できませんでした。"));
    }
  };

  const openNoteEditor = (note?: StoryNote) => {
    setEditor("note");
    setEditingId(note?.id ?? null);
    setTitleDraft(note?.title ?? "");
    setNoteContentDraft(note?.content ?? "");
  };

  const saveEditor = async () => {
    if (!workId || !titleDraft.trim()) return;
    try {
      if (editor === "episode") {
        const payload = { title: titleDraft.trim(), plot: plotDraft || null, summary: summaryDraft || null, status: statusDraft };
        const result = editingId ? await storyApi.updateEpisode(editingId, payload) : await storyApi.createEpisode(workId, payload);
        setEpisodes((items) => editingId ? items.map((item) => item.id === result.id ? result : item) : [...items, result]);
        setSelectedEpisodeId(result.id);
      } else if (editor === "note") {
        const result = editingId ? await storyApi.updateNote(editingId, { title: titleDraft.trim(), content: noteContentDraft || null }) : await storyApi.createNote(workId, { title: titleDraft.trim(), content: noteContentDraft || null });
        setNotes((items) => editingId ? items.map((item) => item.id === result.id ? result : item) : [...items, result]);
      }
      setEditor(null);
    } catch (error) {
      Alert.alert("Story", toError(error, "保存に失敗しました。"));
    }
  };

  const saveBody = async () => {
    if (!selectedEpisode) return;
    setBodySaving(true);
    try {
      const result = await storyApi.updateEpisodeBody(selectedEpisode.id, {
        body: bodyDraft,
        expected_etag: selectedEpisode.body_etag || "",
        commit: true,
        origin: "manual",
        created_by: "user",
      });
      setEpisodes((items) => items.map((item) => item.id === selectedEpisode.id ? { ...item, body: bodyDraft, body_etag: result.body_etag, char_count: result.char_count, current_rev_no: result.current_rev_no } : item));
      setBodyConflict(null);
      setLocalDraft(null);
      Alert.alert("Story", "本文を保存しました。");
    } catch (error) {
      // storyApi persists a conflict draft before throwing. Keep the local
      // text visible and expose an explicit server/rebase choice.
      const draft = await storyRepo.getDraft(selectedEpisode.id).catch(() => null);
      if (draft?.body != null) setBodyDraft(draft.body);
      const serverSnapshot = isBodyConflictError(error) ? error.serverSnapshot ?? draft?.server_snapshot ?? null : draft?.server_snapshot ?? null;
      if (isBodyConflictError(error) || draft?.conflict_status === "conflict") {
        const conflictDraft: StoryLocalDraft = draft ?? {
          episode_id: selectedEpisode.id,
          body: bodyDraft,
          expected_etag: selectedEpisode.body_etag ?? null,
          server_snapshot: serverSnapshot,
          conflict_status: "conflict",
        };
        if (serverSnapshot) setEpisodes((items) => items.map((item) => item.id === serverSnapshot.id ? serverSnapshot : item));
        setLocalDraft(conflictDraft);
        setBodyConflict({ localBody: conflictDraft.body, expectedEtag: conflictDraft.expected_etag ?? null, serverSnapshot });
      } else {
        Alert.alert("Story", toError(error, "本文を保存できませんでした。"));
      }
    } finally {
      setBodySaving(false);
    }
  };

  const useServerBody = async () => {
    if (!selectedEpisode || !bodyConflict?.serverSnapshot) return;
    const server = bodyConflict.serverSnapshot;
    await storyRepo.clearDraft(selectedEpisode.id).catch(() => undefined);
    setEpisodes((items) => items.map((item) => item.id === server.id ? server : item));
    setBodyDraft(server.body ?? "");
    setLocalDraft(null);
    setBodyConflict(null);
  };

  const rebaseLocalBody = async () => {
    if (!selectedEpisode || !bodyConflict?.serverSnapshot) return;
    const server = bodyConflict.serverSnapshot;
    const localBody = bodyDraft;
    setBodySaving(true);
    try {
      const result = await storyApi.updateEpisodeBody(selectedEpisode.id, {
        body: localBody,
        expected_etag: server.body_etag || bodyConflict.expectedEtag || "",
        commit: true,
        origin: "manual",
        created_by: "user",
      });
      setEpisodes((items) => items.map((item) => item.id === selectedEpisode.id ? { ...item, body: localBody, body_etag: result.body_etag, char_count: result.char_count, current_rev_no: result.current_rev_no } : item));
      setBodyDraft(localBody);
      setBodyConflict(null);
      setLocalDraft(null);
      Alert.alert("Story", "ローカル本文を最新ETagへリベースして保存しました。");
    } catch (error) {
      const draft = await storyRepo.getDraft(selectedEpisode.id).catch(() => null);
      if (draft) {
        setLocalDraft(draft);
        setBodyConflict({ localBody: draft.body, expectedEtag: draft.expected_etag ?? null, serverSnapshot: isBodyConflictError(error) ? error.serverSnapshot ?? server : server });
      }
      Alert.alert("本文の競合", toError(error, "リベースに失敗しました。最新サーバー版を取得して再試行してください。"));
    } finally {
      setBodySaving(false);
    }
  };

  const archiveEpisode = async (episode: StoryEpisode) => {
    try {
      const result = await storyApi.deleteEpisode(episode.id);
      setEpisodes((items) => items.filter((item) => item.id !== episode.id));
      setArchivedEpisodes((items) => [...items, { episode, restoreToken: result.restore_token }]);
      setSelectedEpisodeId((current) => current === episode.id ? null : current);
    } catch (error) {
      Alert.alert("Story", toError(error, "エピソードをアーカイブできませんでした。"));
    }
  };

  const restoreEpisode = async (item: ArchivedEpisode) => {
    try {
      const restored = await storyApi.restoreEpisode(item.episode.id, item.restoreToken);
      setEpisodes((items) => [...items, restored].sort((a, b) => (a.sort_hint ?? 0) - (b.sort_hint ?? 0)));
      setArchivedEpisodes((items) => items.filter((entry) => entry.episode.id !== item.episode.id));
    } catch (error) {
      Alert.alert("Story", toError(error, "復元に失敗しました。"));
    }
  };

  const refreshGraph = async () => {
    if (!workId) return;
    try {
      const next = await storyApi.getGraph(workId);
      setGraph(next);
      // Graph responses intentionally omit manuscript bodies. Rehydrate the
      // selected episode so graph mutations never drop its ETag/body from the
      // editor and a later save cannot accidentally PUT with an empty ETag.
      const selected = selectedEpisodeId
        ? await storyApi.getEpisode(selectedEpisodeId).catch(() => null)
        : null;
      setEpisodes(next.episodes.map((item) => selected?.id === item.id ? selected : item));
    } catch (error) {
      Alert.alert("Story", toError(error, "グラフを取得できませんでした。"));
    }
  };

  const searchWork = async () => {
    if (!workId || !searchQuery.trim()) {
      setSearchResults(null);
      return;
    }
    setSearchBusy(true);
    try {
      setSearchResults(await storyApi.searchWork(workId, searchQuery.trim()));
    } catch (error) {
      Alert.alert("Search", toError(error, "作品を検索できませんでした。"));
    } finally {
      setSearchBusy(false);
    }
  };

  const exportWork = async (scope: "route" | "all") => {
    if (!workId) return;
    try {
      const text = await storyApi.exportWork(workId, scope);
      await Share.share({
        title: `${work?.title || "Story"} (${scope})`,
        message: text,
      });
    } catch (error) {
      Alert.alert("Export", toError(error, "作品を書き出せませんでした。"));
    }
  };

  const loadContextPreview = async () => {
    if (!selectedEpisode) return;
    setContextBusy(true);
    try {
      setContextPreview(await storyApi.contextPreview(selectedEpisode.id));
    } catch (error) {
      Alert.alert("Context", toError(error, "Storyコンテキストを取得できませんでした。"));
    } finally {
      setContextBusy(false);
    }
  };

  const regenerateSummary = async (episode: StoryEpisode | null = selectedEpisode) => {
    if (!episode) return;
    setSummaryBusy(true);
    try {
      const result = await storyApi.regenerateSummary(episode.id);
      setEpisodes((items) => items.map((item) => item.id === result.id ? result : item));
      setOverview((current) => current ? { ...current, graph: { ...current.graph, episodes: current.graph.episodes.map((item) => item.id === result.id ? result : item) } } : current);
    } catch (error) {
      Alert.alert("Summary", toError(error, "要約を再生成できませんでした。"));
    } finally {
      setSummaryBusy(false);
    }
  };

  const openSplitEditor = () => {
    if (!selectedEpisode) return;
    if (bodyDraft !== (selectedEpisode.body ?? "")) {
      Alert.alert("Split", "未保存の本文があります。先に本文を保存してから分割してください。");
      return;
    }
    setSplitOffset(String(Math.max(1, Math.floor((selectedEpisode.body || "").length / 2))));
    setSplitTitle(`${selectedEpisode.title}（後半）`);
    setSplitVisible(true);
  };

  const splitEpisode = async () => {
    if (!selectedEpisode) return;
    const offset = Number.parseInt(splitOffset, 10);
    const bodyLength = (selectedEpisode.body || "").length;
    if (!Number.isFinite(offset) || offset < 0 || offset > bodyLength || !splitTitle.trim()) {
      Alert.alert("Split", "分割位置と新しい章タイトルを確認してください。");
      return;
    }
    setSplitBusy(true);
    try {
      await storyApi.splitEpisode(selectedEpisode.id, {
        offset,
        new_title: splitTitle.trim(),
        expected_etag: selectedEpisode.body_etag || "",
      });
      setSplitVisible(false);
      await load();
      Alert.alert("Split", "章を分割しました。");
    } catch (error) {
      Alert.alert("Split", toError(error, "章を分割できませんでした。"));
    } finally {
      setSplitBusy(false);
    }
  };

  const addNextLink = async () => {
    if (!workId || episodes.length < 2) return;
    const from = selectedEpisode ?? episodes[0];
    const to = episodes.find((item) => item.id !== from.id);
    if (!to) return;
    try {
      await storyApi.applyStructure(workId, [{ op: "add_link", from: from.id, to: to.id, choice_label: "next", is_primary: true }]);
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "リンクを追加できませんでした。"));
    }
  };

  const duplicateBranch = async () => {
    if (!workId || !selectedEpisode) return;
    try {
      await storyApi.duplicateAsBranch(workId, selectedEpisode.id, {
        choice_label: "別パターン",
        new_title: `${selectedEpisode.title}（別パターン）`,
      });
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "分岐を複製できませんでした。"));
    }
  };

  const removeLink = async (linkId: string) => {
    if (!workId) return;
    try {
      await storyApi.removeLink(workId, linkId);
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "リンクを削除できませんでした。"));
    }
  };

  const openLinkEditor = (link: StoryGraph["links"][number]) => {
    setLinkEditingId(link.id);
    setLinkLabelDraft(link.choice_label || "");
  };

  const saveLinkLabel = async () => {
    if (!workId || !linkEditingId) return;
    try {
      await storyApi.updateLink(workId, linkEditingId, { choice_label: linkLabelDraft.trim() || null });
      setLinkEditingId(null);
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "リンクラベルを更新できませんでした。"));
    }
  };

  const makePrimaryLink = async (linkId: string) => {
    if (!workId) return;
    try {
      await storyApi.updateLink(workId, linkId, { is_primary: true });
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "既定の分岐を更新できませんでした。"));
    }
  };

  const insertSelectedEpisode = async (link: StoryGraph["links"][number]) => {
    if (!workId || !selectedEpisode || selectedEpisode.id === link.from_episode_id || selectedEpisode.id === link.to_episode_id) return;
    try {
      await storyApi.insertEpisodeBetween(workId, link.id, selectedEpisode.id);
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "選択した章をリンクの間に挿入できませんでした。"));
    }
  };

  const reorderEpisode = async (episodeId: string, direction: -1 | 1) => {
    if (!workId) return;
    const index = episodes.findIndex((item) => item.id === episodeId);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= episodes.length) return;
    const ids = episodes.map((item) => item.id);
    [ids[index], ids[nextIndex]] = [ids[nextIndex], ids[index]];
    try {
      await storyApi.reorderEpisodes(workId, ids);
      await refreshGraph();
    } catch (error) {
      Alert.alert("Graph", toError(error, "章の並び替えは線形区間のみ対応しています。"));
    }
  };

  const setStartEpisode = async (episodeId: string) => {
    if (!workId) return;
    try {
      const updated = await storyApi.updateWork(workId, { start_episode_id: episodeId });
      setWork(updated);
    } catch (error) {
      Alert.alert("Graph", toError(error, "開始点を更新できませんでした。"));
    }
  };

  const loadRevisions = async () => {
    if (!selectedEpisode) return;
    setRevisionBusy(true);
    try {
      const result = await storyApi.listRevisions(selectedEpisode.id);
      setRevisions(result.items);
    } catch (error) {
      Alert.alert("Revisions", toError(error, "履歴を取得できませんでした。"));
    } finally {
      setRevisionBusy(false);
    }
  };

  useEffect(() => {
    if (section !== "revisions" || !selectedEpisode) return;
    let active = true;
    setRevisionBusy(true);
    void storyApi.listRevisions(selectedEpisode.id).then((result) => {
      if (active) setRevisions(result.items);
    }).catch(() => {
      // The explicit Refresh button exposes a retry without interrupting the
      // manuscript editor when history is temporarily unavailable.
    }).finally(() => {
      if (active) setRevisionBusy(false);
    });
    return () => { active = false; };
  }, [section, selectedEpisodeId]);

  const viewRevision = async (revision: StoryEpisodeRevision) => {
    if (!selectedEpisode) return;
    try {
      setRevisionDetail(await storyApi.getRevision(selectedEpisode.id, revision.rev_no));
    } catch (error) {
      Alert.alert("Revisions", toError(error, "リビジョンを取得できませんでした。"));
    }
  };

  const checkpointRevision = async () => {
    if (!selectedEpisode || !revisionMessage.trim()) return;
    setRevisionBusy(true);
    try {
      const result = await storyApi.checkpoint(selectedEpisode.id, revisionMessage.trim());
      setRevisions((items) => [result, ...items.filter((item) => item.id !== result.id)]);
      setRevisionMessage("");
    } catch (error) {
      Alert.alert("Revisions", toError(error, "チェックポイント作成に失敗しました。"));
    } finally {
      setRevisionBusy(false);
    }
  };

  const restoreRevision = async (revision: StoryEpisodeRevision) => {
    if (!selectedEpisode) return;
    setRevisionBusy(true);
    try {
      const result = await storyApi.restoreRevision(selectedEpisode.id, revision.rev_no);
      setEpisodes((items) => items.map((item) => item.id === result.episode.id ? result.episode : item));
      setBodyDraft(result.episode.body ?? "");
      setRevisionDetail(null);
      await loadRevisions();
      Alert.alert("Revisions", `Revision ${revision.rev_no} を復元しました。`);
    } catch (error) {
      Alert.alert("Revisions", toError(error, "リビジョン復元に失敗しました。"));
    } finally {
      setRevisionBusy(false);
    }
  };

  const toggleCharacter = async (character: StoryCharacter) => {
    if (!workId) return;
    const id = character.id;
    const attached = workCharacters.some((item) => (item.character_id ?? item.id) === id);
    const next = attached ? workCharacters.filter((item) => (item.character_id ?? item.id) !== id) : [...workCharacters, character];
    try {
      const payload: StoryWorkCharacterInput[] = next.map((item, index) => ({ character_id: item.character_id ?? item.id, role_note: item.role_note ?? null, position: index }));
      const result = await storyApi.replaceWorkCharacters(workId, payload);
      setWorkCharacters(result as AttachedCharacter[]);
    } catch (error) {
      Alert.alert("Characters", toError(error, "登場人物の関連付けを更新できませんでした。"));
    }
  };

  const openCharacterEditor = (character?: StoryCharacter) => {
    setCharacterEditingId(character?.id ?? null);
    setCharacterNameDraft(character?.name ?? "");
    setCharacterSummaryDraft(character?.summary ?? "");
    setCharacterDescriptionDraft(character?.description ?? "");
    setCharacterEditorVisible(true);
  };

  const saveCharacter = async () => {
    if (!characterNameDraft.trim()) return;
    try {
      const payload = {
        name: characterNameDraft.trim(),
        summary: characterSummaryDraft.trim() || null,
        description: characterDescriptionDraft.trim() || null,
      };
      const result = characterEditingId
        ? await storyApi.updateCharacter(characterEditingId, payload)
        : await storyApi.createCharacter(payload);
      setCharacters((items) => characterEditingId ? items.map((item) => item.id === result.id ? result : item) : [...items, result]);
      setCharacterEditorVisible(false);
    } catch (error) {
      Alert.alert("Characters", toError(error, "登場人物を保存できませんでした。"));
    }
  };

  const archiveCharacter = async (character: StoryCharacter) => {
    try {
      await storyApi.archiveCharacter(character.id);
      setCharacters((items) => items.filter((item) => item.id !== character.id));
      setWorkCharacters((items) => items.filter((item) => (item.character_id ?? item.id) !== character.id));
    } catch (error) {
      Alert.alert("Characters", toError(error, "登場人物をアーカイブできませんでした。"));
    }
  };

  const toggleRulebook = async (rulebook: StoryRulebook) => {
    if (!workId) return;
    const id = rulebook.id;
    const attached = workRulebooks.some((item) => (item.rulebook_id ?? item.id) === id);
    const next = attached ? workRulebooks.filter((item) => (item.rulebook_id ?? item.id) !== id) : [...workRulebooks, rulebook];
    try {
      const payload: StoryWorkRulebookInput[] = next.map((item, index) => ({ rulebook_id: item.rulebook_id ?? item.id, enabled: item.enabled ?? true, position: index }));
      const result = await storyApi.replaceWorkRulebooks(workId, payload);
      setWorkRulebooks(result as AttachedRulebook[]);
    } catch (error) {
      Alert.alert("Rulebooks", toError(error, "ルールブックの関連付けを更新できませんでした。"));
    }
  };

  const openRulebookEditor = (rulebook?: StoryRulebook) => {
    setRulebookEditingId(rulebook?.id ?? null);
    setRulebookNameDraft(rulebook?.name ?? "");
    setRulebookContentDraft(rulebook?.content ?? "");
    setRulebookEditorVisible(true);
  };

  const saveRulebook = async () => {
    if (!rulebookNameDraft.trim()) return;
    try {
      const payload = { name: rulebookNameDraft.trim(), content: rulebookContentDraft.trim() || null };
      const result = rulebookEditingId
        ? await storyApi.updateRulebook(rulebookEditingId, payload)
        : await storyApi.createRulebook(payload);
      setRulebooks((items) => rulebookEditingId ? items.map((item) => item.id === result.id ? result : item) : [...items, result]);
      setRulebookEditorVisible(false);
    } catch (error) {
      Alert.alert("Rulebooks", toError(error, "ルールブックを保存できませんでした。"));
    }
  };

  const archiveRulebook = async (rulebook: StoryRulebook) => {
    try {
      await storyApi.archiveRulebook(rulebook.id);
      setRulebooks((items) => items.filter((item) => item.id !== rulebook.id));
      setWorkRulebooks((items) => items.filter((item) => (item.rulebook_id ?? item.id) !== rulebook.id));
    } catch (error) {
      Alert.alert("Rulebooks", toError(error, "ルールブックをアーカイブできませんでした。"));
    }
  };

  const deleteNote = async (note: StoryNote) => {
    try {
      await storyApi.deleteNote(note.id);
      setNotes((items) => items.filter((item) => item.id !== note.id));
    } catch (error) {
      Alert.alert("Notes", toError(error, "設定資料を削除できませんでした。"));
    }
  };

  const runJob = async (kind: "compose" | "generate" | "revise" | "batch") => {
    if (!workId) return;
    setJobBusy(true);
    try {
      const next = kind === "compose"
        ? await storyApi.compose(workId, { episode_count: 1 })
        : kind === "batch"
          ? await storyApi.batchGenerate(workId, { episode_ids: episodes.map((item) => item.id) })
          : selectedEpisode
            ? await storyApi[kind](selectedEpisode.id, { instruction: "Keep the canonical Story context consistent." })
            : null;
      if (next) {
        setJob(next);
        setSection("jobs");
      }
    } catch (error) {
      Alert.alert("Story Job", toError(error, "ジョブを開始できませんでした。"));
    } finally {
      setJobBusy(false);
    }
  };

  const pollJob = async () => {
    if (!job) return;
    try {
      const next = await storyApi.getJob(job.id);
      setJob(next);
    } catch (error) {
      Alert.alert("Story Job", toError(error, "ジョブ状態を取得できませんでした。"));
    }
  };

  const applyComposeResult = async () => {
    if (!workId || !job || job.kind !== "compose" || job.status !== "done") return;
    const result = job.result;
    const generatedEpisodes = Array.isArray(result?.episodes) ? result.episodes.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object")) : [];
    const generatedLinks = Array.isArray(result?.links) ? result.links.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object")) : [];
    if (!generatedEpisodes.length) return;
    setJobBusy(true);
    try {
      await storyApi.composeApply(workId, { episodes: generatedEpisodes, links: generatedLinks });
      await load();
      Alert.alert("Story Job", "Compose結果を作品へ適用しました。");
    } catch (error) {
      Alert.alert("Story Job", toError(error, "Compose結果の適用に失敗しました。"));
    } finally {
      setJobBusy(false);
    }
  };

  const jobAction = async (action: "cancel" | "resume") => {
    if (!job) return;
    try {
      setJob(action === "cancel" ? await storyApi.cancelJob(job.id) : await storyApi.resumeJob(job.id));
    } catch (error) {
      Alert.alert("Story Job", toError(error, "ジョブ操作に失敗しました。"));
    }
  };

  const renderEpisodes = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Search this work</Text><Button compact textColor="#c4b5fd" loading={searchBusy} disabled={!searchQuery.trim()} onPress={() => void searchWork()}>Search</Button></View>
        <TextInput mode="outlined" label="Title, plot, summary, or manuscript" value={searchQuery} onChangeText={setSearchQuery} onSubmitEditing={() => void searchWork()} style={styles.input} />
        {searchResults ? <>{!searchResults.results.length ? <Text style={styles.description}>No matches.</Text> : searchResults.results.map((result) => <Button key={`${result.episode_id}-${result.match_start ?? 0}`} compact textColor="#89b4fa" onPress={() => { setSelectedEpisodeId(result.episode_id); setSection("episodes"); }}>{result.title} · {result.snippet}</Button>)}</> : null}
      </Surface>
      <View style={styles.rowBetween}>
        <Text style={styles.sectionTitle}>Episodes</Text>
        <Button compact textColor="#c4b5fd" onPress={() => openEpisodeEditor()}>New</Button>
      </View>
      {episodes.map((episode) => (
        <Surface key={episode.id} style={[styles.itemCard, selectedEpisodeId === episode.id && styles.itemSelected]} elevation={0}>
          <View style={styles.rowBetween}>
            <Button compact textColor="#cdd6f4" onPress={() => { setSelectedEpisodeId(episode.id); setBodyDraft(episode.body ?? ""); }}>{episode.title}</Button>
            <Text style={styles.meta}>{episode.status} · {episode.char_count} chars</Text>
          </View>
            <Text style={styles.description}>{episode.summary || episode.plot || "Summary unset"}</Text>
           <View style={styles.actions}>
             <Button compact textColor="#89b4fa" onPress={() => openEpisodeEditor(episode)}>Edit meta</Button>
             <Button compact textColor="#c4b5fd" onPress={() => { setSelectedEpisodeId(episode.id); void regenerateSummary(episode); }} loading={summaryBusy}>Summary</Button>
             <Button compact textColor="#f38ba8" onPress={() => void archiveEpisode(episode)}>Archive</Button>
             <Button compact textColor="#f9e2af" onPress={() => void setStartEpisode(episode.id)}>Start</Button>
             <Button compact textColor="#89b4fa" disabled={episodes.indexOf(episode) === 0} onPress={() => void reorderEpisode(episode.id, -1)}>↑</Button>
             <Button compact textColor="#89b4fa" disabled={episodes.indexOf(episode) === episodes.length - 1} onPress={() => void reorderEpisode(episode.id, 1)}>↓</Button>
           </View>
        </Surface>
      ))}
      {archivedEpisodes.length ? <Text style={styles.subheading}>Archived</Text> : null}
      {archivedEpisodes.map((item) => <Surface key={item.episode.id} style={styles.itemCard} elevation={0}><View style={styles.rowBetween}><Text style={styles.description}>{item.episode.title}</Text><Button compact textColor="#a6e3a1" onPress={() => void restoreEpisode(item)}>Restore</Button></View></Surface>)}
      {selectedEpisode ? (
        <Surface style={styles.editorCard} elevation={0}>
          <Text style={styles.sectionTitle}>Manuscript: {selectedEpisode.title}</Text>
          <TextInput mode="outlined" multiline value={bodyDraft} onChangeText={setBodyDraft} style={styles.bodyInput} label="Body" />
          <View style={styles.actions}><Button mode="contained" buttonColor="#7c3aed" loading={bodySaving} onPress={() => void saveBody()}>Save Body</Button><Button compact textColor="#89b4fa" onPress={() => router.push(`/scenario/session?workId=${workId}&episodeId=${selectedEpisode.id}`)}>Write in Chat</Button><Button compact textColor="#c4b5fd" onPress={openSplitEditor}>Split</Button><Button compact textColor="#c4b5fd" loading={contextBusy} onPress={() => void loadContextPreview()}>Context</Button></View>
          {bodyConflict ? <Surface style={styles.conflictCard} elevation={0}><Text style={styles.conflictTitle}>本文の競合: ローカル下書きを保持中</Text><Text style={styles.description}>サーバー版を確認するか、最新ETagへローカル本文をリベースしてください。</Text><View style={styles.actions}><Button compact mode="outlined" textColor="#89b4fa" onPress={() => void useServerBody()}>Server version</Button><Button compact mode="contained" buttonColor="#7c3aed" loading={bodySaving} onPress={() => void rebaseLocalBody()}>Rebase local</Button></View></Surface> : null}
          {localDraft && !bodyConflict ? <Text style={styles.meta}>Local draft recovered · {localDraft.conflict_status}</Text> : null}
          <Text style={styles.meta}>ETag {selectedEpisode.body_etag || "not set"} · revision {selectedEpisode.current_rev_no ?? 0}</Text>
          {contextPreview ? <Surface style={styles.contextCard} elevation={0}><Text style={styles.cardTitle}>Context preview · {contextPreview.estimated_chars} chars</Text><Text style={styles.description}>{contextPreview.resolved_model || contextPreview.model_layer || "canonical model"}</Text><Text style={styles.contextPrompt}>{contextPreview.prompt}</Text><Button compact textColor="#a6adc8" onPress={() => setContextPreview(null)}>Close context</Button></Surface> : null}
        </Surface>
      ) : null}
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Graph / branches</Text><View style={styles.actions}><Button compact textColor="#c4b5fd" onPress={() => void addNextLink()}>Add next</Button><Button compact textColor="#c4b5fd" disabled={!selectedEpisode} onPress={() => void duplicateBranch()}>Duplicate branch</Button></View></View>
        {(graph?.links ?? []).map((link) => <Surface key={link.id} style={styles.linkCard} elevation={0}><Text style={styles.description}>{episodes.find((item) => item.id === link.from_episode_id)?.title || link.from_episode_id} → {episodes.find((item) => item.id === link.to_episode_id)?.title || link.to_episode_id} {link.choice_label ? `(${link.choice_label})` : ""} {link.is_primary ? "· primary" : ""}</Text><View style={styles.actions}><Button compact textColor="#89b4fa" onPress={() => openLinkEditor(link)}>Label</Button><Button compact textColor="#f9e2af" disabled={Boolean(link.is_primary)} onPress={() => void makePrimaryLink(link.id)}>Primary</Button><Button compact textColor="#f38ba8" onPress={() => void removeLink(link.id)}>Remove</Button><Button compact textColor="#f9e2af" disabled={!selectedEpisode || selectedEpisode.id === link.from_episode_id || selectedEpisode.id === link.to_episode_id} onPress={() => void insertSelectedEpisode(link)}>Insert selected</Button></View></Surface>)}
        <Text style={styles.meta}>Current route: {overview?.current_route?.length ?? 0} episodes</Text>
      </Surface>
    </>
  );

  const renderMap = () => (
    <Surface style={styles.editorCard} elevation={0}>
      <View style={styles.rowBetween}><View><Text style={styles.sectionTitle}>Story map</Text><Text style={styles.description}>モバイルでは位置と分岐を一覧で安全に編集します。</Text></View><Button compact textColor="#c4b5fd" onPress={() => void refreshGraph()}>Refresh</Button></View>
      {(graph?.episodes ?? []).map((episode) => <Surface key={episode.id} style={styles.mapRow} elevation={0}><View style={styles.rowBetween}><Text style={styles.cardTitle}>{episode.title}</Text><Text style={styles.meta}>{episode.map_x ?? "—"}, {episode.map_y ?? "—"}</Text></View><Text style={styles.description}>{(graph?.links ?? []).filter((link) => link.from_episode_id === episode.id).length} outgoing · {(graph?.links ?? []).filter((link) => link.to_episode_id === episode.id).length} incoming</Text><View style={styles.actions}><Button compact textColor="#89b4fa" onPress={() => { setSelectedEpisodeId(episode.id); setSection("episodes"); }}>Open</Button><Button compact textColor="#f9e2af" onPress={() => void setStartEpisode(episode.id)}>{work?.start_episode_id === episode.id ? "Start" : "Set start"}</Button></View></Surface>)}
      {!graph?.episodes?.length ? <Text style={styles.description}>No map nodes yet.</Text> : null}
    </Surface>
  );

  const renderReview = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Review / export</Text><View style={styles.actions}><Button compact textColor="#c4b5fd" onPress={() => void exportWork("route")}>Export route</Button><Button compact textColor="#c4b5fd" onPress={() => void exportWork("all")}>Export all</Button></View></View>
        <Text style={styles.description}>現在の開始点から読者が辿るルートを確認します。分岐はGraphで編集できます。</Text>
        {(overview?.current_route ?? []).map((episodeId, index) => { const episode = episodes.find((item) => item.id === episodeId); return <Button key={episodeId} compact textColor="#89b4fa" onPress={() => { setSelectedEpisodeId(episodeId); setSection("episodes"); }}>{index + 1}. {episode?.title || episodeId}</Button>; })}
        {!overview?.current_route?.length ? <Text style={styles.description}>開始点が設定されていません。</Text> : null}
      </Surface>
      {selectedEpisode ? <Surface style={styles.editorCard} elevation={0}><Text style={styles.sectionTitle}>Selected context</Text><Button mode="outlined" textColor="#c4b5fd" loading={contextBusy} onPress={() => void loadContextPreview()}>Preview canonical context</Button>{contextPreview ? <Text style={styles.contextPrompt}>{contextPreview.prompt}</Text> : null}</Surface> : null}
    </>
  );

  const renderLibrary = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}><View style={styles.rowBetween}><Text style={styles.sectionTitle}>Shared library</Text><Button compact textColor="#c4b5fd" onPress={() => setSection("characters")}>Open cast</Button></View><Text style={styles.description}>{characters.length} characters · {rulebooks.length} rulebooks available to this account.</Text><Button compact textColor="#89b4fa" onPress={() => setSection("rulebooks")}>Open rules</Button></Surface>
      {characters.slice(0, 5).map((character) => <Surface key={character.id} style={styles.itemCard} elevation={0}><Text style={styles.cardTitle}>{character.name}</Text><Text style={styles.description}>{character.summary || character.description || "No summary"}</Text></Surface>)}
      {rulebooks.slice(0, 5).map((rulebook) => <Surface key={rulebook.id} style={styles.itemCard} elevation={0}><Text style={styles.cardTitle}>{rulebook.name}</Text><Text style={styles.description}>{rulebook.content || "No content"}</Text></Surface>)}
    </>
  );

  const renderSettings = () => (
    <Surface style={styles.editorCard} elevation={0}>
      <Text style={styles.sectionTitle}>Story settings</Text>
      <Text style={styles.description}>Status: {work?.status || "—"}</Text>
      <Text style={styles.description}>Kind: {work?.kind === "trpg" ? "TRPG" : "Novel"}</Text>
      <Text style={styles.description}>Target: {work?.target_episode_chars ?? "—"} chars / episode</Text>
      <Button mode="contained" buttonColor="#7c3aed" onPress={openWorkEditor}>Edit work metadata</Button>
    </Surface>
  );

  const renderRevisions = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Revision history</Text><Button compact textColor="#c4b5fd" loading={revisionBusy} disabled={!selectedEpisode} onPress={() => void loadRevisions()}>Refresh</Button></View>
        {selectedEpisode ? <><Text style={styles.meta}>Episode: {selectedEpisode.title}</Text><TextInput mode="outlined" label="Checkpoint message" value={revisionMessage} onChangeText={setRevisionMessage} style={styles.input} /><Button mode="outlined" textColor="#c4b5fd" disabled={!revisionMessage.trim() || revisionBusy} onPress={() => void checkpointRevision()}>Create checkpoint</Button></> : <Text style={styles.description}>Select an episode first.</Text>}
      </Surface>
      {revisions.map((revision) => <Surface key={revision.id} style={styles.itemCard} elevation={0}><View style={styles.rowBetween}><Text style={styles.cardTitle}>Revision {revision.rev_no}</Text><Text style={styles.meta}>{revision.origin} · {revision.char_count} chars</Text></View><Text style={styles.description}>{revision.message || "No message"}</Text><View style={styles.actions}><Button compact textColor="#89b4fa" onPress={() => void viewRevision(revision)}>View</Button><Button compact textColor="#f9e2af" disabled={revisionBusy} onPress={() => void restoreRevision(revision)}>Restore</Button></View></Surface>)}
      {revisionDetail ? <Surface style={styles.editorCard} elevation={0}><Text style={styles.sectionTitle}>Revision {revisionDetail.rev_no} detail</Text><Text style={styles.description}>{revisionDetail.body || "(empty body)"}</Text><Button compact textColor="#a6adc8" onPress={() => setRevisionDetail(null)}>Close</Button></Surface> : null}
      {!revisions.length && selectedEpisode ? <Text style={styles.description}>No revisions loaded. Tap Refresh.</Text> : null}
    </>
  );

  const renderCharacters = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Character pool</Text><Button compact textColor="#c4b5fd" onPress={() => openCharacterEditor()}>New</Button></View>
        {characters.map((character) => { const active = workCharacters.some((item) => (item.character_id ?? item.id) === character.id); return <View key={character.id} style={styles.poolRow}><Chip selected={active} onPress={() => void toggleCharacter(character)} style={active ? styles.chipActive : styles.chip} textStyle={styles.chipText}>{character.name}</Chip><Button compact textColor="#89b4fa" onPress={() => openCharacterEditor(character)}>Edit</Button><Button compact textColor="#f38ba8" onPress={() => void archiveCharacter(character)}>Archive</Button></View>; })}
        {!characters.length ? <Text style={styles.description}>Character pool is empty.</Text> : null}
        <Text style={styles.meta}>Tap a character to attach or detach it from this work.</Text>
      </Surface>
    </>
  );

  const renderRulebooks = () => (
    <>
      <Surface style={styles.editorCard} elevation={0}>
        <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Rulebook pool</Text><Button compact textColor="#c4b5fd" onPress={() => openRulebookEditor()}>New</Button></View>
        {rulebooks.map((rulebook) => { const active = workRulebooks.some((item) => (item.rulebook_id ?? item.id) === rulebook.id); return <View key={rulebook.id} style={styles.poolRow}><Chip selected={active} onPress={() => void toggleRulebook(rulebook)} style={active ? styles.chipActive : styles.chip} textStyle={styles.chipText}>{rulebook.name}</Chip><Button compact textColor="#89b4fa" onPress={() => openRulebookEditor(rulebook)}>Edit</Button><Button compact textColor="#f38ba8" onPress={() => void archiveRulebook(rulebook)}>Archive</Button></View>; })}
        {!rulebooks.length ? <Text style={styles.description}>Rulebook pool is empty.</Text> : null}
        <Text style={styles.meta}>Tap a rulebook to attach or detach it from this work.</Text>
      </Surface>
    </>
  );

  const renderNotes = () => (
    <>
      <View style={styles.rowBetween}><Text style={styles.sectionTitle}>Notes</Text><Button compact textColor="#c4b5fd" onPress={() => openNoteEditor()}>New</Button></View>
      {notes.map((note) => <Surface key={note.id} style={styles.itemCard} elevation={0}><Text style={styles.cardTitle}>{note.title}</Text><Text style={styles.description}>{note.content || ""}</Text><View style={styles.actions}><Button compact textColor="#89b4fa" onPress={() => openNoteEditor(note)}>Edit</Button><Button compact textColor="#f38ba8" onPress={() => void deleteNote(note)}>Delete</Button></View></Surface>)}
      {!notes.length ? <Text style={styles.description}>No notes yet.</Text> : null}
    </>
  );

  const renderJobs = () => (
    <Surface style={styles.editorCard} elevation={0}>
      <Text style={styles.sectionTitle}>Generation jobs</Text>
      <View style={styles.actions}><Button compact mode="outlined" onPress={() => void runJob("compose")} disabled={jobBusy}>Compose</Button><Button compact mode="outlined" onPress={() => void runJob("generate")} disabled={jobBusy || !selectedEpisode}>Generate</Button><Button compact mode="outlined" onPress={() => void runJob("revise")} disabled={jobBusy || !selectedEpisode}>Revise</Button><Button compact mode="outlined" onPress={() => void runJob("batch")} disabled={jobBusy || !episodes.length}>Batch</Button></View>
      {job ? <><Text style={styles.cardTitle}>{job.kind} · {job.status}</Text><Text style={styles.description}>{job.error || JSON.stringify(job.progress || {})}</Text><ProgressBar progress={typeof job.progress?.completed === "number" && typeof job.progress?.total === "number" && job.progress.total > 0 ? job.progress.completed / job.progress.total : job.status === "done" ? 1 : 0} color="#7c3aed" /><View style={styles.actions}><Button compact textColor="#89b4fa" onPress={() => void pollJob()}>Refresh</Button>{job.kind === "compose" && job.status === "done" && Array.isArray(job.result?.episodes) ? <Button compact mode="contained" buttonColor="#7c3aed" loading={jobBusy} onPress={() => void applyComposeResult()}>Apply compose result</Button> : null}{job.status === "queued" || job.status === "running" ? <Button compact textColor="#f38ba8" onPress={() => void jobAction("cancel")}>Cancel</Button> : null}{job.status === "canceled" || job.status === "error" ? <Button compact textColor="#a6e3a1" onPress={() => void jobAction("resume")}>Resume</Button> : null}</View></> : <Text style={styles.description}>No active job.</Text>}
    </Surface>
  );

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}><View style={styles.headerRow}><IconButton icon="arrow-left" iconColor="#cdd6f4" onPress={() => goBackOrReplace(router, "/scenarios")} /><View style={styles.headerText}><Text variant="titleLarge" style={styles.headerTitle}>{work?.title || "Story Work"}</Text><Text style={styles.headerSubtext}>{work?.kind === "trpg" ? "TRPG" : "Novel"} · canonical Story</Text></View><Button compact textColor="#c4b5fd" onPress={openWorkEditor}>Edit work</Button></View></Surface>
      <ScrollView contentContainerStyle={styles.content}>
        {errorMessage ? <Text style={styles.error}>{errorMessage}</Text> : null}
        <View style={styles.sectionTabs}>{(["episodes", "map", "review", "library", "settings", "characters", "rulebooks", "notes", "revisions", "jobs"] as SectionKey[]).map((key) => <Button key={key} compact mode={section === key ? "contained" : "outlined"} buttonColor={section === key ? "#4c1d95" : undefined} textColor="#cdd6f4" onPress={() => setSection(key)}>{key}</Button>)}</View>
        {section === "episodes" ? renderEpisodes() : null}
        {section === "map" ? renderMap() : null}
        {section === "review" ? renderReview() : null}
        {section === "library" ? renderLibrary() : null}
        {section === "settings" ? renderSettings() : null}
        {section === "characters" ? renderCharacters() : null}
        {section === "rulebooks" ? renderRulebooks() : null}
        {section === "notes" ? renderNotes() : null}
        {section === "revisions" ? renderRevisions() : null}
        {section === "jobs" ? renderJobs() : null}
      </ScrollView>
      <Portal>
        <Dialog visible={workEditorVisible} onDismiss={() => setWorkEditorVisible(false)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>Edit Story Work</Dialog.Title><Dialog.Content><TextInput label="Title" mode="outlined" value={workTitleDraft} onChangeText={setWorkTitleDraft} style={styles.input} /><TextInput label="Synopsis" mode="outlined" multiline value={workSynopsisDraft} onChangeText={setWorkSynopsisDraft} style={styles.input} /></Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setWorkEditorVisible(false)}>Cancel</Button><Button textColor="#c4b5fd" disabled={!workTitleDraft.trim()} onPress={() => void saveWork()}>Save</Button></Dialog.Actions></Dialog>
        <Dialog visible={editor != null} onDismiss={() => setEditor(null)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>{editor === "episode" ? (editingId ? "Edit Episode" : "New Episode") : (editingId ? "Edit Note" : "New Note")}</Dialog.Title><Dialog.Content><TextInput label="Title" mode="outlined" value={titleDraft} onChangeText={setTitleDraft} style={styles.input} />{editor === "episode" ? <><TextInput label="Plot" mode="outlined" multiline value={plotDraft} onChangeText={setPlotDraft} style={styles.input} /><TextInput label="Summary" mode="outlined" multiline value={summaryDraft} onChangeText={setSummaryDraft} style={styles.input} /><TextInput label="Status" mode="outlined" value={statusDraft} onChangeText={setStatusDraft} style={styles.input} /></> : <TextInput label="Content" mode="outlined" multiline value={noteContentDraft} onChangeText={setNoteContentDraft} style={styles.input} />}</Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setEditor(null)}>Cancel</Button><Button textColor="#c4b5fd" disabled={!titleDraft.trim()} onPress={() => void saveEditor()}>Save</Button></Dialog.Actions></Dialog>
        <Dialog visible={characterEditorVisible} onDismiss={() => setCharacterEditorVisible(false)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>{characterEditingId ? "Edit Character" : "New Character"}</Dialog.Title><Dialog.Content><TextInput label="Name" mode="outlined" value={characterNameDraft} onChangeText={setCharacterNameDraft} style={styles.input} /><TextInput label="Summary" mode="outlined" multiline value={characterSummaryDraft} onChangeText={setCharacterSummaryDraft} style={styles.input} /><TextInput label="Description" mode="outlined" multiline value={characterDescriptionDraft} onChangeText={setCharacterDescriptionDraft} style={styles.input} /></Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setCharacterEditorVisible(false)}>Cancel</Button><Button textColor="#c4b5fd" disabled={!characterNameDraft.trim()} onPress={() => void saveCharacter()}>Save</Button></Dialog.Actions></Dialog>
        <Dialog visible={rulebookEditorVisible} onDismiss={() => setRulebookEditorVisible(false)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>{rulebookEditingId ? "Edit Rulebook" : "New Rulebook"}</Dialog.Title><Dialog.Content><TextInput label="Name" mode="outlined" value={rulebookNameDraft} onChangeText={setRulebookNameDraft} style={styles.input} /><TextInput label="Content" mode="outlined" multiline value={rulebookContentDraft} onChangeText={setRulebookContentDraft} style={styles.input} /></Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setRulebookEditorVisible(false)}>Cancel</Button><Button textColor="#c4b5fd" disabled={!rulebookNameDraft.trim()} onPress={() => void saveRulebook()}>Save</Button></Dialog.Actions></Dialog>
        <Dialog visible={splitVisible} onDismiss={() => setSplitVisible(false)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>Split episode</Dialog.Title><Dialog.Content><Text style={styles.description}>本文の文字位置で現在の章を前半・後半へ分けます。</Text><TextInput label="Offset" mode="outlined" keyboardType="numeric" value={splitOffset} onChangeText={setSplitOffset} style={styles.input} /><TextInput label="New episode title" mode="outlined" value={splitTitle} onChangeText={setSplitTitle} style={styles.input} /></Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setSplitVisible(false)}>Cancel</Button><Button textColor="#c4b5fd" loading={splitBusy} onPress={() => void splitEpisode()}>Split</Button></Dialog.Actions></Dialog>
        <Dialog visible={linkEditingId != null} onDismiss={() => setLinkEditingId(null)} style={styles.dialog}><Dialog.Title style={styles.dialogTitle}>Edit branch label</Dialog.Title><Dialog.Content><TextInput label="Choice label" mode="outlined" value={linkLabelDraft} onChangeText={setLinkLabelDraft} style={styles.input} /></Dialog.Content><Dialog.Actions><Button textColor="#a6adc8" onPress={() => setLinkEditingId(null)}>Cancel</Button><Button textColor="#c4b5fd" onPress={() => void saveLinkLabel()}>Save</Button></Dialog.Actions></Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: { paddingTop: 52, paddingHorizontal: 8, paddingBottom: 16, backgroundColor: "#1e1e2e" },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerText: { flex: 1 },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  content: { padding: 16, gap: 12, paddingBottom: 40 },
  sectionTabs: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: { backgroundColor: "#313244" },
  chipActive: { backgroundColor: "#4c1d95" },
  chipText: { color: "#cdd6f4" },
  sectionTitle: { color: "#c4b5fd", fontSize: 15, fontWeight: "700" },
  subheading: { color: "#a6adc8", fontSize: 13, marginTop: 8 },
  rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 },
  itemCard: { backgroundColor: "#1e1e2e", borderRadius: 10, padding: 12 },
  itemSelected: { borderWidth: 1, borderColor: "#7c3aed" },
  editorCard: { backgroundColor: "#1e1e2e", borderRadius: 10, padding: 14, gap: 8 },
  conflictCard: { backgroundColor: "#2b2034", borderRadius: 8, padding: 10, gap: 6 },
  contextCard: { backgroundColor: "#181825", borderRadius: 8, padding: 10, gap: 6 },
  contextPrompt: { color: "#bac2de", fontSize: 12, lineHeight: 17, maxHeight: 260 },
  linkCard: { backgroundColor: "#181825", borderRadius: 8, padding: 8, gap: 4 },
  mapRow: { backgroundColor: "#181825", borderRadius: 8, padding: 10, gap: 4 },
  conflictTitle: { color: "#f9e2af", fontWeight: "700" },
  cardTitle: { color: "#cdd6f4", fontWeight: "700" },
  description: { color: "#a6adc8", fontSize: 13, lineHeight: 19 },
  meta: { color: "#6c7086", fontSize: 11 },
  error: { color: "#f38ba8", padding: 8, backgroundColor: "#2b1722", borderRadius: 8 },
  actions: { flexDirection: "row", flexWrap: "wrap", justifyContent: "flex-end", gap: 6 },
  chipWrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  poolRow: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: 4 },
  input: { marginBottom: 10 },
  bodyInput: { minHeight: 220 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
});
