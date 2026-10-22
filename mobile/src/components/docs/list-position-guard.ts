type Geometry = { offsetY: number; contentHeight: number; timestamp: number };
type Gesture = {
  generation: number;
  phase: "drag" | "await-momentum" | "momentum";
  contentHeight: number;
  headerHeight: number | null;
  measurement: number;
  timestamp: number;
};

/** Only a current user gesture with freshly measured geometry may release MVP. */
export function createDocsListPositionGuard({ measureHeader, release }: {
  measureHeader: (receive: (height: number) => void) => void;
  release: () => void;
}) {
  let generation = 0;
  let focused = false;
  let rootActive = false;
  let gesture: Gesture | null = null;
  const active = () => focused && rootActive;
  const current = (candidate: Gesture) => active()
    && gesture === candidate && candidate.generation === generation;
  const accepts = (candidate: Gesture, point: Geometry) => current(candidate)
    && Number.isFinite(point.timestamp) && point.timestamp >= candidate.timestamp;
  const measure = (candidate: Gesture) => {
    candidate.headerHeight = null;
    const measurement = ++candidate.measurement;
    if (candidate.phase === "await-momentum") return;
    measureHeader((height) => {
      if (current(candidate) && candidate.measurement === measurement
        && Number.isFinite(height) && height >= 0) candidate.headerHeight = height;
    });
  };
  const releaseAt = (candidate: Gesture, point: Geometry) => {
    if (current(candidate) && candidate.headerHeight !== null
      && point.contentHeight === candidate.contentHeight
      && Number.isFinite(point.offsetY) && point.offsetY < candidate.headerHeight) release();
  };
  return {
    active,
    focus() { generation++; focused = true; gesture = null; },
    blur() { generation++; focused = false; gesture = null; },
    reset() { generation++; gesture = null; },
    cancelGesture() { gesture = null; },
    setRootActive(value: boolean) {
      rootActive = value;
      if (!value) gesture = null;
    },
    headerLayout() { if (gesture) measure(gesture); },
    contentSize(height: number) {
      if (gesture && height !== gesture.contentHeight) {
        gesture.contentHeight = height;
        measure(gesture);
      }
    },
    beginDrag(point: Geometry) {
      gesture = null;
      if (!active() || !Number.isFinite(point.timestamp)) return;
      gesture = { generation, phase: "drag", contentHeight: point.contentHeight,
        headerHeight: null, measurement: 0, timestamp: point.timestamp };
      measure(gesture);
    },
    endDrag(point: Geometry, momentumExpected: boolean) {
      if (!gesture || gesture.phase !== "drag" || !accepts(gesture, point)) return;
      releaseAt(gesture, point);
      gesture.timestamp = point.timestamp;
      if (momentumExpected) gesture.phase = "await-momentum";
      else gesture = null;
    },
    beginMomentum(point: Geometry) {
      if (!gesture || gesture.phase !== "await-momentum" || !accepts(gesture, point)) return;
      gesture.phase = "momentum";
      gesture.contentHeight = point.contentHeight;
      gesture.timestamp = point.timestamp;
      measure(gesture);
    },
    endMomentum(point: Geometry) {
      if (!gesture || gesture.phase !== "momentum" || !accepts(gesture, point)) return;
      releaseAt(gesture, point);
      gesture = null;
    },
  };
}

/**
 * A JS-owned list anchor used when Android native
 * maintainVisibleContentPosition is intentionally disabled.
 *
 * The controller only stores geometry and a stable row key.  It never stores
 * row text, document data, authentication material, or a reference to the
 * list itself.  The screen supplies the final scroll operation after the
 * controller has proved that the current layout has enough measurements.
 */
export type DocsListCellLayout = {
  key: string;
  index: number;
  y: number;
  height: number;
};

export type DocsListViewportLayout = {
  offsetY: number;
  viewportHeight: number;
  contentHeight: number;
};

export type DocsListMeasurementToken = number;

export type DocsListMeasuredSnapshot = {
  row: DocsListCellLayout;
  headerHeight: number;
  viewportHeight: number;
  contentHeight: number;
};

export type DocsListSeedRestoreResult = "applied" | "not-ready" | "invalid";

export type DocsListSeedRestoreGeometry = {
  headerHeight: number;
  viewportHeight: number;
  contentHeight: number;
};

export type DocsListRestoreLifecycle = {
  event: "capture" | "arm" | "seed" | "execute" | "complete" | "drop";
  generation: number;
  measurementEpoch: DocsListMeasurementToken;
  layoutRevision: number;
  targetIndex: number;
  targetOffset: number;
  viewportY: number;
  viewportHeight: number;
  contentHeight: number;
  pending: boolean;
  ready: boolean;
  reasonCode: number;
};

type Row = DocsListCellLayout & { dataRevision: number };
type Anchor = {
  key: string;
  index: number;
  y: number;
  height: number;
  viewportY: number | null;
  rootY: number | null;
  rootContentHeight: number | null;
  dataRevision: number;
  dataSignature: string;
  needsRestore: boolean;
  lastAppliedRevision: number | null;
  seedAppliedRevision: number | null;
};

type MeasurementKind = "row" | "header" | "viewport" | "content";
type MeasurementCohort = Record<MeasurementKind, boolean>;

const GEOMETRY_EPSILON = 0.5;

function changed(a: number | null, b: number | null): boolean {
  if (a === null || b === null) return a !== b;
  return Math.abs(a - b) > GEOMETRY_EPSILON;
}

/**
 * Preserve a visible row across focus/layout changes without relying on the
 * Android native MVP helper.  A later header/content/row measurement creates
 * another layout revision and re-arms the same anchor, so a first early
 * restore cannot make subsequent canonical receipt resizing stale.
 */
export function createDocsListPositionController({
  onLifecycle,
}: {
  onLifecycle?: (entry: DocsListRestoreLifecycle) => void;
} = {}) {
  let focused = false;
  let rootActive = false;
  let generation = 0;
  let measurementEpoch = 0;
  let layoutRevision = 0;
  let dataRevision = 0;
  let dataSignature = "";
  let userGesture = false;
  let anchor: Anchor | null = null;
  let headerHeight: number | null = null;
  let viewport: DocsListViewportLayout | null = null;
  let viewportOffsetMeasured = false;
  let cohortOpen = false;
  let cohort: MeasurementCohort = {
    row: false,
    header: false,
    viewport: false,
    content: false,
  };
  const invalidKinds = new Set<MeasurementKind>();
  const rows = new Map<string, Row>();

  const resetCohort = () => {
    cohort = { row: false, header: false, viewport: false, content: false };
    invalidKinds.clear();
  };

  const rotateMeasurementEpoch = (reuseMeasurements: boolean) => {
    measurementEpoch += 1;
    cohortOpen = false;
    if (!reuseMeasurements) {
      resetCohort();
      viewportOffsetMeasured = false;
    }
    return measurementEpoch;
  };

  const acceptsToken = (token: DocsListMeasurementToken) => token === measurementEpoch;

  const markMeasurement = (kind: MeasurementKind, fresh = false) => {
    if (!anchor) {
      cohort[kind] = true;
      return;
    }
    if (!anchor.needsRestore) return;
    if (invalidKinds.has(kind)) {
      if (!fresh) return;
      invalidKinds.delete(kind);
    }
    if (!cohortOpen) cohortOpen = true;
    cohort[kind] = true;
  };

  const lifecycle = (
    event: DocsListRestoreLifecycle["event"],
    reasonCode = 0,
    ready = false,
    targetOffsetOverride?: number,
  ) => {
    const currentRow = anchor ? rows.get(anchor.key) : undefined;
    onLifecycle?.({
      event,
      generation,
      measurementEpoch,
      layoutRevision,
      targetIndex: anchor?.index ?? currentRow?.index ?? -1,
      targetOffset: targetOffsetOverride ?? (anchor && anchor.viewportY !== null && currentRow
        ? currentRow.y - anchor.viewportY
        : 0),
      viewportY: anchor?.viewportY ?? 0,
      viewportHeight: viewport?.viewportHeight ?? 0,
      contentHeight: viewport?.contentHeight ?? 0,
      pending: Boolean(anchor?.needsRestore),
      ready,
      reasonCode,
    });
  };

  const arm = (reasonCode: number, resetKinds: readonly MeasurementKind[] = []) => {
    if (!anchor) return;
    anchor.needsRestore = true;
    anchor.lastAppliedRevision = null;
    if (!cohortOpen) {
      cohortOpen = true;
      layoutRevision += 1;
    } else if (anchor.seedAppliedRevision !== null) {
      // A layout change after a seed starts a new bounded geometry pass.  The
      // next seed is then allowed once, while repeated callbacks in this pass
      // continue to share the same revision.
      layoutRevision += 1;
      anchor.seedAppliedRevision = null;
    }
    for (const kind of resetKinds) {
      cohort[kind] = false;
      invalidKinds.add(kind);
    }
    lifecycle("arm", reasonCode);
  };

  const drop = (reasonCode: number) => {
    if (!anchor) return;
    anchor = null;
    cohortOpen = false;
    invalidKinds.clear();
    lifecycle("drop", reasonCode);
  };

  const validNumber = (value: number) => Number.isFinite(value);

  return {
    active: () => focused && rootActive,
    pending: () => Boolean(anchor?.needsRestore),
    measurementToken: () => measurementEpoch,
    focus() {
      generation += 1;
      const token = rotateMeasurementEpoch(true);
      focused = true;
      userGesture = false;
      // A route transition can detach and reattach the native list without
      // changing any row layout.  Re-check the cached measurements on focus.
      arm(1);
      return token;
    },
    blur() {
      generation += 1;
      const token = rotateMeasurementEpoch(true);
      focused = false;
      userGesture = false;
      return token;
    },
    setRootActive(value: boolean) {
      if (rootActive !== value) rotateMeasurementEpoch(true);
      rootActive = value;
      if (!value) userGesture = false;
      return measurementEpoch;
    },
    invalidate(reasonCode = 2) {
      const token = rotateMeasurementEpoch(false);
      rows.clear();
      dataSignature = "";
      dataRevision += 1;
      drop(reasonCode);
      return token;
    },
    observeDataKeys(keys: readonly string[], token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return measurementEpoch;
      const signature = keys.join("\u0001");
      if (signature === dataSignature) return measurementEpoch;
      dataSignature = signature;
      const preserveMeasuredGeometry = anchor !== null;
      rotateMeasurementEpoch(preserveMeasuredGeometry);
      dataRevision += 1;
      const keySet = new Set(keys);
      for (const key of rows.keys()) {
        if (!keySet.has(key)) rows.delete(key);
      }
      if (anchor && !keySet.has(anchor.key)) {
        drop(3);
        return measurementEpoch;
      }
      arm(4, ["row"]);
      return measurementEpoch;
    },
    observeRow(layout: DocsListCellLayout, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      if (!layout || !validNumber(layout.index) || !validNumber(layout.y)
        || !validNumber(layout.height) || layout.height < 0) return;
      const previous = rows.get(layout.key);
      const geometryChanged = !previous
        || previous.index !== layout.index
        || changed(previous.y, layout.y)
        || changed(previous.height, layout.height)
        || previous.dataRevision !== dataRevision;
      rows.set(layout.key, { ...layout, dataRevision });
      if (anchor?.key !== layout.key) return;
      if (anchor.viewportY === null && viewport) {
        anchor.viewportY = layout.y - viewport.offsetY;
      }
      // A current row callback can provide the first baseline after a list
      // reattach.  Keep the capture baseline stable once it exists; a
      // coherent native snapshot below is the only later source that may
      // replace it.
      if (anchor.rootY === null && headerHeight !== null) {
        anchor.rootY = layout.y - headerHeight;
      }
      if (anchor.rootContentHeight === null && anchor.rootY !== null
        && headerHeight !== null && viewport && viewport.contentHeight > 0) {
        anchor.rootContentHeight = viewport.contentHeight - headerHeight;
      }
      if (geometryChanged) {
        anchor.index = layout.index;
        anchor.y = layout.y;
        anchor.height = layout.height;
        const resetKinds = cohortOpen
          ? (["viewport", "content"] as const).filter((kind) => !cohort[kind])
          : (["viewport", "content"] as const);
        arm(5, resetKinds);
      }
      markMeasurement("row", geometryChanged);
    },
    observeHeader(height: number, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      if (!validNumber(height) || height < 0) return;
      const geometryChanged = changed(headerHeight, height);
      headerHeight = height;
      if (anchor && anchor.rootY === null) anchor.rootY = anchor.y - height;
      if (anchor && anchor.rootContentHeight === null && viewport && viewport.contentHeight > 0) {
        anchor.rootContentHeight = viewport.contentHeight - height;
      }
      if (geometryChanged) arm(6, ["row", "viewport", "content"]);
      markMeasurement("header", geometryChanged);
    },
    observeContentSize(height: number, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      if (!validNumber(height) || height < 0) return;
      const previous = viewport?.contentHeight ?? null;
      viewport = {
        offsetY: viewport?.offsetY ?? 0,
        viewportHeight: viewport?.viewportHeight ?? 0,
        contentHeight: height,
      };
      if (anchor && anchor.rootContentHeight === null && anchor.rootY !== null
        && headerHeight !== null) {
        anchor.rootContentHeight = height - headerHeight;
      }
      const geometryChanged = changed(previous, height);
      if (geometryChanged) arm(7, ["row", "viewport"]);
      markMeasurement("content", geometryChanged);
    },
    observeViewportLayout(height: number, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      if (!validNumber(height) || height < 0) return;
      const previous = viewport?.viewportHeight ?? null;
      viewport = {
        offsetY: viewport?.offsetY ?? 0,
        viewportHeight: height,
        contentHeight: viewport?.contentHeight ?? 0,
      };
      const geometryChanged = changed(previous, height);
      if (geometryChanged) arm(8, ["row", "content"]);
      markMeasurement("viewport", geometryChanged);
    },
    observeScroll(next: DocsListViewportLayout, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      if (!next || !validNumber(next.offsetY) || !validNumber(next.viewportHeight)
        || !validNumber(next.contentHeight)) return;
      const previous = viewport;
      viewport = { ...next };
      viewportOffsetMeasured = true;
      const viewportChanged = previous !== null && changed(previous.viewportHeight, next.viewportHeight);
      const contentChanged = previous !== null && changed(previous.contentHeight, next.contentHeight);
      const offsetChanged = previous !== null && changed(previous.offsetY, next.offsetY);
      if (viewportChanged || contentChanged) arm(9, ["row"]);
      // A current scroll event carries the current viewport/content geometry
      // even when their dimensions are unchanged.  An unchanged offset from
      // the previous cohort remains stale until a later native snapshot.
      markMeasurement("viewport", viewportChanged || offsetChanged || previous === null);
      markMeasurement("content", contentChanged || offsetChanged || previous === null);
    },
    observeSnapshot(snapshot: DocsListMeasuredSnapshot, token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return false;
      const row = snapshot?.row;
      if (!row || !validNumber(row.index) || !validNumber(row.y) || !validNumber(row.height)
        || row.height < 0 || !validNumber(snapshot.headerHeight) || snapshot.headerHeight < 0
        || !validNumber(snapshot.viewportHeight) || snapshot.viewportHeight <= 0
        || !validNumber(snapshot.contentHeight) || snapshot.contentHeight <= 0) return false;
      if (!anchor || anchor.key !== row.key) return false;
      const previousRow = rows.get(row.key);
      const rowChanged = !previousRow
        || previousRow.index !== row.index
        || changed(previousRow.y, row.y)
        || changed(previousRow.height, row.height)
        || previousRow.dataRevision !== dataRevision;
      const headerChanged = changed(headerHeight, snapshot.headerHeight);
      const previousViewport = viewport;
      const viewportChanged = !previousViewport
        || changed(previousViewport.viewportHeight, snapshot.viewportHeight)
        || changed(previousViewport.contentHeight, snapshot.contentHeight);
      rows.set(row.key, { ...row, dataRevision });
      headerHeight = snapshot.headerHeight;
      viewport = {
        offsetY: viewport?.offsetY ?? 0,
        viewportHeight: snapshot.viewportHeight,
        contentHeight: snapshot.contentHeight,
      };
      if (anchor.viewportY === null) anchor.viewportY = row.y - viewport.offsetY;
      anchor.index = row.index;
      anchor.y = row.y;
      anchor.height = row.height;
      anchor.dataRevision = dataRevision;
      anchor.dataSignature = dataSignature;
      anchor.rootY = row.y - snapshot.headerHeight;
      anchor.rootContentHeight = snapshot.contentHeight - snapshot.headerHeight;
      if (rowChanged || headerChanged || viewportChanged) {
        arm(13, ["row", "header", "viewport", "content"]);
      }
      // These values are one native measurement sequence.  A numerically
      // unchanged value is still fresh for this sequence.
      invalidKinds.clear();
      cohort.row = true;
      cohort.header = true;
      cohort.viewport = true;
      cohort.content = true;
      cohortOpen = true;
      return true;
    },
    capture(key: string, token: DocsListMeasurementToken) {
      if (!acceptsToken(token) || !focused || !rootActive || userGesture === true || !key) return false;
      const row = rows.get(key);
      const currentViewport = viewport;
      const viewportY = row && currentViewport && viewportOffsetMeasured
        ? row.y - currentViewport.offsetY
        : null;
      if (!row || !currentViewport || viewportY === null
        || currentViewport.viewportHeight <= 0 || currentViewport.contentHeight <= 0) return false;
      anchor = {
        key,
        index: row.index,
        y: row.y,
        height: row.height,
        viewportY,
        rootY: headerHeight === null ? null : row.y - headerHeight,
        rootContentHeight: headerHeight === null
          ? null
          : currentViewport.contentHeight - headerHeight,
        dataRevision,
        dataSignature,
        needsRestore: true,
        lastAppliedRevision: null,
        seedAppliedRevision: null,
      };
      layoutRevision += 1;
      cohortOpen = false;
      cohort.row = Boolean(row);
      cohort.header = headerHeight !== null;
      cohort.viewport = Boolean(currentViewport && currentViewport.viewportHeight > 0);
      cohort.content = Boolean(currentViewport && currentViewport.contentHeight > 0);
      invalidKinds.clear();
      lifecycle("capture", 0);
      return true;
    },
    beginUserGesture(token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      userGesture = true;
      drop(10);
    },
    cancelUserGesture(token: DocsListMeasurementToken) {
      if (!acceptsToken(token)) return;
      userGesture = false;
    },
    trySeedRestore(
      token: DocsListMeasurementToken,
      geometry: DocsListSeedRestoreGeometry,
      scrollToOffset: (offset: number) => boolean,
    ): DocsListSeedRestoreResult {
      if (!acceptsToken(token) || !focused || !rootActive || userGesture || !anchor?.needsRestore) {
        return "not-ready";
      }
      if (!validNumber(geometry.headerHeight) || geometry.headerHeight < 0
        || !validNumber(geometry.viewportHeight) || geometry.viewportHeight <= 0
        || !validNumber(geometry.contentHeight) || geometry.contentHeight <= 0) {
        return "not-ready";
      }
      const row = rows.get(anchor.key);
      if (!row || row.index < 0 || row.dataRevision !== dataRevision
        || anchor.dataRevision !== dataRevision || anchor.dataSignature !== dataSignature
        || row.index !== anchor.index) {
        return "invalid";
      }
      if (anchor.rootY === null || anchor.rootContentHeight === null
        || anchor.viewportY === null) return "not-ready";
      const currentRootContentHeight = geometry.contentHeight - geometry.headerHeight;
      if (!validNumber(currentRootContentHeight) || currentRootContentHeight <= 0) return "not-ready";
      // A seed is safe only while the measured content below the header still
      // proves the same row geometry as the capture.  A changed data order or
      // root extent has no row measurement to disambiguate, so drop it rather
      // than scrolling a different row into view.
      if (changed(anchor.rootContentHeight, currentRootContentHeight)) return "invalid";
      if (anchor.seedAppliedRevision === layoutRevision) return "not-ready";
      headerHeight = geometry.headerHeight;
      viewport = {
        offsetY: viewport?.offsetY ?? 0,
        viewportHeight: geometry.viewportHeight,
        contentHeight: geometry.contentHeight,
      };
      const targetOffset = Math.min(
        Math.max(0, anchor.rootY + geometry.headerHeight - anchor.viewportY),
        Math.max(0, geometry.contentHeight - geometry.viewportHeight),
      );
      if (!validNumber(targetOffset)) return "not-ready";
      if (!scrollToOffset(targetOffset)) return "not-ready";
      anchor.seedAppliedRevision = layoutRevision;
      lifecycle("seed", 14, true, targetOffset);
      return "applied";
    },
    dropUnresolved(token: DocsListMeasurementToken, reasonCode = 15) {
      if (!acceptsToken(token) || !anchor) return false;
      drop(reasonCode);
      return true;
    },
    tryRestore(token: DocsListMeasurementToken, scrollToOffset: (offset: number) => boolean) {
      if (!acceptsToken(token) || !focused || !rootActive || userGesture || !anchor?.needsRestore
        || anchor.viewportY === null || headerHeight === null || !viewport
        || viewport.viewportHeight <= 0 || viewport.contentHeight <= 0) return false;
      if (!cohortOpen || !cohort.row || !cohort.header || !cohort.viewport || !cohort.content) return false;
      const row = rows.get(anchor.key);
      if (!row || row.dataRevision !== dataRevision || row.index < 0) return false;
      const targetOffset = Math.min(
        Math.max(0, row.y - anchor.viewportY),
        Math.max(0, viewport.contentHeight - viewport.viewportHeight),
      );
      if (!validNumber(targetOffset)
        || anchor.lastAppliedRevision === layoutRevision) return false;
      lifecycle("execute", 0, true);
      if (!scrollToOffset(targetOffset)) return false;
      anchor.needsRestore = false;
      anchor.lastAppliedRevision = layoutRevision;
      cohortOpen = false;
      lifecycle("complete", 0, true);
      return true;
    },
  };
}
