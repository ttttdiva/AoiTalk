import React, { useEffect, useState } from "react";
import { Image, StyleSheet, View } from "react-native";
import { IconButton, Text } from "react-native-paper";
import {
  filesApi,
  getFilesMediaKind,
  type FilesEntry,
} from "../../lib/files-api";
import { formatSize, getFileIcon } from "./file-browser-model";

type MediaSource = Awaited<ReturnType<typeof filesApi.getMediaSource>>;

export function FileThumbnail({
  entry,
  size,
}: {
  entry: FilesEntry;
  size: number;
}) {
  const [source, setSource] = useState<MediaSource | null>(null);
  const [failed, setFailed] = useState(false);
  const kind = getFilesMediaKind(entry);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSource(null);
    if (entry.type === "directory" || (kind !== "image" && kind !== "video")) {
      return () => {
        cancelled = true;
      };
    }
    void filesApi
      .getMediaSource(entry, { thumbnail: true, size: Math.max(160, size * 2) })
      .then((nextSource) => {
        if (!cancelled) setSource(nextSource);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [entry, kind, size]);

  if (entry.type === "directory" || !source || failed) {
    return (
      <View
        style={[
          styles.thumbnailFallback,
          { width: size, height: size },
          entry.type === "directory" ? styles.thumbnailFolder : null,
        ]}
      >
        <IconButton
          icon={getFileIcon(entry)}
          iconColor={entry.type === "directory" ? "#f9e2af" : "#89b4fa"}
          size={Math.min(44, size * 0.38)}
          style={styles.thumbnailIcon}
        />
      </View>
    );
  }

  return (
    <View style={[styles.thumbnailFrame, { width: size, height: size }]}>
      <Image
        source={source}
        style={styles.thumbnailImage}
        resizeMode="cover"
        onError={() => setFailed(true)}
      />
      {kind === "video" ? (
        <View style={styles.videoBadge}>
          <IconButton
            icon="play"
            iconColor="#ffffff"
            size={16}
            style={styles.videoBadgeIcon}
          />
        </View>
      ) : null}
    </View>
  );
}

export function FileMetadata({
  entry,
  grid = false,
}: {
  entry: FilesEntry;
  grid?: boolean;
}) {
  const [size, setSize] = useState(entry.size);

  useEffect(() => {
    let cancelled = false;
    setSize(entry.size);
    if (entry.type === "file") {
      void filesApi.getMetadata(entry).then((metadata) => {
        if (!cancelled) setSize(metadata.size);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [entry]);

  return (
    <Text style={grid ? styles.gridFileMeta : styles.fileMeta} numberOfLines={1}>
      {entry.type === "directory"
        ? "フォルダー"
        : grid
          ? formatSize(size)
          : `${entry.mimeType || "ファイル"}${size ? ` ・ ${formatSize(size)}` : ""}`}
    </Text>
  );
}

const styles = StyleSheet.create({
  fileMeta: { color: "#a6adc8", fontSize: 11, marginTop: 3 },
  gridFileMeta: {
    color: "#a6adc8",
    fontSize: 10,
    marginTop: 4,
    textAlign: "center",
  },
  thumbnailFrame: {
    overflow: "hidden",
    borderRadius: 8,
    backgroundColor: "#181825",
  },
  thumbnailImage: { width: "100%", height: "100%" },
  thumbnailFallback: {
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 8,
    backgroundColor: "#181825",
  },
  thumbnailFolder: { backgroundColor: "#241f2f" },
  thumbnailIcon: { margin: 0 },
  videoBadge: {
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(0, 0, 0, 0.18)",
  },
  videoBadgeIcon: { margin: 0, backgroundColor: "rgba(0,0,0,0.45)" },
});
