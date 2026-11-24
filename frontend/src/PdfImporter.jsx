import React, { useEffect, useMemo, useRef, useState } from "react";

const UNITS = ["mm", "cm", "m", "ft", "in"];
const METHODS = [
  { value: "auto", label: "Auto (Vector / Raster)" },
  { value: "vector", label: "Vector (PyMuPDF)" },
  { value: "raster", label: "Raster (OpenCV)" },
  { value: "vlm", label: "Experimental VLM (Placeholder)" },
];
const SHEET_TYPE_OPTIONS = [
  { value: "floor_plan", label: "Floor Plan" },
  { value: "roof_plan", label: "Roof Plan" },
  { value: "elevation", label: "Elevation" },
  { value: "section", label: "Section" },
  { value: "cover_toc", label: "Cover / TOC" },
  { value: "lot_site", label: "Lot / Site Plan" },
  { value: "schedule_detail", label: "Schedule / Detail" },
  { value: "unknown", label: "Unknown / Plan" },
];

function statusColor(status) {
  if (status === "approved") return "#45c9a8";
  if (status === "rejected") return "#e06c75";
  return "#efc37d";
}

function sheetBadgeInfo(page) {
  const st = page.sheet_type || "unknown";
  if (st === "floor_plan") {
    return {
      label: page.viewport_count > 1 ? `FLOOR PLAN (${page.viewport_count})` : "FLOOR PLAN",
      cls: "sheet-floor",
    };
  }
  if (st === "roof_plan") return { label: "ROOF PLAN", cls: "sheet-roof" };
  if (st === "elevation") return { label: "ELEVATION", cls: "sheet-elev" };
  if (st === "section") return { label: "SECTION", cls: "sheet-sect" };
  if (st === "cover_toc") return { label: "SKIP: COVER", cls: "sheet-skip" };
  if (st === "lot_site") return { label: "SKIP: SITE", cls: "sheet-skip" };
  if (st === "schedule_detail") return { label: "SKIP: SCHEDULE", cls: "sheet-skip" };
  return { label: "PLAN", cls: "sheet-unknown" };
}

export default function PdfImporter({ project, onClose, onTransferred }) {
  const pid = encodeURIComponent(project.project_id);
  const svgRef = useRef(null);

  const [imports, setImports] = useState([]);
  const [doc, setDoc] = useState(null);
  const [pageNum, setPageNum] = useState(1);
  // inspect | calibrate | place_column | draw_region | set_origin
  const [mode, setMode] = useState("inspect");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // Calibration draft state
  const [calP1, setCalP1] = useState(null);
  const [calP2, setCalP2] = useState(null);
  const [knownDist, setKnownDist] = useState("6.0");
  const [unit, setUnit] = useState("m");

  // Detection & manual placement controls
  const [method, setMethod] = useState("auto");
  const [showDetectSettings, setShowDetectSettings] = useState(false);
  const [minSpanPct, setMinSpanPct] = useState("28");
  const [borderMarginPct, setBorderMarginPct] = useState("4");
  const [clusterTolPx, setClusterTolPx] = useState("8");
  const [mergeDashed, setMergeDashed] = useState(true);
  const [detectLabels, setDetectLabels] = useState(true);
  const [manualWidthMm, setManualWidthMm] = useState("400");
  const [manualDepthMm, setManualDepthMm] = useState("400");
  const [manualGridRef, setManualGridRef] = useState("");
  const [mergeMode, setMergeMode] = useState("merge");
  const [selectedId, setSelectedId] = useState(null);

  // Plan Regions & Draw Region state
  const [activeRegionId, setActiveRegionId] = useState("");
  const [masterSourceRegionId, setMasterSourceRegionId] = useState("");
  const [regionCorner1, setRegionCorner1] = useState(null);
  const [hoverPt, setHoverPt] = useState(null);
  const [newRegionName, setNewRegionName] = useState("");
  const [newRegionLevel, setNewRegionLevel] = useState("");
  const [levelEdits, setLevelEdits] = useState({});

  async function apiJson(url, options) {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  async function loadImportsList(selectDocId = null) {
    try {
      const list = await apiJson(`/api/projects/${pid}/pdf-imports`);
      setImports(list);
      const targetId = selectDocId || (list.length > 0 ? list[0].doc_id : null);
      if (targetId) {
        const full = await apiJson(`/api/projects/${pid}/pdf-imports/${encodeURIComponent(targetId)}`);
        setDoc(full);
        setPageNum((prev) => Math.min(Math.max(1, prev), full.page_count));
      } else {
        setDoc(null);
      }
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    loadImportsList();
  }, [project.project_id]);

  const activePage = useMemo(() => {
    if (!doc) return null;
    return doc.pages.find((p) => p.page_number === pageNum) || doc.pages[0] || null;
  }, [doc, pageNum]);

  const isElevationOrSection =
    activePage?.sheet_type === "elevation" || activePage?.sheet_type === "section";

  const activeCalibration = useMemo(() => {
    if (!doc || !activePage) return null;
    return doc.calibrations?.[String(activePage.page_number)] || null;
  }, [doc, activePage]);

  const pageRegions = useMemo(
    () => (doc ? (doc.regions || []).filter((r) => r.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const activeRegion = useMemo(
    () => pageRegions.find((r) => r.region_id === activeRegionId) || null,
    [pageRegions, activeRegionId]
  );

  const masterSourceRegions = useMemo(() => {
    if (!doc) return [];
    return (doc.regions || []).filter((r) => {
      if (r.region_id === activeRegionId) return false;
      const hasApprovedGrid = (doc.grid_candidates || []).some(
        (g) =>
          g.status === "approved" &&
          (g.region_id === r.region_id ||
            (!g.region_id && g.page_number === r.page_number))
      );
      const cal = doc.calibrations?.[String(r.page_number)];
      const hasOrigin = Boolean(r.origin_px || cal?.origin_px);
      return hasApprovedGrid && hasOrigin;
    });
  }, [doc, activeRegionId]);

  useEffect(() => {
    if (pageRegions.length > 0) {
      if (!pageRegions.some((r) => r.region_id === activeRegionId)) {
        setActiveRegionId(pageRegions[0].region_id);
      }
    } else {
      setActiveRegionId("");
    }
    setRegionCorner1(null);
  }, [doc?.doc_id, pageNum, pageRegions.length]);

  useEffect(() => {
    if (masterSourceRegions.length > 0) {
      if (!masterSourceRegions.some((r) => r.region_id === masterSourceRegionId)) {
        setMasterSourceRegionId(masterSourceRegions[0].region_id);
      }
    } else {
      setMasterSourceRegionId("");
    }
  }, [masterSourceRegions]);

  useEffect(() => {
    if (activeCalibration) {
      setCalP1(activeCalibration.p1);
      setCalP2(activeCalibration.p2);
      setKnownDist(String(activeCalibration.known_distance));
      setUnit(activeCalibration.unit);
    } else {
      setCalP1(null);
      setCalP2(null);
    }
    setSelectedId(null);
  }, [doc?.doc_id, pageNum, activeCalibration?.calibrated_at]);

  const pageGrids = useMemo(
    () => (doc ? doc.grid_candidates.filter((g) => g.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const pageCols = useMemo(
    () => (doc ? doc.column_candidates.filter((c) => c.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const pageLevels = useMemo(() => {
    if (!doc) return [];
    return (doc.detected_levels || [])
      .filter((l) => l.page_number === pageNum)
      .slice()
      .sort((a, b) => a.elevation_m - b.elevation_m);
  }, [doc, pageNum]);

  const approvedLevelsCount = useMemo(
    () => pageLevels.filter((l) => l.engineer_approved || l.review_status === "approved").length,
    [pageLevels]
  );

  const counts = useMemo(() => {
    const all = [...pageGrids, ...pageCols];
    return {
      total: all.length,
      pending: all.filter((x) => x.status === "pending").length,
      approved: all.filter((x) => x.status === "approved").length,
      rejected: all.filter((x) => x.status === "rejected").length,
      approvedGrids: pageGrids.filter((x) => x.status === "approved").length,
      approvedCols: pageCols.filter((x) => x.status === "approved").length,
    };
  }, [pageGrids, pageCols]);

  const draftPixelDist = useMemo(() => {
    if (!calP1 || !calP2) return null;
    return Math.hypot(calP2.x_px - calP1.x_px, calP2.y_px - calP1.y_px);
  }, [calP1, calP2]);

  async function handleUpload(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const uploaded = await apiJson(`/api/projects/${pid}/pdf-imports`, {
        method: "POST",
        body: fd,
      });
      setDoc(uploaded);
      const firstFloor = uploaded.pages.find((p) => p.sheet_type === "floor_plan");
      setPageNum(firstFloor ? firstFloor.page_number : 1);
      setMode("inspect");
      setNotice(
        `Uploaded ${uploaded.filename} (${uploaded.page_count} page${uploaded.page_count > 1 ? "s" : ""}, ${uploaded.regions?.length || 0} auto-region(s)).`
      );
      await loadImportsList(uploaded.doc_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSelectDoc(docId) {
    if (!docId) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const full = await apiJson(`/api/projects/${pid}/pdf-imports/${encodeURIComponent(docId)}`);
      setDoc(full);
      setPageNum(1);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleChangePageType(newSheetType) {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sheet_type: newSheetType }),
        }
      );
      setDoc(updated);
      setNotice(`Page ${activePage.page_number} type set to ${newSheetType}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function getSvgPoint(e) {
    if (!svgRef.current || !activePage) return null;
    const rect = svgRef.current.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const x_px = Number((((e.clientX - rect.left) / rect.width) * activePage.width_px).toFixed(2));
    const y_px = Number((((e.clientY - rect.top) / rect.height) * activePage.height_px).toFixed(2));
    return {
      x_px: Math.min(Math.max(0, x_px), activePage.width_px),
      y_px: Math.min(Math.max(0, y_px), activePage.height_px),
    };
  }

  function handleCanvasMove(e) {
    if (mode !== "draw_region" || !regionCorner1) return;
    const pt = getSvgPoint(e);
    if (pt) setHoverPt(pt);
  }

  async function handleCanvasClick(e) {
    if (!doc || !activePage || busy) return;
    const pt = getSvgPoint(e);
    if (!pt) return;

    if (mode === "calibrate") {
      setError("");
      if (!calP1 || (calP1 && calP2)) {
        setCalP1(pt);
        setCalP2(null);
        setNotice(`Calibration Point 1 set at (${pt.x_px}, ${pt.y_px}) px. Click Point 2.`);
      } else {
        setCalP2(pt);
        const dist = Math.hypot(pt.x_px - calP1.x_px, pt.y_px - calP1.y_px).toFixed(1);
        setNotice(`Calibration Point 2 set (${dist} px). Enter known real distance and apply scale.`);
      }
      return;
    }

    if (mode === "draw_region") {
      setError("");
      if (!regionCorner1) {
        setRegionCorner1(pt);
        setHoverPt(pt);
        setNotice(`Region Corner 1 set at (${pt.x_px}, ${pt.y_px}) px. Click opposite corner.`);
        return;
      }
      const x0 = Math.min(regionCorner1.x_px, pt.x_px);
      const y0 = Math.min(regionCorner1.y_px, pt.y_px);
      const w = Math.abs(pt.x_px - regionCorner1.x_px);
      const h = Math.abs(pt.y_px - regionCorner1.y_px);
      if (w < 20 || h < 20) {
        setError("Drawn region is too small. Click two opposite corners of the plan area.");
        setRegionCorner1(null);
        setHoverPt(null);
        return;
      }
      setBusy(true);
      try {
        const regName =
          newRegionName.trim() || `Page ${activePage.page_number} Region ${pageRegions.length + 1}`;
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              page_number: activePage.page_number,
              name: regName,
              floor_level: newRegionLevel.trim(),
              x_px: x0,
              y_px: y0,
              width_px: w,
              height_px: h,
            }),
          }
        );
        setDoc(updated);
        const created = updated.regions[updated.regions.length - 1];
        if (created) setActiveRegionId(created.region_id);
        setRegionCorner1(null);
        setHoverPt(null);
        setNewRegionName("");
        setMode("inspect");
        setNotice(`Created region '${regName}' (${Math.round(w)}×${Math.round(h)} px).`);
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "set_origin") {
      const targetReg = activeRegion || pageRegions[0] || null;
      if (!targetReg) {
        setError("Create or select a Plan Region first (use 'Draw Region') before setting (0,0) origin.");
        return;
      }
      setBusy(true);
      setError("");
      try {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(targetReg.region_id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ origin_px: pt }),
          }
        );
        setDoc(updated);
        setActiveRegionId(targetReg.region_id);
        setMode("inspect");
        setNotice(`Origin (0,0) for '${targetReg.name}' set at (${pt.x_px}, ${pt.y_px}) px.`);
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "place_column") {
      setBusy(true);
      setError("");
      try {
        const w = parseFloat(manualWidthMm) || 400;
        const d = parseFloat(manualDepthMm) || 400;
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/candidates/columns`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              page_number: activePage.page_number,
              region_id: activeRegionId || null,
              x_px: pt.x_px,
              y_px: pt.y_px,
              grid: manualGridRef.trim(),
              width_mm: w,
              depth_mm: d,
              status: "approved",
            }),
          }
        );
        setDoc(updated);
        const newest = updated.column_candidates[updated.column_candidates.length - 1];
        setSelectedId(newest?.id || null);
        setNotice(`Placed manual column ${newest?.column_id || ""} at (${pt.x_px}, ${pt.y_px}) px.`);
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
    }
  }

  async function handleDeleteRegion(regionId) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(regionId)}`,
        { method: "DELETE" }
      );
      setDoc(updated);
      if (activeRegionId === regionId) setActiveRegionId("");
      setNotice("Deleted plan region.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateRegionMeta(regionId, patch) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(regionId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        }
      );
      setDoc(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyCalibration(confirmImmediately = false) {
    if (!doc || !activePage || !calP1 || !calP2) {
      setError("Click two points on the plan image first.");
      return;
    }
    const distVal = parseFloat(knownDist);
    if (!Number.isFinite(distVal) || distVal <= 0) {
      setError("Enter a positive known distance.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/calibrate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            p1: calP1,
            p2: calP2,
            known_distance: distVal,
            unit,
            confirmed: confirmImmediately,
          }),
        }
      );
      setDoc(updated);
      const cal = updated.calibrations[String(activePage.page_number)];
      setNotice(
        `Scale ${confirmImmediately ? "confirmed" : "calibrated"}: ${cal.real_distance_m} m over ${cal.pixel_distance} px (${cal.meters_per_px.toFixed(6)} m/px).`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirmCalibration() {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/confirm-calibration`,
        { method: "POST" }
      );
      setDoc(updated);
      setNotice(`Page ${activePage.page_number} scale calibration confirmed by engineer.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleRunExtraction() {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const minSpanVal = Math.min(100, Math.max(1, parseFloat(minSpanPct) || 28));
      const borderMarginVal = Math.min(40, Math.max(0, parseFloat(borderMarginPct) || 4));
      const clusterTolVal = Math.min(100, Math.max(1, parseFloat(clusterTolPx) || 8));

      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/extract`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            method,
            replace_unreviewed: true,
            region_id: activeRegionId || null,
            options: {
              min_span_ratio: Number((minSpanVal / 100).toFixed(4)),
              border_margin_ratio: Number((borderMarginVal / 100).toFixed(4)),
              cluster_tolerance_px: clusterTolVal,
              merge_dashed_segments: mergeDashed,
              detect_grid_labels: detectLabels,
            },
          }),
        }
      );
      setDoc(res.document);
      setMode("inspect");
      setNotice(
        res.notice ||
          `Detection (${res.method_used}) found ${res.grids_detected} grids and ${res.columns_detected} columns.`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyMasterGrid() {
    if (!doc || !activeRegionId || !masterSourceRegionId) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(activeRegionId)}/apply-master-grid`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_region_id: masterSourceRegionId }),
        }
      );
      setDoc(res.document);
      setNotice(res.notice);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleExtractLevels() {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/extract-levels`,
        { method: "POST" }
      );
      setDoc(res.document);
      setNotice(res.notice || `Extracted ${res.levels_detected} floor level(s).`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateLevel(levelId, patch) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/levels/${encodeURIComponent(levelId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        }
      );
      setDoc(updated);
      setLevelEdits((prev) => {
        const next = { ...prev };
        delete next[levelId];
        return next;
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleTransferLevels() {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/transfer-levels`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            page_number: activePage.page_number,
            replace_existing: true,
          }),
        }
      );
      setDoc(res.document);
      setNotice(res.notice);
      if (onTransferred) onTransferred(res);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleReviewCandidate(candidateId, patch) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/candidates/${encodeURIComponent(candidateId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        }
      );
      setDoc(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBulkReview(status) {
    if (!doc) return;
    const ids = [...pageGrids, ...pageCols].map((x) => x.id);
    if (ids.length === 0) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/candidates/bulk-review`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_ids: ids, status }),
        }
      );
      setDoc(updated);
      setNotice(`Marked ${ids.length} candidate(s) on page ${pageNum} as ${status}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleTransfer() {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/transfer`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            page_number: activePage.page_number,
            region_id: activeRegionId || null,
            merge_mode: mergeMode,
          }),
        }
      );
      setDoc(res.document);
      setNotice(res.notice);
      if (onTransferred) {
        onTransferred(res);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const canTransfer = Boolean(activeCalibration?.confirmed && counts.approved > 0 && !busy);
  const canCopyMasterGrid = Boolean(activeRegion?.origin_px && masterSourceRegionId && !busy);
  const imageUrl =
    doc && activePage
      ? `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/image`
      : null;

  return (
    <section className="pdf-importer">
      <div className="pdf-topbar">
        <div>
          <label>MODULE 02 · PDF PLAN IMPORTER</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="pdf-topbar-actions">
          <label className="pdf-upload-btn">
            {busy ? "Working..." : "Upload PDF Plan"}
            <input
              type="file"
              accept=".pdf,application/pdf"
              disabled={busy}
              onChange={handleUpload}
            />
          </label>
          {imports.length > 0 && (
            <select
              className="pdf-select"
              value={doc?.doc_id || ""}
              onChange={(e) => handleSelectDoc(e.target.value)}
              disabled={busy}
            >
              {imports.map((item) => (
                <option key={item.doc_id} value={item.doc_id}>
                  {item.filename} ({item.page_count}p · {item.doc_id})
                </option>
              ))}
            </select>
          )}
          <button className="secondary" onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {error && <div className="pdf-alert error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {!doc || !activePage ? (
        <div className="pdf-empty">
          <h3>No PDF plan uploaded yet</h3>
          <p>
            Upload a multi-page architectural or structural PDF set to auto-classify sheets (Cover,
            Floor Plans, Elevations, Sections, Schedules), calibrate scale, extract grids, columns,
            and floor levels, and transfer engineer-approved geometry into <code>ProjectState</code>.
          </p>
        </div>
      ) : (
        <>
          {/* Architectural Set Page Navigator Strip */}
          <div className="pdf-page-strip" role="navigation" aria-label="PDF Sheet Navigator">
            {doc.pages.map((pg) => {
              const badge = sheetBadgeInfo(pg);
              const isActive = pg.page_number === activePage.page_number;
              return (
                <button
                  key={pg.page_number}
                  type="button"
                  className={`pdf-sheet-pill ${isActive ? "active" : ""} ${badge.cls}`}
                  onClick={() => setPageNum(pg.page_number)}
                  title={pg.sheet_title || `Page ${pg.page_number}`}
                >
                  <span className="sheet-num">
                    {pg.sheet_number || `P${pg.page_number}`}
                  </span>
                  <span className={`sheet-tag ${badge.cls}`}>{badge.label}</span>
                </button>
              );
            })}
          </div>

          <div className="pdf-workspace">
            {/* Left / Main Canvas Column */}
            <div className="pdf-canvas-col">
              {/* Page & Interaction Toolbar */}
              <div className="pdf-toolbar">
                <div className="pdf-pager">
                  <button
                    className="secondary"
                    disabled={busy || pageNum <= 1}
                    onClick={() => setPageNum((p) => Math.max(1, p - 1))}
                  >
                    Prev
                  </button>
                  <span className="pdf-page-label">
                    {activePage.sheet_number ? `${activePage.sheet_number} · ` : ""}
                    Page {activePage.page_number} / {doc.page_count}
                  </span>
                  <button
                    className="secondary"
                    disabled={busy || pageNum >= doc.page_count}
                    onClick={() => setPageNum((p) => Math.min(doc.page_count, p + 1))}
                  >
                    Next
                  </button>
                  <span className={`pdf-badge class-${activePage.classification}`}>
                    {activePage.classification.toUpperCase()}
                  </span>
                  <select
                    className={`pdf-type-select ${sheetBadgeInfo(activePage).cls}`}
                    value={activePage.sheet_type || "unknown"}
                    onChange={(e) => handleChangePageType(e.target.value)}
                    disabled={busy}
                    aria-label="Override page sheet type"
                    title="Change page type (e.g., switch to Elevation/Section or Floor Plan)"
                  >
                    {SHEET_TYPE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                  {activePage.sheet_title && (
                    <span className="pdf-meta-dim" title={activePage.sheet_title}>
                      {activePage.sheet_title}
                    </span>
                  )}
                </div>

                {!isElevationOrSection && (
                  <div className="pdf-modes">
                    <button
                      className={`secondary ${mode === "inspect" ? "active" : ""}`}
                      onClick={() => {
                        setMode("inspect");
                        setRegionCorner1(null);
                      }}
                    >
                      Inspect
                    </button>
                    <button
                      className={`secondary ${mode === "calibrate" ? "active" : ""}`}
                      onClick={() => {
                        setMode("calibrate");
                        setRegionCorner1(null);
                      }}
                    >
                      Calibrate (2-Pt)
                    </button>
                    <button
                      className={`secondary ${mode === "place_column" ? "active" : ""}`}
                      onClick={() => {
                        setMode("place_column");
                        setRegionCorner1(null);
                      }}
                    >
                      Place Column
                    </button>
                    <button
                      className={`secondary ${mode === "draw_region" ? "active" : ""}`}
                      onClick={() => {
                        setMode("draw_region");
                        setRegionCorner1(null);
                        setNotice("Draw Region mode: click Corner 1 then opposite Corner 2 on the sheet.");
                      }}
                    >
                      Draw Region
                    </button>
                    <button
                      className={`secondary ${mode === "set_origin" ? "active" : ""}`}
                      onClick={() => {
                        setMode("set_origin");
                        setRegionCorner1(null);
                        setNotice("Set Origin (0,0) mode: click the (0,0) grid intersection for the active region.");
                      }}
                    >
                      Set Origin (0,0)
                    </button>
                  </div>
                )}
              </div>

              {/* Plan Regions Panel (shown on plan pages & whenever regions exist) */}
              {!isElevationOrSection && (
                <div className="pdf-regions-bar">
                  <div className="pdf-regions-top">
                    <span className="pdf-group-title">
                      Plan Regions ({pageRegions.length})
                    </span>
                    <div className="pdf-inline-form">
                      <label className="pdf-field-chip">
                        <span>Active Target Region</span>
                        <select
                          className="pdf-select"
                          value={activeRegionId}
                          onChange={(e) => setActiveRegionId(e.target.value)}
                          disabled={busy}
                        >
                          <option value="">Full Sheet (No Crop)</option>
                          {pageRegions.map((reg) => (
                            <option key={reg.region_id} value={reg.region_id}>
                              {reg.name}
                              {reg.floor_level ? ` [${reg.floor_level}]` : ""}
                              {reg.origin_px ? " ◎" : ""}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                  </div>

                  {mode === "draw_region" && (
                    <div className="pdf-inline-form">
                      <input
                        type="text"
                        value={newRegionName}
                        onChange={(e) => setNewRegionName(e.target.value)}
                        placeholder="New region name (e.g. Second Floor Plan)"
                        style={{ minWidth: 210 }}
                      />
                      <input
                        type="text"
                        value={newRegionLevel}
                        onChange={(e) => setNewRegionLevel(e.target.value)}
                        placeholder="Floor level (e.g. 2F)"
                        style={{ width: 120 }}
                      />
                      <small className="pdf-hint">
                        {regionCorner1
                          ? "Corner 1 set — click opposite corner to save region."
                          : "Click two opposite corners on the plan image."}
                      </small>
                    </div>
                  )}

                  {pageRegions.length > 0 ? (
                    <div className="pdf-region-cards">
                      {pageRegions.map((reg) => {
                        const isAct = reg.region_id === activeRegionId;
                        return (
                          <div
                            key={reg.region_id}
                            className={`pdf-region-card ${isAct ? "active" : ""}`}
                            onClick={() => setActiveRegionId(reg.region_id)}
                          >
                            <div className="pdf-region-card-main">
                              <strong>{reg.name}</strong>
                              <small>
                                {reg.floor_level ? `Level: ${reg.floor_level} · ` : ""}
                                {Math.round(reg.width_px)}×{Math.round(reg.height_px)} px ·{" "}
                                {reg.origin_px
                                  ? `Origin (${Math.round(reg.origin_px.x_px)}, ${Math.round(reg.origin_px.y_px)})`
                                  : "Origin not set"}
                              </small>
                            </div>
                            <div className="pdf-cand-actions">
                              <input
                                type="text"
                                className="region-level-input"
                                defaultValue={reg.floor_level || ""}
                                placeholder="Level"
                                title="Floor level tag (press Enter or blur to save)"
                                onClick={(ev) => ev.stopPropagation()}
                                onBlur={(ev) => {
                                  if (ev.target.value !== (reg.floor_level || "")) {
                                    handleUpdateRegionMeta(reg.region_id, {
                                      floor_level: ev.target.value,
                                    });
                                  }
                                }}
                              />
                              <button
                                type="button"
                                className="mini-btn on-reject"
                                disabled={busy}
                                onClick={(ev) => {
                                  ev.stopPropagation();
                                  handleDeleteRegion(reg.region_id);
                                }}
                                title="Delete region"
                              >
                                Delete
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <small className="pdf-hint">
                      No regions on this page yet. Click <strong>Draw Region</strong> to define a
                      viewport box, or switch the Page Type dropdown above to{" "}
                      <strong>Floor Plan</strong>.
                    </small>
                  )}

                  {activeRegion && (
                    <div className="pdf-master-grid-row">
                      {masterSourceRegions.length > 0 ? (
                        <>
                          <select
                            className="pdf-select"
                            value={masterSourceRegionId}
                            onChange={(e) => setMasterSourceRegionId(e.target.value)}
                            disabled={busy}
                            aria-label="Master grid source region"
                          >
                            {masterSourceRegions.map((src) => (
                              <option key={src.region_id} value={src.region_id}>
                                Source: P{src.page_number} · {src.name}
                              </option>
                            ))}
                          </select>
                          <button
                            className="secondary pdf-launch-btn"
                            disabled={!canCopyMasterGrid}
                            onClick={handleApplyMasterGrid}
                            title={
                              activeRegion.origin_px
                                ? "Copy approved master grids into this region and probe intersection columns"
                                : "Click 'Set Origin (0,0)' on this viewport first"
                            }
                          >
                            Copy Master Grid to This Region
                          </button>
                        </>
                      ) : (
                        <small className="pdf-hint">
                          Approve grids &amp; set (0,0) origins on a floor region to enable Copy
                          Master Grid.
                        </small>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Elevation / Section Level Extraction Strip */}
              {isElevationOrSection ? (
                <div className="pdf-controls-strip elev-strip">
                  <div className="pdf-control-group">
                    <span className="pdf-group-title">
                      Elevation / Section Datum Extraction
                    </span>
                    <div className="pdf-inline-form">
                      <button disabled={busy} onClick={handleExtractLevels}>
                        Extract Floor Levels
                      </button>
                      <small className="pdf-hint">
                        Detects horizontal datum lines (≥45% width), matches left-side level names
                        and right-side elevations, and flags duplicate tags.
                      </small>
                    </div>
                  </div>
                </div>
              ) : (
                <>
                  {/* Calibration & Detection Strip */}
                  <div className="pdf-controls-strip">
                    <div className="pdf-control-group">
                      <span className="pdf-group-title">
                        Scale Calibration{" "}
                        <em
                          className={`pdf-cal-status ${
                            activeCalibration?.confirmed
                              ? "confirmed"
                              : activeCalibration
                                ? "draft"
                                : "none"
                          }`}
                        >
                          {activeCalibration?.confirmed
                            ? `Confirmed (${activeCalibration.meters_per_px.toFixed(5)} m/px)`
                            : activeCalibration
                              ? `Unconfirmed (${activeCalibration.meters_per_px.toFixed(5)} m/px)`
                              : "Uncalibrated"}
                        </em>
                      </span>
                      <div className="pdf-inline-form">
                        <input
                          type="number"
                          step="any"
                          min="0.0001"
                          value={knownDist}
                          onChange={(e) => setKnownDist(e.target.value)}
                          placeholder="Distance"
                          aria-label="Known distance"
                        />
                        <select
                          value={unit}
                          onChange={(e) => setUnit(e.target.value)}
                          aria-label="Unit"
                        >
                          {UNITS.map((u) => (
                            <option key={u} value={u}>
                              {u}
                            </option>
                          ))}
                        </select>
                        <button
                          className="secondary"
                          disabled={busy || !calP1 || !calP2}
                          onClick={() => handleApplyCalibration(false)}
                        >
                          Apply Scale
                        </button>
                        <button
                          disabled={busy || !activeCalibration || activeCalibration.confirmed}
                          onClick={handleConfirmCalibration}
                        >
                          {activeCalibration?.confirmed ? "Scale Confirmed" : "Confirm Scale"}
                        </button>
                      </div>
                      <small className="pdf-hint">
                        {draftPixelDist
                          ? `Selected span: ${draftPixelDist.toFixed(1)} px (P1: ${calP1.x_px}, ${calP1.y_px} -> P2: ${calP2.x_px}, ${calP2.y_px})`
                          : "Switch to Calibrate (2-Pt) and click two known reference points on the plan."}
                      </small>
                    </div>

                    <div className="pdf-control-group">
                      <span className="pdf-group-title">Element Detection</span>
                      <div className="pdf-inline-form">
                        <select
                          value={method}
                          onChange={(e) => setMethod(e.target.value)}
                          aria-label="Detection method"
                        >
                          {METHODS.map((m) => (
                            <option key={m.value} value={m.value}>
                              {m.label}
                            </option>
                          ))}
                        </select>
                        <button className="secondary" disabled={busy} onClick={handleRunExtraction}>
                          2. Detect Grids &amp; Columns
                        </button>
                      </div>
                      {mode === "place_column" && (
                        <div className="pdf-inline-form manual-opts">
                          <input
                            type="number"
                            min="100"
                            step="50"
                            value={manualWidthMm}
                            onChange={(e) => setManualWidthMm(e.target.value)}
                            placeholder="Width mm"
                            title="Column width (mm)"
                          />
                          <input
                            type="number"
                            min="100"
                            step="50"
                            value={manualDepthMm}
                            onChange={(e) => setManualDepthMm(e.target.value)}
                            placeholder="Depth mm"
                            title="Column depth (mm)"
                          />
                          <input
                            type="text"
                            value={manualGridRef}
                            onChange={(e) => setManualGridRef(e.target.value)}
                            placeholder="Grid ref (opt)"
                            title="Optional grid reference"
                          />
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Collapsible Detection Settings Bar */}
                  <div className="pdf-detect-settings">
                    <button
                      type="button"
                      className="pdf-settings-toggle"
                      onClick={() => setShowDetectSettings((v) => !v)}
                    >
                      <span>{showDetectSettings ? "▾" : "▸"} Detection Settings</span>
                      <small>
                        Min Span {minSpanPct}% · Border Margin {borderMarginPct}% · Cluster Tol{" "}
                        {clusterTolPx}px
                      </small>
                    </button>
                    {showDetectSettings && (
                      <div className="pdf-settings-fields">
                        <label className="pdf-field-chip">
                          <span>Min Grid Span (%)</span>
                          <input
                            type="number"
                            min="5"
                            max="95"
                            step="1"
                            value={minSpanPct}
                            onChange={(e) => setMinSpanPct(e.target.value)}
                          />
                        </label>
                        <label className="pdf-field-chip">
                          <span>Border Margin (%)</span>
                          <input
                            type="number"
                            min="0"
                            max="25"
                            step="1"
                            value={borderMarginPct}
                            onChange={(e) => setBorderMarginPct(e.target.value)}
                          />
                        </label>
                        <label className="pdf-field-chip">
                          <span>Cluster Tolerance (px)</span>
                          <input
                            type="number"
                            min="1"
                            max="50"
                            step="1"
                            value={clusterTolPx}
                            onChange={(e) => setClusterTolPx(e.target.value)}
                          />
                        </label>
                        <label className="pdf-check-chip">
                          <input
                            type="checkbox"
                            checked={mergeDashed}
                            onChange={(e) => setMergeDashed(e.target.checked)}
                          />
                          <span>Merge dashed</span>
                        </label>
                        <label className="pdf-check-chip">
                          <input
                            type="checkbox"
                            checked={detectLabels}
                            onChange={(e) => setDetectLabels(e.target.checked)}
                          />
                          <span>Detect labels</span>
                        </label>
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Rendered PNG + Interactive SVG Overlay */}
              <div className={`pdf-stage mode-${mode}`}>
                <img
                  src={imageUrl}
                  alt={`${doc.filename} page ${activePage.page_number}`}
                  className="pdf-page-img"
                  draggable={false}
                />
                <svg
                  ref={svgRef}
                  className="pdf-overlay-svg"
                  viewBox={`0 0 ${activePage.width_px} ${activePage.height_px}`}
                  preserveAspectRatio="none"
                  onClick={handleCanvasClick}
                  onMouseMove={handleCanvasMove}
                >
                  {/* Saved PlanRegion Boxes & (0,0) Origin Crosshairs */}
                  {pageRegions.map((reg) => {
                    const isActiveReg = reg.region_id === activeRegionId;
                    const strokeW = Math.max(activePage.width_px * 0.0022, 1.8);
                    const fontSize = Math.max(activePage.width_px * 0.012, 12);
                    return (
                      <g key={reg.region_id} opacity={isActiveReg ? 0.96 : 0.52}>
                        <rect
                          x={reg.x_px}
                          y={reg.y_px}
                          width={Math.max(10, reg.width_px)}
                          height={Math.max(10, reg.height_px)}
                          fill={isActiveReg ? "#38bdf8" : "none"}
                          fillOpacity={isActiveReg ? 0.05 : 0}
                          stroke={isActiveReg ? "#38bdf8" : "#94a3b8"}
                          strokeWidth={strokeW}
                          strokeDasharray={`${strokeW * 4} ${strokeW * 3}`}
                        />
                        <text
                          x={reg.x_px + 12}
                          y={reg.y_px + fontSize + 8}
                          fill={isActiveReg ? "#38bdf8" : "#cbd5e1"}
                          fontSize={fontSize}
                          fontWeight="800"
                          stroke="#0b1220"
                          strokeWidth="3"
                          paintOrder="stroke"
                        >
                          {reg.name}
                          {reg.floor_level ? ` (${reg.floor_level})` : ""}
                        </text>
                        {reg.origin_px && (
                          <g>
                            <circle
                              cx={reg.origin_px.x_px}
                              cy={reg.origin_px.y_px}
                              r={Math.max(activePage.width_px * 0.008, 7)}
                              fill="none"
                              stroke="#45c9a8"
                              strokeWidth={strokeW}
                            />
                            <line
                              x1={reg.origin_px.x_px - 14}
                              y1={reg.origin_px.y_px}
                              x2={reg.origin_px.x_px + 14}
                              y2={reg.origin_px.y_px}
                              stroke="#45c9a8"
                              strokeWidth={strokeW}
                            />
                            <line
                              x1={reg.origin_px.x_px}
                              y1={reg.origin_px.y_px - 14}
                              x2={reg.origin_px.x_px}
                              y2={reg.origin_px.y_px + 14}
                              stroke="#45c9a8"
                              strokeWidth={strokeW}
                            />
                            <text
                              x={reg.origin_px.x_px + 12}
                              y={reg.origin_px.y_px - 8}
                              fill="#45c9a8"
                              fontSize={Math.max(activePage.width_px * 0.01, 11)}
                              fontWeight="800"
                              stroke="#0b1220"
                              strokeWidth="3"
                              paintOrder="stroke"
                            >
                              (0,0)
                            </text>
                          </g>
                        )}
                      </g>
                    );
                  })}

                  {/* Live Preview Box while Drawing a Region */}
                  {mode === "draw_region" && regionCorner1 && hoverPt && (
                    <rect
                      x={Math.min(regionCorner1.x_px, hoverPt.x_px)}
                      y={Math.min(regionCorner1.y_px, hoverPt.y_px)}
                      width={Math.abs(hoverPt.x_px - regionCorner1.x_px)}
                      height={Math.abs(hoverPt.y_px - regionCorner1.y_px)}
                      fill="#45c9a8"
                      fillOpacity={0.12}
                      stroke="#45c9a8"
                      strokeWidth={Math.max(activePage.width_px * 0.0025, 2)}
                      strokeDasharray="8 5"
                    />
                  )}

                  {/* Elevation / Section Horizontal Floor Level Datum Lines */}
                  {pageLevels.map((lvl) => {
                    const color = statusColor(lvl.review_status);
                    const strokeW = Math.max(activePage.width_px * 0.0022, 1.8);
                    const fontSize = Math.max(activePage.width_px * 0.012, 12);
                    return (
                      <g
                        key={lvl.level_id}
                        opacity={lvl.review_status === "rejected" ? 0.3 : 0.95}
                      >
                        <line
                          x1={0}
                          y1={lvl.y_px}
                          x2={activePage.width_px}
                          y2={lvl.y_px}
                          stroke={color}
                          strokeWidth={strokeW}
                          strokeDasharray={`${strokeW * 6} ${strokeW * 3}`}
                        />
                        <text
                          x={14}
                          y={Math.max(fontSize + 4, lvl.y_px - 6)}
                          fill={color}
                          fontSize={fontSize}
                          fontWeight="800"
                          stroke="#0b1220"
                          strokeWidth="3.5"
                          paintOrder="stroke"
                        >
                          {lvl.name}
                        </text>
                        <text
                          x={activePage.width_px - 14}
                          y={Math.max(fontSize + 4, lvl.y_px - 6)}
                          textAnchor="end"
                          fill={color}
                          fontSize={fontSize}
                          fontWeight="800"
                          stroke="#0b1220"
                          strokeWidth="3.5"
                          paintOrder="stroke"
                        >
                          {lvl.elevation_m >= 0 ? "+" : ""}
                          {lvl.elevation_m.toFixed(2)} m
                          {lvl.story_height_m !== null ? ` (Δ ${lvl.story_height_m.toFixed(2)}m)` : ""}
                        </text>
                      </g>
                    );
                  })}

                  {/* Grid Candidates */}
                  {pageGrids.map((g) => {
                    const color = statusColor(g.status);
                    const isSel = selectedId === g.id;
                    const strokeW = Math.max(activePage.width_px * 0.0022, 1.5);
                    const bubbleR = Math.max(activePage.width_px * 0.014, 11);
                    const topY = Math.min(g.y1_px, g.y2_px);
                    const leftX = Math.min(g.x1_px, g.x2_px);
                    const bx =
                      g.axis === "x"
                        ? Math.min(Math.max(bubbleR + 4, g.x1_px), activePage.width_px - bubbleR - 4)
                        : Math.max(bubbleR + 4, leftX - bubbleR - 2);
                    const by =
                      g.axis === "x"
                        ? Math.max(bubbleR + 4, topY - bubbleR - 2)
                        : Math.min(Math.max(bubbleR + 4, g.y1_px), activePage.height_px - bubbleR - 4);
                    return (
                      <g
                        key={g.id}
                        opacity={g.status === "rejected" ? 0.32 : 0.92}
                        onClick={(ev) => {
                          if (mode !== "inspect") return;
                          ev.stopPropagation();
                          setSelectedId(g.id);
                        }}
                      >
                        <line
                          x1={g.x1_px}
                          y1={g.y1_px}
                          x2={g.x2_px}
                          y2={g.y2_px}
                          stroke={color}
                          strokeWidth={isSel ? strokeW * 1.9 : strokeW}
                          strokeDasharray={`${strokeW * 5} ${strokeW * 3}`}
                        />
                        <circle
                          cx={bx}
                          cy={by}
                          r={bubbleR}
                          fill="#0b1220"
                          stroke={color}
                          strokeWidth={strokeW}
                        />
                        <text
                          x={bx}
                          y={by + bubbleR * 0.36}
                          textAnchor="middle"
                          fill={color}
                          fontSize={bubbleR * 0.95}
                          fontWeight="800"
                        >
                          {g.label}
                        </text>
                      </g>
                    );
                  })}

                  {/* Column Candidates */}
                  {pageCols.map((c) => {
                    const color = statusColor(c.status);
                    const isSel = selectedId === c.id;
                    const rw = Math.max(c.width_px, activePage.width_px * 0.015, 12);
                    const rh = Math.max(c.height_px, activePage.height_px * 0.015, 12);
                    const strokeW = Math.max(activePage.width_px * 0.0025, 1.8);
                    return (
                      <g
                        key={c.id}
                        opacity={c.status === "rejected" ? 0.3 : 0.96}
                        onClick={(ev) => {
                          if (mode !== "inspect") return;
                          ev.stopPropagation();
                          setSelectedId(c.id);
                        }}
                      >
                        <rect
                          x={c.x_px - rw / 2}
                          y={c.y_px - rh / 2}
                          width={rw}
                          height={rh}
                          fill={color}
                          fillOpacity={isSel ? 0.45 : 0.25}
                          stroke={color}
                          strokeWidth={isSel ? strokeW * 1.8 : strokeW}
                        />
                        <text
                          x={c.x_px + rw / 2 + 5}
                          y={c.y_px - rh / 2 + 4}
                          fill={color}
                          fontSize={Math.max(activePage.width_px * 0.013, 12)}
                          fontWeight="800"
                          stroke="#0b1220"
                          strokeWidth="3"
                          paintOrder="stroke"
                        >
                          {c.column_id}
                        </text>
                      </g>
                    );
                  })}

                  {/* 2-Point Calibration Overlay */}
                  {calP1 && !isElevationOrSection && (
                    <g className="cal-overlay">
                      {calP2 && (
                        <line
                          x1={calP1.x_px}
                          y1={calP1.y_px}
                          x2={calP2.x_px}
                          y2={calP2.y_px}
                          stroke="#38bdf8"
                          strokeWidth={Math.max(activePage.width_px * 0.003, 2.5)}
                        />
                      )}
                      <circle
                        cx={calP1.x_px}
                        cy={calP1.y_px}
                        r={Math.max(activePage.width_px * 0.007, 6)}
                        fill="#38bdf8"
                        stroke="#0b1220"
                        strokeWidth="2"
                      />
                      {calP2 && (
                        <circle
                          cx={calP2.x_px}
                          cy={calP2.y_px}
                          r={Math.max(activePage.width_px * 0.007, 6)}
                          fill="#38bdf8"
                          stroke="#0b1220"
                          strokeWidth="2"
                        />
                      )}
                    </g>
                  )}
                </svg>
              </div>
            </div>

            {/* Right Review & Transfer Panel */}
            <aside className="pdf-review-col">
              {isElevationOrSection ? (
                <>
                  <div className="pdf-review-header">
                    <div>
                      <label>FLOOR LEVELS · PAGE {activePage.page_number}</label>
                      <h3>
                        {approvedLevelsCount} Approved · {pageLevels.length} Detected
                      </h3>
                    </div>
                  </div>

                  <div className="pdf-review-list">
                    {pageLevels.length === 0 ? (
                      <p className="pdf-muted">
                        Click <strong>Extract Floor Levels</strong> to parse horizontal datum lines
                        and elevation tags on this sheet.
                      </p>
                    ) : (
                      pageLevels.map((lvl) => {
                        const draft = levelEdits[lvl.level_id] || {
                          name: lvl.name,
                          elevation_m: String(lvl.elevation_m),
                        };
                        const dirty =
                          draft.name !== lvl.name ||
                          String(draft.elevation_m) !== String(lvl.elevation_m);
                        return (
                          <div
                            key={lvl.level_id}
                            className={`pdf-level-card status-${lvl.review_status}`}
                          >
                            <div className="pdf-level-inputs">
                              <input
                                type="text"
                                value={draft.name}
                                onChange={(e) =>
                                  setLevelEdits((prev) => ({
                                    ...prev,
                                    [lvl.level_id]: { ...draft, name: e.target.value },
                                  }))
                                }
                                aria-label="Level name"
                              />
                              <div className="pdf-level-num-row">
                                <input
                                  type="number"
                                  step="0.05"
                                  value={draft.elevation_m}
                                  onChange={(e) =>
                                    setLevelEdits((prev) => ({
                                      ...prev,
                                      [lvl.level_id]: { ...draft, elevation_m: e.target.value },
                                    }))
                                  }
                                  aria-label="Elevation in meters"
                                />
                                <span>m</span>
                                {lvl.story_height_m !== null && (
                                  <small className="pdf-hint">
                                    Δ {lvl.story_height_m.toFixed(2)} m
                                  </small>
                                )}
                              </div>
                            </div>
                            {lvl.warnings && lvl.warnings.length > 0 && (
                              <div className="pdf-level-warn">{lvl.warnings.join(" ")}</div>
                            )}
                            <div className="pdf-cand-actions">
                              {dirty && (
                                <button
                                  className="mini-btn"
                                  disabled={busy}
                                  onClick={() =>
                                    handleUpdateLevel(lvl.level_id, {
                                      name: draft.name,
                                      elevation_m: parseFloat(draft.elevation_m),
                                    })
                                  }
                                >
                                  Save
                                </button>
                              )}
                              <button
                                className={`mini-btn ${lvl.engineer_approved ? "on-approve" : ""}`}
                                disabled={busy}
                                onClick={() =>
                                  handleUpdateLevel(lvl.level_id, {
                                    name: draft.name,
                                    elevation_m: parseFloat(draft.elevation_m),
                                    review_status: "approved",
                                    engineer_approved: true,
                                  })
                                }
                              >
                                Approve
                              </button>
                              <button
                                className={`mini-btn ${lvl.review_status === "rejected" ? "on-reject" : ""}`}
                                disabled={busy}
                                onClick={() =>
                                  handleUpdateLevel(lvl.level_id, {
                                    review_status: "rejected",
                                    engineer_approved: false,
                                  })
                                }
                              >
                                Reject
                              </button>
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>

                  <div className="pdf-transfer-box">
                    <button
                      className="pdf-transfer-btn"
                      disabled={busy || approvedLevelsCount === 0}
                      onClick={handleTransferLevels}
                    >
                      Transfer Approved Levels to Project ({approvedLevelsCount})
                    </button>
                    <small className="pdf-hint">
                      Writes engineer-approved levels into <code>ProjectState.levels</code> sorted
                      by <code>elevation_m</code>.
                    </small>
                  </div>
                </>
              ) : (
                <>
                  <div className="pdf-review-header">
                    <div>
                      <label>STAGED REVIEW · PAGE {activePage.page_number}</label>
                      <h3>
                        {counts.approved} Approved · {counts.pending} Pending · {counts.rejected}{" "}
                        Rejected
                      </h3>
                    </div>
                    <div className="pdf-bulk-actions">
                      <button
                        className="secondary"
                        disabled={busy || counts.total === 0}
                        onClick={() => handleBulkReview("approved")}
                      >
                        Approve All
                      </button>
                      <button
                        className="secondary"
                        disabled={busy || counts.total === 0}
                        onClick={() => handleBulkReview("rejected")}
                      >
                        Reject All
                      </button>
                    </div>
                  </div>

                  <div className="pdf-review-list">
                    <h4>Grid Lines ({pageGrids.length})</h4>
                    {pageGrids.length === 0 ? (
                      <p className="pdf-muted">No staged grids on this page yet.</p>
                    ) : (
                      pageGrids.map((g) => (
                        <div
                          key={g.id}
                          className={`pdf-cand-row status-${g.status} ${selectedId === g.id ? "selected" : ""}`}
                          onClick={() => setSelectedId(g.id)}
                        >
                          <div className="pdf-cand-info">
                            <strong>
                              {g.axis.toUpperCase()}-Grid {g.label}
                            </strong>
                            <small>
                              {g.coord_m !== null ? `${g.coord_m.toFixed(3)} m` : `${g.coord_px} px`}{" "}
                              · {g.source} ({Math.round(g.confidence * 100)}%)
                              {g.transferred ? " · Transferred" : ""}
                            </small>
                          </div>
                          <div className="pdf-cand-actions">
                            <button
                              className={`mini-btn ${g.status === "approved" ? "on-approve" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(g.id, { status: "approved" });
                              }}
                            >
                              Accept
                            </button>
                            <button
                              className={`mini-btn ${g.status === "rejected" ? "on-reject" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(g.id, { status: "rejected" });
                              }}
                            >
                              Reject
                            </button>
                          </div>
                        </div>
                      ))
                    )}

                    <h4>Columns ({pageCols.length})</h4>
                    {pageCols.length === 0 ? (
                      <p className="pdf-muted">
                        No staged columns yet. Run detection or switch to Place Column mode.
                      </p>
                    ) : (
                      pageCols.map((c) => (
                        <div
                          key={c.id}
                          className={`pdf-cand-row status-${c.status} ${selectedId === c.id ? "selected" : ""}`}
                          onClick={() => setSelectedId(c.id)}
                        >
                          <div className="pdf-cand-info">
                            <strong>
                              {c.column_id} {c.grid ? `(${c.grid})` : ""}
                            </strong>
                            <small>
                              {c.x_m !== null && c.y_m !== null
                                ? `(${c.x_m.toFixed(2)}, ${c.y_m.toFixed(2)}) m`
                                : `(${c.x_px}, ${c.y_px}) px`}{" "}
                              · {Math.round(c.width_mm)}×{Math.round(c.depth_mm)} mm · {c.source}
                              {c.transferred ? " · Transferred" : ""}
                            </small>
                          </div>
                          <div className="pdf-cand-actions">
                            <button
                              className={`mini-btn ${c.status === "approved" ? "on-approve" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(c.id, { status: "approved" });
                              }}
                            >
                              Accept
                            </button>
                            <button
                              className={`mini-btn ${c.status === "rejected" ? "on-reject" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(c.id, { status: "rejected" });
                              }}
                            >
                              Reject
                            </button>
                          </div>
                        </div>
                      ))
                    )}
                  </div>

                  <div className="pdf-transfer-box">
                    <div className="pdf-inline-form">
                      <select
                        value={mergeMode}
                        onChange={(e) => setMergeMode(e.target.value)}
                        disabled={busy}
                        aria-label="Transfer merge mode"
                      >
                        <option value="merge">Merge with existing project geometry</option>
                        <option value="replace_geometry">Replace project grids &amp; columns</option>
                      </select>
                    </div>
                    <button
                      className="pdf-transfer-btn"
                      disabled={!canTransfer}
                      onClick={handleTransfer}
                    >
                      Transfer Approved to Project ({counts.approvedGrids}G · {counts.approvedCols}C)
                    </button>
                    <small className="pdf-hint">
                      {!activeCalibration?.confirmed
                        ? "Confirm scale calibration on this page to unlock transfer."
                        : counts.approved === 0
                          ? "Approve at least one grid or column to enable transfer."
                          : "Only engineer-approved elements will be written to ProjectState."}
                    </small>
                  </div>
                </>
              )}
            </aside>
          </div>
        </>
      )}
    </section>
  );
}
