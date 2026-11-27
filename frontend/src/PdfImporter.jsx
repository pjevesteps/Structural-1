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
const OPENING_CATEGORIES = [
  { value: "stairwell", label: "Stairwell", color: "#f59e0b" },
  { value: "airwell", label: "Airwell / Air Vent", color: "#38bdf8" },
  { value: "shaft_other", label: "Shaft / Other", color: "#c084fc" },
];
const COLUMN_FILL_MODES = [
  { value: "shaded_or_hatched", label: "Shaded / Hatched Only" },
  { value: "gray_shaded_only", label: "Gray Shaded Only" },
  { value: "any_filled", label: "Any Filled" },
];

function openingColor(category) {
  const found = OPENING_CATEGORIES.find((c) => c.value === category);
  return found ? found.color : "#c084fc";
}

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

function nextGridLabel(current, axis) {
  const raw = (current || "").trim();
  if (!raw) return axis === "x" ? "A" : "1";
  // Numeric with optional prime (e.g. "1'" -> "1", "1" -> "2")
  const numPrime = raw.match(/^(\d+)'$/);
  if (numPrime) return numPrime[1];
  const numOnly = raw.match(/^(\d+)$/);
  if (numOnly) return String(parseInt(numOnly[1], 10) + 1);
  // Single or multi uppercase letter (A -> B -> ... -> Z -> AA)
  const alphaOnly = raw.match(/^([A-Z]+)$/);
  if (alphaOnly) {
    const chars = alphaOnly[1].split("");
    let i = chars.length - 1;
    while (i >= 0) {
      if (chars[i] !== "Z") {
        chars[i] = String.fromCharCode(chars[i].charCodeAt(0) + 1);
        return chars.join("");
      }
      chars[i] = "A";
      i--;
    }
    return "A" + chars.join("");
  }
  return raw;
}

export default function PdfImporter({ project, onClose, onTransferred }) {
  const pid = encodeURIComponent(project.project_id);
  const viewportRef = useRef(null);
  const svgRef = useRef(null);
  const panStateRef = useRef({ active: false, startX: 0, startY: 0, origX: 0, origY: 0, moved: false });

  const [imports, setImports] = useState([]);
  const [doc, setDoc] = useState(null);
  const [manualLvlName, setManualLvlName] = useState("");
  const [manualLvlElev, setManualLvlElev] = useState("0.00");
  const [manualLvlApprove, setManualLvlApprove] = useState(true);
  const [manualLvlYPx, setManualLvlYPx] = useState(0);
  const [projectLevels, setProjectLevels] = useState(() =>
    Array.isArray(project?.levels) ? project.levels : []
  );
  const [pageNum, setPageNum] = useState(1);
  // inspect | calibrate | draw_region | set_origin | trace_grid_x | trace_grid_y | stamp_master_grid | place_column | trace_opening
  const [mode, setMode] = useState("inspect");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // Zoom & Pan state
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [spacePressed, setSpacePressed] = useState(false);
  const [shiftPressed, setShiftPressed] = useState(false);
  const [isPanning, setIsPanning] = useState(false);
  const [hoverPt, setHoverPt] = useState(null);

  // Calibration draft state
  const [calP1, setCalP1] = useState(null);
  const [calP2, setCalP2] = useState(null);
  const [knownDist, setKnownDist] = useState("6.0");
  const [unit, setUnit] = useState("m");

  // Detection controls
  const [method, setMethod] = useState("auto");
  const [showDetectSettings, setShowDetectSettings] = useState(false);
  const [minSpanPct, setMinSpanPct] = useState("28");
  const [borderMarginPct, setBorderMarginPct] = useState("4");
  const [clusterTolPx, setClusterTolPx] = useState("8");
  const [mergeDashed, setMergeDashed] = useState(true);
  const [detectLabels, setDetectLabels] = useState(true);
  const [requireGridLabel, setRequireGridLabel] = useState(true);
  const [columnFillMode, setColumnFillMode] = useState("shaded_or_hatched");
  const [minColSizeM, setMinColSizeM] = useState("0.18");
  const [maxColSizeM, setMaxColSizeM] = useState("1.10");
  const [minColFillPct, setMinColFillPct] = useState("35");
  const [maxColAspect, setMaxColAspect] = useState("2.5");
  const [requireNearGrid, setRequireNearGrid] = useState(false);

  // Manual tracing & column/opening inputs
  const [nextGridXLabel, setNextGridXLabel] = useState("A");
  const [nextGridYLabel, setNextGridYLabel] = useState("1");
  const [undoStack, setUndoStack] = useState([]);
  const [redoStack, setRedoStack] = useState([]);
  const [manualWidthMm, setManualWidthMm] = useState("400");
  const [manualDepthMm, setManualDepthMm] = useState("400");
  const [manualGridRef, setManualGridRef] = useState("");
  const [mergeMode, setMergeMode] = useState("merge");
  const [autoAddRoof, setAutoAddRoof] = useState(true);
  const [roofFramingStyle, setRoofFramingStyle] = useState("full_grid");
  const [defaultRoofHeightM, setDefaultRoofHeightM] = useState("3.00");
  const [selectedId, setSelectedId] = useState(null);

  // Plan Regions & Openings state
  const [activeRegionId, setActiveRegionId] = useState("");
  const [copyOpeningsSourceId, setCopyOpeningsSourceId] = useState("");
  const [regionCorner1, setRegionCorner1] = useState(null);
  const [newRegionName, setNewRegionName] = useState("");
  const [newRegionType, setNewRegionType] = useState("plan");
  const [newRegionLevel, setNewRegionLevel] = useState("");
  const [openingCategory, setOpeningCategory] = useState("stairwell");
  const [openingLabel, setOpeningLabel] = useState("STAIR-1");
  const [openingVertices, setOpeningVertices] = useState([]);
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

  async function loadProjectLevels() {
    try {
      const fullProj = await apiJson(`/api/projects/${pid}`);
      const lvls = Array.isArray(fullProj?.levels)
        ? [...fullProj.levels].sort((a, b) => a.elevation_m - b.elevation_m)
        : [];
      setProjectLevels(lvls);
    } catch {
      // keep existing projectLevels if fetch fails
    }
  }

  useEffect(() => {
    loadImportsList();
    loadProjectLevels();
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

  const pageGrids = useMemo(
    () => (doc ? doc.grid_candidates.filter((g) => g.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const pageCols = useMemo(
    () => (doc ? doc.column_candidates.filter((c) => c.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const pageOpenings = useMemo(
    () => (doc ? (doc.openings || []).filter((o) => o.page_number === pageNum) : []),
    [doc, pageNum]
  );

  const pageLevels = useMemo(() => {
    if (!doc) return [];
    return (doc.detected_levels || [])
      .filter((l) => l.page_number === pageNum)
      .slice()
      .sort((a, b) => a.elevation_m - b.elevation_m);
  }, [doc, pageNum]);

  const hasElevationRegion = useMemo(
    () => pageRegions.some((r) => (r.region_type || "plan") === "elevation"),
    [pageRegions]
  );

  const showLevelsPanel = isElevationOrSection || hasElevationRegion || pageLevels.length > 0;

  const availableLevelOptions = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const lv of projectLevels) {
      const id = lv.id || lv.name;
      if (id && !seen.has(id.toUpperCase())) {
        seen.add(id.toUpperCase());
        out.push({ id, label: `${lv.name || lv.id} (${lv.elevation_m} m)` });
      }
    }
    for (const dl of doc?.detected_levels || []) {
      if (dl.engineer_approved || dl.review_status === "approved") {
        const id = (dl.name || "").trim();
        if (id && !seen.has(id.toUpperCase())) {
          seen.add(id.toUpperCase());
          out.push({ id, label: `${id} (${dl.elevation_m} m)` });
        }
      }
    }
    return out;
  }, [projectLevels, doc?.detected_levels]);

  // Active-region grids for intersection snapping
  const activeRegionGrids = useMemo(() => {
    if (!activeRegionId) return pageGrids.filter((g) => g.status !== "rejected");
    const scoped = pageGrids.filter(
      (g) => g.status !== "rejected" && (g.region_id === activeRegionId || !g.region_id)
    );
    return scoped;
  }, [pageGrids, activeRegionId]);

  const gridIntersections = useMemo(() => {
    const xs = activeRegionGrids.filter((g) => g.axis === "x");
    const ys = activeRegionGrids.filter((g) => g.axis === "y");
    const pts = [];
    for (const gx of xs) {
      for (const gy of ys) {
        pts.push({
          x_px: gx.coord_px,
          y_px: gy.coord_px,
          xLabel: gx.label,
          yLabel: gy.label,
          gridRef: `${gx.label}-${gy.label}`,
        });
      }
    }
    return pts;
  }, [activeRegionGrids]);

  const openingSourceRegions = useMemo(() => {
    if (!doc) return [];
    return (doc.regions || []).filter((r) => {
      if (r.region_id === activeRegionId) return false;
      return (doc.openings || []).some(
        (o) =>
          (o.engineer_approved || o.review_status === "approved") &&
          (o.region_id === r.region_id || (!o.region_id && o.page_number === r.page_number))
      );
    });
  }, [doc, activeRegionId]);

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
      approvedOpenings: pageOpenings.filter((o) => o.engineer_approved || o.review_status === "approved").length,
    };
  }, [pageGrids, pageCols, pageOpenings]);

  const draftPixelDist = useMemo(() => {
    if (!calP1 || !calP2) return null;
    return Math.hypot(calP2.x_px - calP1.x_px, calP2.y_px - calP1.y_px);
  }, [calP1, calP2]);

  // Reset zoom/pan and transient drafts when switching page or document
  function resetView() {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }

  useEffect(() => {
    resetView();
    setRegionCorner1(null);
    setOpeningVertices([]);
    setHoverPt(null);
    if (pageRegions.length > 0) {
      if (!pageRegions.some((r) => r.region_id === activeRegionId)) {
        setActiveRegionId(pageRegions[0].region_id);
      }
    } else {
      setActiveRegionId("");
    }
  }, [doc?.doc_id, pageNum, pageRegions.length]);

  useEffect(() => {
    if (openingSourceRegions.length > 0) {
      if (!openingSourceRegions.some((r) => r.region_id === copyOpeningsSourceId)) {
        setCopyOpeningsSourceId(openingSourceRegions[0].region_id);
      }
    } else {
      setCopyOpeningsSourceId("");
    }
  }, [openingSourceRegions]);

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

  // Wheel zoom centered on cursor (non-passive listener)
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    function onWheel(e) {
      e.preventDefault();
      const vpRect = vp.getBoundingClientRect();
      const mouseX = e.clientX - vpRect.left;
      const mouseY = e.clientY - vpRect.top;
      const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
      setZoom((prevZoom) => {
        const nextZoom = Math.min(8, Math.max(0.5, Number((prevZoom * factor).toFixed(4))));
        const ratio = nextZoom / prevZoom;
        setPan((prevPan) => ({
          x: mouseX - (mouseX - prevPan.x) * ratio,
          y: mouseY - (mouseY - prevPan.y) * ratio,
        }));
        return nextZoom;
      });
    }
    vp.addEventListener("wheel", onWheel, { passive: false });
    return () => vp.removeEventListener("wheel", onWheel);
  }, [doc?.doc_id, pageNum]);

  useEffect(() => {
    setUndoStack([]);
    setRedoStack([]);
  }, [doc?.doc_id]);

  // Keyboard shortcuts (Space to pan, Shift to bypass snap, Ctrl+Z/Ctrl+Y for grid Undo/Redo, Enter/Backspace/Esc for polygon tracing)
  useEffect(() => {
    function onKeyDown(e) {
      const tag = e.target?.tagName?.toLowerCase();
      const inInput =
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        Boolean(e.target?.isContentEditable);
      if (e.code === "Space" && !inInput) {
        e.preventDefault();
        setSpacePressed(true);
      }
      if (e.key === "Shift") setShiftPressed(true);
      if (inInput) return;

      if ((e.ctrlKey || e.metaKey) && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "z" && !e.shiftKey) {
          e.preventDefault();
          handleUndoGrid();
          return;
        }
        if (k === "y" || (k === "z" && e.shiftKey)) {
          e.preventDefault();
          handleRedoGrid();
          return;
        }
      }

      if (e.key === "Escape") {
        setOpeningVertices([]);
        setRegionCorner1(null);
        setNotice("Cancelled active draft.");
      } else if (e.key === "Backspace" && mode === "trace_opening" && openingVertices.length > 0) {
        e.preventDefault();
        setOpeningVertices((prev) => prev.slice(0, -1));
      } else if (e.key === "Enter" && mode === "trace_opening" && openingVertices.length >= 3) {
        e.preventDefault();
        commitPolygonOpening(openingVertices);
      }
    }
    function onKeyUp(e) {
      if (e.code === "Space") setSpacePressed(false);
      if (e.key === "Shift") setShiftPressed(false);
    }
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [
    mode,
    openingVertices,
    openingCategory,
    openingLabel,
    activeRegionId,
    activePage,
    doc,
    undoStack,
    redoStack,
    busy,
  ]);

  function zoomBy(factor) {
    const vp = viewportRef.current;
    if (!vp) {
      setZoom((z) => Math.min(8, Math.max(0.5, z * factor)));
      return;
    }
    const vpRect = vp.getBoundingClientRect();
    const cx = vpRect.width / 2;
    const cy = vpRect.height / 2;
    setZoom((prevZoom) => {
      const nextZoom = Math.min(8, Math.max(0.5, Number((prevZoom * factor).toFixed(4))));
      const ratio = nextZoom / prevZoom;
      setPan((prevPan) => ({
        x: cx - (cx - prevPan.x) * ratio,
        y: cy - (cy - prevPan.y) * ratio,
      }));
      return nextZoom;
    });
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
      screenScale: rect.width / activePage.width_px,
    };
  }

  // Compute nearest grid intersection within 18 screen-px (unless Shift is held)
  const snappedIntersection = useMemo(() => {
    if (!hoverPt || shiftPressed) return null;
    if (mode !== "place_column" && mode !== "trace_opening" && mode !== "set_origin" && mode !== "stamp_master_grid") {
      return null;
    }
    const scale = hoverPt.screenScale || 1;
    const maxImgDist = 18 / Math.max(scale, 0.05);
    let best = null;
    let bestDist = Infinity;
    for (const inter of gridIntersections) {
      const d = Math.hypot(hoverPt.x_px - inter.x_px, hoverPt.y_px - inter.y_px);
      if (d <= maxImgDist && d < bestDist) {
        bestDist = d;
        best = inter;
      }
    }
    return best;
  }, [hoverPt, shiftPressed, mode, gridIntersections]);

  // Live cursor readout in plan meters (if calibrated or master grid scale exists)
  const hoverMeters = useMemo(() => {
    if (!hoverPt || !activePage) return null;
    const ppm = activeCalibration?.pixels_per_m || doc?.master_grid?.source_pixels_per_meter || null;
    const origin = activeRegion?.origin_px || activeCalibration?.origin_px || null;
    if (!ppm || !origin) return null;
    return {
      x_m: ((hoverPt.x_px - origin.x_px) / ppm).toFixed(2),
      y_m: ((origin.y_px - hoverPt.y_px) / ppm).toFixed(2),
    };
  }, [hoverPt, activePage, activeCalibration, doc?.master_grid, activeRegion]);

  function handleViewportMouseDown(e) {
    const canPan =
      e.button === 1 || (e.button === 0 && (mode === "inspect" || spacePressed));
    if (!canPan) return;
    e.preventDefault();
    panStateRef.current = {
      active: true,
      startX: e.clientX,
      startY: e.clientY,
      origX: pan.x,
      origY: pan.y,
      moved: false,
    };
    setIsPanning(true);
  }

  function handleViewportMouseMove(e) {
    if (panStateRef.current.active) {
      const dx = e.clientX - panStateRef.current.startX;
      const dy = e.clientY - panStateRef.current.startY;
      if (Math.hypot(dx, dy) > 4) panStateRef.current.moved = true;
      setPan({ x: panStateRef.current.origX + dx, y: panStateRef.current.origY + dy });
      return;
    }
    const pt = getSvgPoint(e);
    if (pt) setHoverPt(pt);
  }

  function handleViewportMouseUp() {
    if (panStateRef.current.active) {
      panStateRef.current.active = false;
      setIsPanning(false);
    }
  }

  async function commitPolygonOpening(verts) {
    if (!doc || !activePage || verts.length < 3) return;
    setBusy(true);
    setError("");
    try {
      const label = openingLabel.trim() || `${openingCategory.toUpperCase()}-${pageOpenings.length + 1}`;
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/openings`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            page_number: activePage.page_number,
            region_id: activeRegionId || null,
            floor_level: activeRegion?.floor_level || activeRegion?.name || "",
            category: openingCategory,
            label,
            polygon_px: verts.map((v) => ({ x_px: v.x_px, y_px: v.y_px })),
          }),
        }
      );
      setDoc(updated);
      setOpeningVertices([]);
      setNotice(`Saved ${openingCategory} opening '${label}' (${verts.length} vertices).`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleCanvasClick(e) {
    if (panStateRef.current.moved) {
      panStateRef.current.moved = false;
      return;
    }
    if (!doc || !activePage || busy || spacePressed) return;
    const pt = getSvgPoint(e);
    if (!pt) return;

    if (mode === "pick_datum") {
      setError("");
      setManualLvlYPx(pt.y_px);
      // If 2+ levels already exist on this page with distinct y_px, estimate elevation linearly
      const refLvls = pageLevels.filter((l) => typeof l.y_px === "number" && l.y_px > 0);
      if (refLvls.length >= 2) {
        const sortedRef = [...refLvls].sort((a, b) => a.elevation_m - b.elevation_m);
        const low = sortedRef[0];
        const high = sortedRef[sortedRef.length - 1];
        if (Math.abs(low.y_px - high.y_px) > 10) {
          const est =
            low.elevation_m +
            ((low.y_px - pt.y_px) / (low.y_px - high.y_px)) * (high.elevation_m - low.elevation_m);
          setManualLvlElev(est.toFixed(2));
        }
      }
      setNotice(
        `Picked datum at y = ${pt.y_px.toFixed(0)} px — enter Level Label & Elevation (m) and click + Add Level Manually.`
      );
      return;
    }

    if (mode === "calibrate") {
      setError("");
      if (!calP1 || (calP1 && calP2)) {
        setCalP1({ x_px: pt.x_px, y_px: pt.y_px });
        setCalP2(null);
        setNotice(`Calibration Point 1 set at (${pt.x_px}, ${pt.y_px}) px. Click Point 2.`);
      } else {
        setCalP2({ x_px: pt.x_px, y_px: pt.y_px });
        const dist = Math.hypot(pt.x_px - calP1.x_px, pt.y_px - calP1.y_px).toFixed(1);
        setNotice(`Calibration Point 2 set (${dist} px). Enter known real distance and apply scale.`);
      }
      return;
    }

    if (mode === "draw_region") {
      setError("");
      if (!regionCorner1) {
        setRegionCorner1({ x_px: pt.x_px, y_px: pt.y_px });
        setNotice(`Region Corner 1 set at (${pt.x_px}, ${pt.y_px}) px. Click opposite corner.`);
        return;
      }
      const x0 = Math.min(regionCorner1.x_px, pt.x_px);
      const y0 = Math.min(regionCorner1.y_px, pt.y_px);
      const w = Math.abs(pt.x_px - regionCorner1.x_px);
      const h = Math.abs(pt.y_px - regionCorner1.y_px);
      if (w < 20 || h < 20) {
        setError("Drawn region is too small. Click two opposite corners.");
        setRegionCorner1(null);
        return;
      }
      setBusy(true);
      try {
        const elevCount = pageRegions.filter((r) => (r.region_type || "plan") === "elevation").length;
        const regName =
          newRegionName.trim() ||
          (newRegionType === "elevation"
            ? `Elevation ${elevCount + 1}`
            : `Page ${activePage.page_number} Region ${pageRegions.length + 1}`);
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              page_number: activePage.page_number,
              name: regName,
              region_type: newRegionType,
              floor_level: newRegionType === "elevation" ? "" : newRegionLevel.trim(),
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
        setNewRegionName("");
        setMode("inspect");
        setNotice(`Created region '${regName}'.`);
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
        setError("Create or select a Plan Region first before setting (0,0) origin.");
        return;
      }
      const anchor = snappedIntersection
        ? { x_px: snappedIntersection.x_px, y_px: snappedIntersection.y_px }
        : { x_px: pt.x_px, y_px: pt.y_px };
      setBusy(true);
      setError("");
      try {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(targetReg.region_id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ origin_px: anchor }),
          }
        );
        setDoc(updated);
        setActiveRegionId(targetReg.region_id);
        setMode("inspect");
        setNotice(`Origin (0,0) for '${targetReg.name}' set at (${anchor.x_px}, ${anchor.y_px}) px.`);
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "trace_grid_x" || mode === "trace_grid_y") {
      const isX = mode === "trace_grid_x";
      const label = (isX ? nextGridXLabel : nextGridYLabel).trim();
      if (!label) {
        setError("Enter a Next Grid Label first.");
        return;
      }
      const rx0 = activeRegion ? activeRegion.x_px : 0;
      const ry0 = activeRegion ? activeRegion.y_px : 0;
      const rx1 = activeRegion ? activeRegion.x_px + activeRegion.width_px : activePage.width_px;
      const ry1 = activeRegion ? activeRegion.y_px + activeRegion.height_px : activePage.height_px;
      const p1 = isX ? { x_px: pt.x_px, y_px: ry0 } : { x_px: rx0, y_px: pt.y_px };
      const p2 = isX ? { x_px: pt.x_px, y_px: ry1 } : { x_px: rx1, y_px: pt.y_px };
      const payload = {
        page_number: activePage.page_number,
        region_id: activeRegionId || null,
        element_type: isX ? "grid_x" : "grid_y",
        label,
        p1,
        p2,
      };
      const prevIds = new Set((doc.grid_candidates || []).map((g) => g.id));
      const nextLabelAfter = nextGridLabel(label, isX ? "x" : "y");

      setBusy(true);
      setError("");
      try {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/manual-grid`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }
        );
        const created =
          (updated.grid_candidates || []).find((g) => !prevIds.has(g.id)) ||
          updated.grid_candidates?.[updated.grid_candidates.length - 1];
        setDoc(updated);
        if (isX) setNextGridXLabel(nextLabelAfter);
        else setNextGridYLabel(nextLabelAfter);
        if (created?.id) {
          setUndoStack((prev) => [
            ...prev,
            {
              action: "add_grid",
              element_id: created.id,
              payload,
              prevNextLabel: label,
              nextLabelAfter,
            },
          ]);
          setRedoStack([]);
        }
        setNotice(`Traced ${isX ? "Grid X (V)" : "Grid Y (H)"} '${label}'.`);
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "stamp_master_grid") {
      const targetReg = activeRegion || pageRegions[0] || null;
      if (!targetReg) {
        setError("Select a target Plan Region before stamping the Master Grid.");
        return;
      }
      const anchor = snappedIntersection
        ? { x_px: snappedIntersection.x_px, y_px: snappedIntersection.y_px }
        : { x_px: pt.x_px, y_px: pt.y_px };
      setBusy(true);
      setError("");
      try {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(targetReg.region_id)}/stamp-master-grid`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              anchor_px: anchor,
              clear_existing_grids: true,
            }),
          }
        );
        setDoc(updated);
        setMode("place_column");
        setNotice(
          `Stamped Master Grid onto '${targetReg.name}' at anchor (${anchor.x_px}, ${anchor.y_px}) px. Ready to snap columns!`
        );
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "place_column") {
      const targetPt = snappedIntersection
        ? { x_px: snappedIntersection.x_px, y_px: snappedIntersection.y_px }
        : { x_px: pt.x_px, y_px: pt.y_px };
      const gridRef = snappedIntersection ? snappedIntersection.gridRef : manualGridRef.trim();

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
              x_px: targetPt.x_px,
              y_px: targetPt.y_px,
              grid: gridRef,
              width_mm: w,
              depth_mm: d,
              status: "approved",
            }),
          }
        );
        setDoc(updated);
        const newest = updated.column_candidates[updated.column_candidates.length - 1];
        setSelectedId(newest?.id || null);
        setNotice(
          snappedIntersection
            ? `Placed column ${newest?.column_id || ""} snapped to ${gridRef}.`
            : `Placed off-grid column ${newest?.column_id || ""} at (${targetPt.x_px}, ${targetPt.y_px}) px.`
        );
      } catch (err) {
        setError(err.message);
      } finally {
        setBusy(false);
      }
      return;
    }

    if (mode === "trace_opening") {
      const clickPt = snappedIntersection
        ? { x_px: snappedIntersection.x_px, y_px: snappedIntersection.y_px }
        : { x_px: pt.x_px, y_px: pt.y_px };

      // Close polygon if clicking near the first vertex (>= 3 vertices)
      if (openingVertices.length >= 3) {
        const first = openingVertices[0];
        const closeRadius = 16 / Math.max(pt.screenScale || 1, 0.05);
        if (Math.hypot(clickPt.x_px - first.x_px, clickPt.y_px - first.y_px) <= closeRadius) {
          await commitPolygonOpening(openingVertices);
          return;
        }
      }
      setOpeningVertices((prev) => [...prev, clickPt]);
    }
  }

  async function handleCanvasDoubleClick(e) {
    if (mode !== "trace_opening" || openingVertices.length < 3) return;
    e.preventDefault();
    e.stopPropagation();
    await commitPolygonOpening(openingVertices);
  }

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

  async function handleAppendPdf(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !doc) return;
    const prevCount = doc.page_count || doc.pages?.length || 0;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports?append=true&doc_id=${encodeURIComponent(doc.doc_id)}`,
        {
          method: "POST",
          body: fd,
        }
      );
      setDoc(updated);
      const added = Math.max(0, (updated.page_count || 0) - prevCount);
      if (added > 0) {
        setPageNum(prevCount + 1);
      }
      setMode("inspect");
      setNotice(
        `Appended ${added} page${added === 1 ? "" : "s"} from ${file.name} (total: ${updated.page_count} pages). Existing tracings preserved.`
      );
      await loadImportsList(updated.doc_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadCad(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const uploaded = await apiJson(`/api/projects/${pid}/pdf-import/upload-cad`, {
        method: "POST",
        body: fd,
      });
      setDoc(uploaded);
      setPageNum(1);
      setMode("inspect");
      setNotice(
        `Imported CAD ${uploaded.filename}: ${uploaded.grid_candidates?.length || 0} grid line(s), ` +
          `${uploaded.column_candidates?.length || 0} column(s), ${uploaded.openings?.length || 0} opening(s) ` +
          `extracted at exact CAD precision (scale pre-confirmed).`
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

  async function handleLockMasterGrid() {
    if (!doc || !activeRegionId) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/master-grid`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_region_id: activeRegionId }),
        }
      );
      setDoc(updated);
      const mg = updated.master_grid;
      setNotice(
        `Master Grid locked (${mg.grid_x_m.length} X-grids × ${mg.grid_y_m.length} Y-grids). Switch to another floor region and click 'Stamp Master Grid'.`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleCopyOpenings() {
    if (!doc || !activeRegionId || !copyOpeningsSourceId) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(activeRegionId)}/copy-openings`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_region_id: copyOpeningsSourceId }),
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

  function applyGridNextLabel(elementType, labelValue) {
    if (elementType === "grid_x") setNextGridXLabel(labelValue);
    else setNextGridYLabel(labelValue);
  }

  async function handleUndoGrid() {
    if (!doc || busy || undoStack.length === 0) return;
    const entry = undoStack[undoStack.length - 1];
    setBusy(true);
    setError("");
    try {
      if (entry.action === "add_grid") {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/${encodeURIComponent(entry.element_id)}`,
          { method: "DELETE" }
        );
        setDoc(updated);
        applyGridNextLabel(entry.payload.element_type, entry.prevNextLabel);
        setUndoStack((prev) => prev.slice(0, -1));
        setRedoStack((prev) => [...prev, entry]);
        setNotice(`Undid manual grid '${entry.payload.label}'.`);
      } else if (entry.action === "delete_grid") {
        const prevIds = new Set((doc.grid_candidates || []).map((g) => g.id));
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/manual-grid`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(entry.payload),
          }
        );
        const recreated =
          (updated.grid_candidates || []).find((g) => !prevIds.has(g.id)) ||
          updated.grid_candidates?.[updated.grid_candidates.length - 1];
        setDoc(updated);
        applyGridNextLabel(entry.payload.element_type, entry.prevNextLabel);
        setUndoStack((prev) => prev.slice(0, -1));
        setRedoStack((prev) => [
          ...prev,
          { ...entry, element_id: recreated?.id || entry.element_id },
        ]);
        setNotice(`Restored deleted grid '${entry.payload.label}'.`);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleRedoGrid() {
    if (!doc || busy || redoStack.length === 0) return;
    const entry = redoStack[redoStack.length - 1];
    setBusy(true);
    setError("");
    try {
      if (entry.action === "add_grid") {
        const prevIds = new Set((doc.grid_candidates || []).map((g) => g.id));
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/manual-grid`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(entry.payload),
          }
        );
        const recreated =
          (updated.grid_candidates || []).find((g) => !prevIds.has(g.id)) ||
          updated.grid_candidates?.[updated.grid_candidates.length - 1];
        setDoc(updated);
        applyGridNextLabel(entry.payload.element_type, entry.nextLabelAfter);
        setRedoStack((prev) => prev.slice(0, -1));
        setUndoStack((prev) => [
          ...prev,
          { ...entry, element_id: recreated?.id || entry.element_id },
        ]);
        setNotice(`Redid manual grid '${entry.payload.label}'.`);
      } else if (entry.action === "delete_grid") {
        const updated = await apiJson(
          `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/${encodeURIComponent(entry.element_id)}`,
          { method: "DELETE" }
        );
        setDoc(updated);
        applyGridNextLabel(entry.payload.element_type, entry.nextLabelAfter);
        setRedoStack((prev) => prev.slice(0, -1));
        setUndoStack((prev) => [...prev, entry]);
        setNotice(`Redid delete of grid '${entry.payload.label}'.`);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleClearRegionGrids() {
    if (!doc || !activeRegion || busy) return;
    const regionGridCount = (doc.grid_candidates || []).filter(
      (g) => g.region_id === activeRegion.region_id
    ).length;
    if (regionGridCount === 0) {
      setNotice(`Region '${activeRegion.name}' has no grid lines to clear.`);
      return;
    }
    const ok = window.confirm(
      `Clear all ${regionGridCount} grid line(s) in '${activeRegion.name}'? Columns and openings will be kept.`
    );
    if (!ok) return;

    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/regions/${encodeURIComponent(activeRegion.region_id)}/grids`,
        { method: "DELETE" }
      );
      setDoc(updated);
      setUndoStack((prev) => prev.filter((e) => e.payload?.region_id !== activeRegion.region_id));
      setRedoStack((prev) => prev.filter((e) => e.payload?.region_id !== activeRegion.region_id));
      setNotice(`Cleared ${regionGridCount} grid line(s) from '${activeRegion.name}'.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteElement(elementId) {
    if (!doc) return;
    const existingGrid = (doc.grid_candidates || []).find((g) => g.id === elementId) || null;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/elements/${encodeURIComponent(elementId)}`,
        { method: "DELETE" }
      );
      setDoc(updated);
      if (existingGrid) {
        const isX = existingGrid.axis === "x";
        const curNext = isX ? nextGridXLabel : nextGridYLabel;
        setUndoStack((prev) => [
          ...prev,
          {
            action: "delete_grid",
            element_id: existingGrid.id,
            payload: {
              page_number: existingGrid.page_number,
              region_id: existingGrid.region_id || null,
              element_type: isX ? "grid_x" : "grid_y",
              label: existingGrid.label,
              p1: { x_px: existingGrid.x1_px, y_px: existingGrid.y1_px },
              p2: { x_px: existingGrid.x2_px, y_px: existingGrid.y2_px },
            },
            prevNextLabel: curNext,
            nextLabelAfter: curNext,
          },
        ]);
        setRedoStack([]);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteOpening(openingId) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/openings/${encodeURIComponent(openingId)}`,
        { method: "DELETE" }
      );
      setDoc(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteLevel(levelId) {
    if (!doc) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/levels/${encodeURIComponent(levelId)}`,
        { method: "DELETE" }
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

  async function handleRunExtraction({ detectGrids = true, detectColumns = true } = {}) {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const minSpanVal = Math.min(100, Math.max(1, parseFloat(minSpanPct) || 28));
      const borderMarginVal = Math.min(40, Math.max(0, parseFloat(borderMarginPct) || 4));
      const clusterTolVal = Math.min(100, Math.max(1, parseFloat(clusterTolPx) || 8));
      const minColVal = Math.min(5, Math.max(0.05, parseFloat(minColSizeM) || 0.18));
      const maxColVal = Math.min(10, Math.max(minColVal + 0.05, parseFloat(maxColSizeM) || 1.1));
      const minFillVal = Math.min(100, Math.max(0, parseFloat(minColFillPct) || 35));
      const maxAspectVal = Math.min(10, Math.max(1, parseFloat(maxColAspect) || 2.5));

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
              require_grid_label: requireGridLabel,
              detect_grids: detectGrids,
              detect_columns: detectColumns,
              column_fill_mode: columnFillMode,
              min_column_size_m: Number(minColVal.toFixed(3)),
              max_column_size_m: Number(maxColVal.toFixed(3)),
              column_min_fill_ratio: Number((minFillVal / 100).toFixed(4)),
              column_max_aspect_ratio: Number(maxAspectVal.toFixed(2)),
              require_near_grid: requireNearGrid,
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

  async function handleAddManualLevel(e) {
    e.preventDefault();
    if (!doc || !activePage || !manualLvlName.trim()) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/levels`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            page_number: activePage.page_number,
            name: manualLvlName.trim(),
            elevation_m: parseFloat(manualLvlElev) || 0.0,
            y_px: manualLvlYPx || 0.0,
            engineer_approved: manualLvlApprove,
          }),
        }
      );
      setDoc(updated);
      setManualLvlName("");
      setManualLvlYPx(0);
      if (mode === "pick_datum") setMode("inspect");
      setNotice(`Added manual level '${manualLvlName.trim()}'.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleExtractLevels(regionId = null) {
    if (!doc || !activePage) return;
    setBusy(true);
    setError("");
    try {
      const fetchOpts = regionId
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ region_id: regionId }),
          }
        : { method: "POST" };
      const res = await apiJson(
        `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/extract-levels`,
        fetchOpts
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
      if (Array.isArray(res.project?.levels)) {
        setProjectLevels([...res.project.levels].sort((a, b) => a.elevation_m - b.elevation_m));
      } else {
        loadProjectLevels();
      }
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
            auto_add_roof_level: autoAddRoof,
            roof_framing_style: roofFramingStyle,
            default_roof_height_m: Math.min(10.0, Math.max(1.0, parseFloat(defaultRoofHeightM) || 3.0)),
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

  const hasConfirmedScale = Boolean(
    activeCalibration?.confirmed ||
      (doc?.master_grid && doc.calibrations?.[String(doc.master_grid.source_page_number)]?.confirmed)
  );
  const totalApprovedToTransfer = counts.approved + counts.approvedOpenings;
  const canTransfer = Boolean(hasConfirmedScale && totalApprovedToTransfer > 0 && !busy);
  const canLockMaster = Boolean(
    activeRegion && activeCalibration && (activeRegion.origin_px || activeCalibration.origin_px) && counts.approvedGrids > 0 && !busy
  );
  const imageUrl =
    doc && activePage
      ? `/api/projects/${pid}/pdf-imports/${encodeURIComponent(doc.doc_id)}/pages/${activePage.page_number}/image`
      : null;

  // Inverse zoom scale for crisp overlays at high magnification
  const invZ = 1 / Math.max(zoom, 0.5);

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
          {doc && (
            <label
              className="pdf-upload-btn"
              style={{ background: "#1e293b", borderColor: "#38bdf8", color: "#e0f2fe" }}
              title="Append additional PDF pages (e.g., 2nd floor, elevations, structural plans) into this document"
            >
              {busy ? "Working..." : "Append PDF Pages"}
              <input
                type="file"
                accept=".pdf,application/pdf"
                disabled={busy}
                onChange={handleAppendPdf}
              />
            </label>
          )}
          <label className="pdf-upload-btn cad-upload-btn" title="Exact millimeter CAD extraction">
            {busy ? "Working..." : "Upload CAD (.dxf / .dwg)"}
            <input
              type="file"
              accept=".dxf,.dwg"
              disabled={busy}
              onChange={handleUploadCad}
            />
          </label>
          <span className="pdf-badge class-vector" title="CAD uploads skip raster detection">
            CAD = exact mm extraction
          </span>
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
            Floor Plans, Elevations, Sections, Schedules), zoom &amp; pan with precision, lock a
            Master Grid, snap columns, trace polygon floor openings, and transfer approved geometry.
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
              {/* Page Header & Zoom Toolbar */}
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

                {/* Precision Zoom Controls */}
                <div className="pdf-zoom-bar">
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => zoomBy(1 / 1.25)}
                    title="Zoom out"
                  >
                    -
                  </button>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => {
                      setZoom(1);
                      setPan({ x: 0, y: 0 });
                    }}
                    title="Reset to 100%"
                  >
                    {Math.round(zoom * 100)}%
                  </button>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => zoomBy(1.25)}
                    title="Zoom in"
                  >
                    +
                  </button>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={resetView}
                    title="Fit to viewport"
                  >
                    Fit
                  </button>
                  {hoverPt && (
                    <span className="pdf-cursor-readout">
                      {hoverMeters
                        ? `(${hoverMeters.x_m}, ${hoverMeters.y_m}) m`
                        : `(${Math.round(hoverPt.x_px)}, ${Math.round(hoverPt.y_px)}) px`}
                    </span>
                  )}
                </div>
              </div>

              {/* Unified Tracing & Master Grid Mode Toolbar */}
              {!isElevationOrSection && (
                <div className="pdf-modes-bar">
                  <div className="pdf-modes">
                    <button
                      className={`secondary ${mode === "inspect" ? "active" : ""}`}
                      onClick={() => {
                        setMode("inspect");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Inspect (Pan)
                    </button>
                    <button
                      className={`secondary ${mode === "calibrate" ? "active" : ""}`}
                      onClick={() => {
                        setMode("calibrate");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Calibrate (2-Pt)
                    </button>
                    <button
                      className={`secondary ${mode === "draw_region" ? "active" : ""}`}
                      onClick={() => {
                        setMode("draw_region");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Draw Region
                    </button>
                    <button
                      className={`secondary ${mode === "set_origin" ? "active" : ""}`}
                      onClick={() => {
                        setMode("set_origin");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Set Origin (0,0)
                    </button>
                    <button
                      className={`secondary ${mode === "trace_grid_x" ? "active" : ""}`}
                      onClick={() => {
                        setMode("trace_grid_x");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Trace Grid X (V)
                    </button>
                    <button
                      className={`secondary ${mode === "trace_grid_y" ? "active" : ""}`}
                      onClick={() => {
                        setMode("trace_grid_y");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Trace Grid Y (H)
                    </button>
                    <button
                      className={`secondary ${mode === "stamp_master_grid" ? "active" : ""}`}
                      disabled={!doc.master_grid}
                      title={
                        doc.master_grid
                          ? "Click the reference grid intersection on this floor to stamp the locked Master Grid"
                          : "Lock a Master Grid from a calibrated floor region first"
                      }
                      onClick={() => {
                        setMode("stamp_master_grid");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                        setNotice(
                          "Stamp Master Grid mode: click the reference intersection (e.g. Grid A-1') on this floor region."
                        );
                      }}
                    >
                      Stamp Master Grid
                    </button>
                    <button
                      className={`secondary ${mode === "place_column" ? "active" : ""}`}
                      onClick={() => {
                        setMode("place_column");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                      }}
                    >
                      Place Column
                    </button>
                    <button
                      className={`secondary ${mode === "trace_opening" ? "active" : ""}`}
                      onClick={() => {
                        setMode("trace_opening");
                        setRegionCorner1(null);
                        setOpeningVertices([]);
                        setNotice(
                          "Trace Opening: click vertices around the opening. Click first vertex, double-click, or press Enter to close (Backspace=undo, Esc=cancel)."
                        );
                      }}
                    >
                      Trace Opening (Polygon)
                    </button>
                    {mode !== "trace_grid_x" && mode !== "trace_grid_y" && (
                      <div className="pdf-history-group">
                        <button
                          type="button"
                          className="mini-btn"
                          disabled={busy || undoStack.length === 0}
                          onClick={handleUndoGrid}
                          title="Undo last manual grid action (Ctrl+Z)"
                        >
                          ↶ Undo{undoStack.length > 0 ? ` (${undoStack.length})` : ""}
                        </button>
                        <button
                          type="button"
                          className="mini-btn"
                          disabled={busy || redoStack.length === 0}
                          onClick={handleRedoGrid}
                          title="Redo manual grid action (Ctrl+Y / Ctrl+Shift+Z)"
                        >
                          ↷ Redo{redoStack.length > 0 ? ` (${redoStack.length})` : ""}
                        </button>
                      </div>
                    )}
                  </div>

                  {/* Active Mode Contextual Controls */}
                  {(mode === "trace_grid_x" || mode === "trace_grid_y") && (
                    <div className="pdf-mode-subbar">
                      <label className="pdf-field-chip">
                        <span>Next Grid Label</span>
                        <input
                          type="text"
                          value={mode === "trace_grid_x" ? nextGridXLabel : nextGridYLabel}
                          onChange={(e) =>
                            mode === "trace_grid_x"
                              ? setNextGridXLabel(e.target.value)
                              : setNextGridYLabel(e.target.value)
                          }
                          placeholder={mode === "trace_grid_x" ? "A, B, C..." : "1', 1, 2..."}
                        />
                      </label>
                      <button
                        type="button"
                        className="mini-btn"
                        disabled={busy || undoStack.length === 0}
                        onClick={handleUndoGrid}
                        title="Undo last manual grid action (Ctrl+Z)"
                      >
                        ↶ Undo{undoStack.length > 0 ? ` (${undoStack.length})` : ""}
                      </button>
                      <button
                        type="button"
                        className="mini-btn"
                        disabled={busy || redoStack.length === 0}
                        onClick={handleRedoGrid}
                        title="Redo manual grid action (Ctrl+Y / Ctrl+Shift+Z)"
                      >
                        ↷ Redo{redoStack.length > 0 ? ` (${redoStack.length})` : ""}
                      </button>
                      <small className="pdf-hint">
                        Click on the grid line to place and auto-approve (auto-increments; supports
                        primes like <code>1&apos;</code>). Ctrl+Z Undo · Ctrl+Y Redo · Space+drag
                        Pan.
                      </small>
                    </div>
                  )}

                  {mode === "place_column" && (
                    <div className="pdf-mode-subbar">
                      <label className="pdf-field-chip">
                        <span>Width (mm)</span>
                        <input
                          type="number"
                          min="100"
                          step="50"
                          value={manualWidthMm}
                          onChange={(e) => setManualWidthMm(e.target.value)}
                        />
                      </label>
                      <label className="pdf-field-chip">
                        <span>Depth (mm)</span>
                        <input
                          type="number"
                          min="100"
                          step="50"
                          value={manualDepthMm}
                          onChange={(e) => setManualDepthMm(e.target.value)}
                        />
                      </label>
                      <label className="pdf-field-chip">
                        <span>Off-Grid Ref</span>
                        <input
                          type="text"
                          value={manualGridRef}
                          onChange={(e) => setManualGridRef(e.target.value)}
                          placeholder="Optional"
                          style={{ width: 90 }}
                        />
                      </label>
                      <small className="pdf-hint">
                        Hover near any grid intersection to snap automatically. Hold{" "}
                        <strong>Shift</strong> to place off-grid.
                      </small>
                    </div>
                  )}

                  {mode === "trace_opening" && (
                    <div className="pdf-mode-subbar">
                      <select
                        className="pdf-select"
                        value={openingCategory}
                        onChange={(e) => {
                          const cat = e.target.value;
                          setOpeningCategory(cat);
                          const prefix =
                            cat === "stairwell" ? "STAIR" : cat === "airwell" ? "AIRWELL" : "SHAFT";
                          setOpeningLabel(`${prefix}-${pageOpenings.length + 1}`);
                        }}
                      >
                        {OPENING_CATEGORIES.map((c) => (
                          <option key={c.value} value={c.value}>
                            {c.label}
                          </option>
                        ))}
                      </select>
                      <input
                        type="text"
                        className="pdf-select"
                        value={openingLabel}
                        onChange={(e) => setOpeningLabel(e.target.value)}
                        placeholder="Opening label"
                        style={{ width: 130 }}
                      />
                      <button
                        type="button"
                        className="mini-btn on-approve"
                        disabled={busy || openingVertices.length < 3}
                        onClick={() => commitPolygonOpening(openingVertices)}
                      >
                        Close &amp; Save ({openingVertices.length} pts)
                      </button>
                      <button
                        type="button"
                        className="mini-btn"
                        disabled={openingVertices.length === 0}
                        onClick={() => setOpeningVertices((prev) => prev.slice(0, -1))}
                      >
                        Undo
                      </button>
                      <button
                        type="button"
                        className="mini-btn on-reject"
                        disabled={openingVertices.length === 0}
                        onClick={() => setOpeningVertices([])}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
              )}

              {/* Plan Regions & Master Grid Panel */}
              {!isElevationOrSection && (
                <div className="pdf-regions-bar">
                  <div className="pdf-regions-top">
                    <span className="pdf-group-title">
                      Plan Regions ({pageRegions.length})
                      {doc.master_grid && (
                        <em className="pdf-cal-status confirmed">
                          Master Grid Locked ({doc.master_grid.grid_x_m.length}X ×{" "}
                          {doc.master_grid.grid_y_m.length}Y)
                        </em>
                      )}
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
                      {activeRegion && (
                        <>
                          <button
                            type="button"
                            className="secondary pdf-launch-btn"
                            disabled={!canLockMaster}
                            onClick={handleLockMasterGrid}
                            title="Lock this region's approved grids and (0,0) origin as the document Master Grid"
                          >
                            Lock as Master Grid
                          </button>
                          <button
                            type="button"
                            className="mini-btn on-reject"
                            disabled={
                              busy ||
                              !(doc.grid_candidates || []).some(
                                (g) => g.region_id === activeRegion.region_id
                              )
                            }
                            onClick={handleClearRegionGrids}
                            title="Clear all grid lines in this active region (keeps columns and openings)"
                          >
                            Clear Grids
                          </button>
                        </>
                      )}
                    </div>
                  </div>

                  {mode === "draw_region" && (
                    <div className="pdf-inline-form">
                      <select
                        className="pdf-select"
                        value={newRegionType}
                        onChange={(e) => {
                          const nextType = e.target.value;
                          setNewRegionType(nextType);
                          if (nextType === "elevation") {
                            setNewRegionLevel("");
                            if (!newRegionName.trim()) {
                              const elevCount = pageRegions.filter(
                                (r) => (r.region_type || "plan") === "elevation"
                              ).length;
                              setNewRegionName(`Elevation ${elevCount + 1}`);
                            }
                          }
                        }}
                        disabled={busy}
                        aria-label="New region type"
                      >
                        <option value="plan">Plan Region</option>
                        <option value="elevation">Elevation Region</option>
                      </select>
                      <input
                        type="text"
                        value={newRegionName}
                        onChange={(e) => setNewRegionName(e.target.value)}
                        placeholder={
                          newRegionType === "elevation"
                            ? "New elevation name (e.g. Elevation 1)"
                            : "New region name (e.g. Second Floor Plan)"
                        }
                        style={{ minWidth: 210 }}
                      />
                      <select
                        className="pdf-select"
                        value={newRegionType === "elevation" ? "" : newRegionLevel}
                        onChange={(e) => setNewRegionLevel(e.target.value)}
                        disabled={busy || newRegionType === "elevation"}
                        aria-label="New region floor level"
                      >
                        {newRegionType === "elevation" ? (
                          <option value="">Elevation (N/A)</option>
                        ) : availableLevelOptions.length === 0 ? (
                          <>
                            <option value="">Extract levels on Elevation/Section first</option>
                            <option value="ALL">All Stories (Typical)</option>
                          </>
                        ) : (
                          <>
                            <option value="">Select Level...</option>
                            <option value="ALL">All Stories (Typical)</option>
                            {availableLevelOptions.map((lv) => (
                              <option key={lv.id} value={lv.id}>
                                {lv.label}
                              </option>
                            ))}
                          </>
                        )}
                      </select>
                      <small className="pdf-hint">
                        {regionCorner1
                          ? "Corner 1 set — click opposite corner to save region."
                          : "Click two opposite corners on the plan image."}
                      </small>
                    </div>
                  )}

                  {pageRegions.length > 0 && (
                    <div className="pdf-region-cards">
                      {pageRegions.map((reg) => {
                        const isAct = reg.region_id === activeRegionId;
                        const isElevReg = (reg.region_type || "plan") === "elevation";
                        const currentLevel = reg.floor_level || "";
                        const hasCurrentInList =
                          !currentLevel ||
                          currentLevel.toUpperCase() === "ALL" ||
                          availableLevelOptions.some(
                            (lv) => lv.id.toUpperCase() === currentLevel.toUpperCase()
                          );
                        return (
                          <div
                            key={reg.region_id}
                            className={`pdf-region-card ${isAct ? "active" : ""}`}
                            style={isElevReg ? { borderLeftColor: "#f59e0b" } : undefined}
                            onClick={() => setActiveRegionId(reg.region_id)}
                          >
                            <div className="pdf-region-card-main">
                              <strong>
                                {isElevReg ? "[ELEV] " : ""}
                                {reg.name}
                              </strong>
                              <small>
                                {isElevReg
                                  ? "Elevation Region · "
                                  : reg.floor_level
                                  ? `Level: ${reg.floor_level === "ALL" ? "All Stories (Typical)" : reg.floor_level} · `
                                  : ""}
                                {Math.round(reg.width_px)}×{Math.round(reg.height_px)} px ·{" "}
                                {isElevReg
                                  ? "Scoped datum extraction"
                                  : reg.origin_px
                                  ? `Origin (${Math.round(reg.origin_px.x_px)}, ${Math.round(reg.origin_px.y_px)})`
                                  : "Origin not set"}
                              </small>
                            </div>
                            <div className="pdf-cand-actions">
                              <select
                                className="region-level-select"
                                value={reg.region_type || "plan"}
                                disabled={busy}
                                title="Switch region between Plan and Elevation"
                                aria-label={`Region type for ${reg.name}`}
                                onClick={(ev) => ev.stopPropagation()}
                                onChange={(ev) => {
                                  ev.stopPropagation();
                                  handleUpdateRegionMeta(reg.region_id, {
                                    region_type: ev.target.value,
                                  });
                                }}
                              >
                                <option value="plan">Plan</option>
                                <option value="elevation">Elevation</option>
                              </select>
                              {isElevReg ? (
                                <button
                                  type="button"
                                  className="mini-btn on-approve"
                                  disabled={busy}
                                  onClick={(ev) => {
                                    ev.stopPropagation();
                                    handleExtractLevels(reg.region_id);
                                  }}
                                  title="Extract floor-to-floor levels strictly inside this elevation region"
                                >
                                  Extract Region Levels
                                </button>
                              ) : (
                                <select
                                  className="region-level-select"
                                  value={currentLevel}
                                  disabled={busy}
                                  title="Assign floor level from ProjectState.levels or All Stories (Typical)"
                                  aria-label={`Floor level for ${reg.name}`}
                                  onClick={(ev) => ev.stopPropagation()}
                                  onChange={(ev) => {
                                    ev.stopPropagation();
                                    handleUpdateRegionMeta(reg.region_id, {
                                      floor_level: ev.target.value,
                                    });
                                  }}
                                >
                                  {availableLevelOptions.length === 0 ? (
                                    <option value="">Extract levels on Elevation/Section first</option>
                                  ) : (
                                    <option value="">-- Level --</option>
                                  )}
                                  <option value="ALL">All Stories (Typical)</option>
                                  {!hasCurrentInList && (
                                    <option value={currentLevel}>{currentLevel} (unmatched)</option>
                                  )}
                                  {availableLevelOptions.map((lv) => (
                                    <option key={lv.id} value={lv.id}>
                                      {lv.label}
                                    </option>
                                  ))}
                                </select>
                              )}
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
                                ×
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {activeRegion && openingSourceRegions.length > 0 && (
                    <div className="pdf-master-grid-row">
                      <select
                        className="pdf-select"
                        value={copyOpeningsSourceId}
                        onChange={(e) => setCopyOpeningsSourceId(e.target.value)}
                        disabled={busy}
                        aria-label="Copy openings source region"
                      >
                        {openingSourceRegions.map((src) => (
                          <option key={src.region_id} value={src.region_id}>
                            Openings from: P{src.page_number} · {src.name}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy || !activeRegion.origin_px || !copyOpeningsSourceId}
                        onClick={handleCopyOpenings}
                        title={
                          activeRegion.origin_px
                            ? "Copy stairwells/airwells/shafts aligned by (0,0) origin"
                            : "Set (0,0) origin or stamp Master Grid on this region first"
                        }
                      >
                        Copy Openings From...
                      </button>
                    </div>
                  )}
                </div>
              )}

              {/* Elevation / Section Level Extraction Strip */}
              {isElevationOrSection ? (
                <div className="pdf-controls-strip elev-strip">
                  <div className="pdf-control-group">
                    <span className="pdf-group-title">
                      Structural Floor-to-Floor Level Extraction
                    </span>
                    <div className="pdf-inline-form">
                      <button disabled={busy} onClick={() => handleExtractLevels(null)}>
                        Extract Floor Levels
                      </button>
                      <small className="pdf-hint">
                        Extracts structural floor &amp; roof framing datums only (ignores ceilings,
                        firewalls, parapets, NGL, and curbs &lt; 1.50 m).
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
                          ? `Span: ${draftPixelDist.toFixed(1)} px`
                          : "Use Calibrate (2-Pt) and click two known reference points."}
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
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() => handleRunExtraction({ detectGrids: true, detectColumns: true })}
                        >
                          2. Detect Grids &amp; Columns
                        </button>
                        <button
                          className="secondary pdf-launch-btn"
                          disabled={busy}
                          onClick={() => handleRunExtraction({ detectGrids: false, detectColumns: true })}
                          title="Scan only shaded/hatched columns while preserving stamped or traced grids"
                        >
                          Scan Columns Only
                        </button>
                      </div>
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
                        {requireGridLabel ? "Labeled Grids" : "All Lines"} ·{" "}
                        {COLUMN_FILL_MODES.find((m) => m.value === columnFillMode)?.label || columnFillMode}{" "}
                        · Col {minColSizeM}–{maxColSizeM}m · Fill ≥{minColFillPct}%
                      </small>
                    </button>
                    {showDetectSettings && (
                      <div className="pdf-settings-fields">
                        <label className="pdf-check-chip">
                          <input
                            type="checkbox"
                            checked={requireGridLabel}
                            onChange={(e) => setRequireGridLabel(e.target.checked)}
                          />
                          <span>Labeled Grids Only</span>
                        </label>
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

                        <span className="pdf-settings-divider" aria-hidden="true" />

                        <label className="pdf-field-chip">
                          <span>Column Fill Mode</span>
                          <select
                            className="pdf-select pdf-compact-select"
                            value={columnFillMode}
                            onChange={(e) => setColumnFillMode(e.target.value)}
                          >
                            {COLUMN_FILL_MODES.map((m) => (
                              <option key={m.value} value={m.value}>
                                {m.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="pdf-field-chip">
                          <span>Col Size (m)</span>
                          <input
                            type="number"
                            min="0.05"
                            max="5"
                            step="0.02"
                            value={minColSizeM}
                            onChange={(e) => setMinColSizeM(e.target.value)}
                            aria-label="Min column size (m)"
                          />
                          <span>–</span>
                          <input
                            type="number"
                            min="0.1"
                            max="10"
                            step="0.05"
                            value={maxColSizeM}
                            onChange={(e) => setMaxColSizeM(e.target.value)}
                            aria-label="Max column size (m)"
                          />
                        </label>
                        <label className="pdf-field-chip">
                          <span>Min Fill (%)</span>
                          <input
                            type="number"
                            min="5"
                            max="100"
                            step="5"
                            value={minColFillPct}
                            onChange={(e) => setMinColFillPct(e.target.value)}
                          />
                        </label>
                        <label className="pdf-field-chip">
                          <span>Max Aspect Ratio</span>
                          <input
                            type="number"
                            min="1"
                            max="10"
                            step="0.1"
                            value={maxColAspect}
                            onChange={(e) => setMaxColAspect(e.target.value)}
                          />
                        </label>
                        <label className="pdf-check-chip">
                          <input
                            type="checkbox"
                            checked={requireNearGrid}
                            onChange={(e) => setRequireNearGrid(e.target.checked)}
                          />
                          <span>Near Grids Only</span>
                        </label>
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Precision Zoom & Pan Viewport */}
              <div
                ref={viewportRef}
                className={`pdf-viewport mode-${mode} ${spacePressed || isPanning ? "panning" : ""}`}
                onMouseDown={handleViewportMouseDown}
                onMouseMove={handleViewportMouseMove}
                onMouseUp={handleViewportMouseUp}
                onMouseLeave={handleViewportMouseUp}
              >
                <div
                  className="pdf-pan-layer"
                  style={{
                    transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                    transformOrigin: "0 0",
                  }}
                >
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
                    onDoubleClick={handleCanvasDoubleClick}
                  >
                    {/* Saved PlanRegion Boxes & (0,0) Origin Crosshairs */}
                    {pageRegions.map((reg) => {
                      const isActiveReg = reg.region_id === activeRegionId;
                      const isElevReg = (reg.region_type || "plan") === "elevation";
                      const strokeW = Math.max(activePage.width_px * 0.0022, 1.8) * invZ;
                      const fontSize = Math.max(activePage.width_px * 0.012, 12) * invZ;
                      const cross = Math.max(activePage.width_px * 0.01, 12) * invZ;
                      const boxStroke = isElevReg
                        ? isActiveReg
                          ? "#f59e0b"
                          : "#d97706"
                        : isActiveReg
                        ? "#38bdf8"
                        : "#94a3b8";
                      const boxFill = isElevReg ? "#f59e0b" : isActiveReg ? "#38bdf8" : "none";
                      const boxFillOp = isElevReg ? (isActiveReg ? 0.12 : 0.06) : isActiveReg ? 0.04 : 0;
                      return (
                        <g key={reg.region_id} opacity={isActiveReg ? 0.96 : 0.62}>
                          <rect
                            x={reg.x_px}
                            y={reg.y_px}
                            width={Math.max(10, reg.width_px)}
                            height={Math.max(10, reg.height_px)}
                            fill={boxFill}
                            fillOpacity={boxFillOp}
                            stroke={boxStroke}
                            strokeWidth={strokeW}
                            strokeDasharray={`${strokeW * 4} ${strokeW * 3}`}
                          />
                          <text
                            x={reg.x_px + 10 * invZ}
                            y={reg.y_px + fontSize + 6 * invZ}
                            fill={isElevReg ? "#fbbf24" : isActiveReg ? "#38bdf8" : "#cbd5e1"}
                            fontSize={fontSize}
                            fontWeight="800"
                            stroke="#0b1220"
                            strokeWidth={strokeW * 1.4}
                            paintOrder="stroke"
                          >
                            {isElevReg ? "[ELEV] " : ""}
                            {reg.name}
                            {!isElevReg && reg.floor_level ? ` (${reg.floor_level})` : ""}
                          </text>
                          {reg.origin_px && (
                            <g>
                              <circle
                                cx={reg.origin_px.x_px}
                                cy={reg.origin_px.y_px}
                                r={cross * 0.65}
                                fill="none"
                                stroke="#45c9a8"
                                strokeWidth={strokeW}
                              />
                              <line
                                x1={reg.origin_px.x_px - cross}
                                y1={reg.origin_px.y_px}
                                x2={reg.origin_px.x_px + cross}
                                y2={reg.origin_px.y_px}
                                stroke="#45c9a8"
                                strokeWidth={strokeW}
                              />
                              <line
                                x1={reg.origin_px.x_px}
                                y1={reg.origin_px.y_px - cross}
                                x2={reg.origin_px.x_px}
                                y2={reg.origin_px.y_px + cross}
                                stroke="#45c9a8"
                                strokeWidth={strokeW}
                              />
                              <text
                                x={reg.origin_px.x_px + cross}
                                y={reg.origin_px.y_px - 6 * invZ}
                                fill="#45c9a8"
                                fontSize={fontSize * 0.9}
                                fontWeight="800"
                                stroke="#0b1220"
                                strokeWidth={strokeW * 1.4}
                                paintOrder="stroke"
                              >
                                (0,0)
                              </text>
                            </g>
                          )}
                        </g>
                      );
                    })}

                    {/* Saved Polygon Floor Openings */}
                    {pageOpenings.map((op) => {
                      const col = openingColor(op.category);
                      const strokeW = Math.max(activePage.width_px * 0.0022, 1.8) * invZ;
                      const fontSize = Math.max(activePage.width_px * 0.011, 11) * invZ;
                      const ptsStr = op.polygon_px.map((p) => `${p.x_px},${p.y_px}`).join(" ");
                      const cx =
                        op.polygon_px.reduce((s, p) => s + p.x_px, 0) /
                        Math.max(op.polygon_px.length, 1);
                      const cy =
                        op.polygon_px.reduce((s, p) => s + p.y_px, 0) /
                        Math.max(op.polygon_px.length, 1);
                      return (
                        <g key={op.opening_id}>
                          <polygon
                            points={ptsStr}
                            fill={col}
                            fillOpacity={0.24}
                            stroke={col}
                            strokeWidth={strokeW}
                          />
                          <text
                            x={cx}
                            y={cy}
                            textAnchor="middle"
                            fill={col}
                            fontSize={fontSize}
                            fontWeight="800"
                            stroke="#0b1220"
                            strokeWidth={strokeW * 1.5}
                            paintOrder="stroke"
                          >
                            {op.label}
                          </text>
                        </g>
                      );
                    })}

                    {/* Live Polygon Opening Rubber-Band Preview */}
                    {mode === "trace_opening" && openingVertices.length > 0 && (
                      <g>
                        <polyline
                          points={[
                            ...openingVertices,
                            ...(hoverPt
                              ? [snappedIntersection || { x_px: hoverPt.x_px, y_px: hoverPt.y_px }]
                              : []),
                          ]
                            .map((p) => `${p.x_px},${p.y_px}`)
                            .join(" ")}
                          fill={openingColor(openingCategory)}
                          fillOpacity={0.14}
                          stroke={openingColor(openingCategory)}
                          strokeWidth={Math.max(activePage.width_px * 0.0024, 2) * invZ}
                          strokeDasharray={`${6 * invZ} ${4 * invZ}`}
                        />
                        {openingVertices.map((v, idx) => (
                          <circle
                            key={idx}
                            cx={v.x_px}
                            cy={v.y_px}
                            r={(idx === 0 ? 7 : 5) * invZ * (activePage.width_px / 1100)}
                            fill={idx === 0 ? "#45c9a8" : openingColor(openingCategory)}
                            stroke="#0b1220"
                            strokeWidth={1.5 * invZ}
                          />
                        ))}
                      </g>
                    )}

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
                        strokeWidth={Math.max(activePage.width_px * 0.0024, 2) * invZ}
                        strokeDasharray={`${8 * invZ} ${5 * invZ}`}
                      />
                    )}

                    {/* Live Full-Region Crosshair Preview for Manual Grid Tracing */}
                    {(mode === "trace_grid_x" || mode === "trace_grid_y") && hoverPt && (
                      <g pointerEvents="none">
                        {mode === "trace_grid_x" ? (
                          <line
                            x1={hoverPt.x_px}
                            y1={activeRegion ? activeRegion.y_px : 0}
                            x2={hoverPt.x_px}
                            y2={
                              activeRegion
                                ? activeRegion.y_px + activeRegion.height_px
                                : activePage.height_px
                            }
                            stroke="#45c9a8"
                            strokeWidth={Math.max(activePage.width_px * 0.0022, 1.8) * invZ}
                            strokeDasharray={`${6 * invZ} ${4 * invZ}`}
                          />
                        ) : (
                          <line
                            x1={activeRegion ? activeRegion.x_px : 0}
                            y1={hoverPt.y_px}
                            x2={
                              activeRegion
                                ? activeRegion.x_px + activeRegion.width_px
                                : activePage.width_px
                            }
                            y2={hoverPt.y_px}
                            stroke="#45c9a8"
                            strokeWidth={Math.max(activePage.width_px * 0.0022, 1.8) * invZ}
                            strokeDasharray={`${6 * invZ} ${4 * invZ}`}
                          />
                        )}
                      </g>
                    )}

                    {/* Elevation / Section Horizontal Structural Floor Levels */}
                    {pageLevels.map((lvl) => {
                      const color = statusColor(lvl.review_status);
                      const strokeW = Math.max(activePage.width_px * 0.0022, 1.8) * invZ;
                      const fontSize = Math.max(activePage.width_px * 0.012, 12) * invZ;
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
                            x={14 * invZ}
                            y={Math.max(fontSize + 4 * invZ, lvl.y_px - 6 * invZ)}
                            fill={color}
                            fontSize={fontSize}
                            fontWeight="800"
                            stroke="#0b1220"
                            strokeWidth={strokeW * 1.6}
                            paintOrder="stroke"
                          >
                            {lvl.name}
                          </text>
                          <text
                            x={activePage.width_px - 14 * invZ}
                            y={Math.max(fontSize + 4 * invZ, lvl.y_px - 6 * invZ)}
                            textAnchor="end"
                            fill={color}
                            fontSize={fontSize}
                            fontWeight="800"
                            stroke="#0b1220"
                            strokeWidth={strokeW * 1.6}
                            paintOrder="stroke"
                          >
                            {lvl.elevation_m >= 0 ? "+" : ""}
                            {lvl.elevation_m.toFixed(2)} m
                            {lvl.story_height_m !== null
                              ? ` (ΔH ${lvl.story_height_m.toFixed(2)}m)`
                              : " (BASE)"}
                          </text>
                        </g>
                      );
                    })}

                    {/* Grid Candidates */}
                    {pageGrids.map((g) => {
                      const color = statusColor(g.status);
                      const isSel = selectedId === g.id;
                      const strokeW = Math.max(activePage.width_px * 0.002, 1.5) * invZ;
                      const bubbleR = Math.max(activePage.width_px * 0.013, 11) * invZ;
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
                          opacity={g.status === "rejected" ? 0.3 : 0.92}
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

                    {/* Column Candidates: outlined rect matching detected width_px × height_px + center crosshair + grid label */}
                    {pageCols.map((c) => {
                      const color = statusColor(c.status);
                      const isSel = selectedId === c.id;
                      const rw = Math.max(c.width_px || 12, 4);
                      const rh = Math.max(c.height_px || 12, 4);
                      const strokeW = Math.max(activePage.width_px * 0.002, 1.6) * invZ;
                      const crossLen = Math.min(Math.max(Math.min(rw, rh) * 0.35, 4 * invZ), 12 * invZ);
                      const fontSize = Math.max(activePage.width_px * 0.011, 11) * invZ;
                      return (
                        <g
                          key={c.id}
                          opacity={c.status === "rejected" ? 0.28 : 0.96}
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
                            fillOpacity={isSel ? 0.26 : 0.12}
                            stroke={color}
                            strokeWidth={isSel ? strokeW * 1.8 : strokeW}
                          />
                          <line
                            x1={c.x_px - crossLen}
                            y1={c.y_px}
                            x2={c.x_px + crossLen}
                            y2={c.y_px}
                            stroke={color}
                            strokeWidth={strokeW * 0.85}
                          />
                          <line
                            x1={c.x_px}
                            y1={c.y_px - crossLen}
                            x2={c.x_px}
                            y2={c.y_px + crossLen}
                            stroke={color}
                            strokeWidth={strokeW * 0.85}
                          />
                          <text
                            x={c.x_px + rw / 2 + 4 * invZ}
                            y={c.y_px - rh / 2 + 4 * invZ}
                            fill={color}
                            fontSize={fontSize}
                            fontWeight="800"
                            stroke="#0b1220"
                            strokeWidth={strokeW * 1.4}
                            paintOrder="stroke"
                          >
                            {c.column_id}
                            {c.grid ? ` (${c.grid})` : ""}
                          </text>
                        </g>
                      );
                    })}

                    {/* Grid Intersection Snap Ring Highlight */}
                    {snappedIntersection && (
                      <g pointerEvents="none">
                        <circle
                          cx={snappedIntersection.x_px}
                          cy={snappedIntersection.y_px}
                          r={Math.max(activePage.width_px * 0.012, 12) * invZ}
                          fill="#45c9a8"
                          fillOpacity={0.22}
                          stroke="#45c9a8"
                          strokeWidth={Math.max(activePage.width_px * 0.0025, 2) * invZ}
                        />
                        <text
                          x={snappedIntersection.x_px + 14 * invZ * (activePage.width_px / 1100)}
                          y={snappedIntersection.y_px - 10 * invZ * (activePage.width_px / 1100)}
                          fill="#7be0c3"
                          fontSize={Math.max(activePage.width_px * 0.012, 12) * invZ}
                          fontWeight="900"
                          stroke="#0b1220"
                          strokeWidth={Math.max(activePage.width_px * 0.003, 2.5) * invZ}
                          paintOrder="stroke"
                        >
                          Snap: {snappedIntersection.gridRef}
                        </text>
                      </g>
                    )}

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
                            strokeWidth={Math.max(activePage.width_px * 0.0028, 2.2) * invZ}
                          />
                        )}
                        <circle
                          cx={calP1.x_px}
                          cy={calP1.y_px}
                          r={Math.max(activePage.width_px * 0.006, 6) * invZ}
                          fill="#38bdf8"
                          stroke="#0b1220"
                          strokeWidth={1.8 * invZ}
                        />
                        {calP2 && (
                          <circle
                            cx={calP2.x_px}
                            cy={calP2.y_px}
                            r={Math.max(activePage.width_px * 0.006, 6) * invZ}
                            fill="#38bdf8"
                            stroke="#0b1220"
                            strokeWidth={1.8 * invZ}
                          />
                        )}
                      </g>
                    )}
                  </svg>
                </div>
              </div>
            </div>

            {/* Right Review & Transfer Panel */}
            <aside className="pdf-review-col">
              {showLevelsPanel && (
                <>
                  <div className="pdf-review-header">
                    <div>
                      <label>FLOOR-TO-FLOOR LEVELS · PAGE {activePage.page_number}</label>
                      <h3>
                        {approvedLevelsCount} Approved · {pageLevels.length} Structural Levels
                      </h3>
                    </div>
                  </div>

                  {/* Manual Level Entry & Click-on-Sheet Datum Picker (works on scanned / SHX elevations) */}
                  <form
                    onSubmit={handleAddManualLevel}
                    className="pdf-level-card"
                    style={{ marginBottom: "8px", borderLeftColor: "#38bdf8" }}
                  >
                    <div className="pdf-level-top" style={{ gap: "6px" }}>
                      <input
                        type="text"
                        placeholder="Level Label (e.g. 2ND FLOOR LINE)"
                        aria-label="Manual level label"
                        value={manualLvlName}
                        onChange={(e) => setManualLvlName(e.target.value)}
                        required
                      />
                    </div>
                    <div className="pdf-level-num-row" style={{ flexWrap: "wrap", gap: "6px" }}>
                      <label className="pdf-field-chip">
                        <span>Elev (m)</span>
                        <input
                          type="number"
                          step="0.05"
                          aria-label="Manual level elevation"
                          value={manualLvlElev}
                          onChange={(e) => setManualLvlElev(e.target.value)}
                        />
                      </label>
                      <label className="pdf-check-chip">
                        <input
                          type="checkbox"
                          checked={manualLvlApprove}
                          onChange={(e) => setManualLvlApprove(e.target.checked)}
                        />
                        Approve
                      </label>
                    </div>
                    <div className="pdf-cand-actions" style={{ flexWrap: "wrap", gap: "6px" }}>
                      <button
                        type="submit"
                        className="mini-btn on-approve"
                        disabled={busy || !manualLvlName.trim()}
                      >
                        + Add Level Manually
                      </button>
                      <button
                        type="button"
                        className={`mini-btn ${mode === "pick_datum" ? "on-approve" : ""}`}
                        disabled={busy}
                        onClick={() => setMode((m) => (m === "pick_datum" ? "inspect" : "pick_datum"))}
                      >
                        {mode === "pick_datum" ? "Picking Datum (Click Sheet)..." : "Click-on-Sheet to Pick Datum"}
                      </button>
                    </div>
                  </form>

                  <div className="pdf-review-list">
                    {pageLevels.length === 0 ? (
                      <p className="pdf-muted">
                        Click <strong>Extract Floor Levels</strong> to parse structural floor and
                        roof framing levels on this sheet.
                      </p>
                    ) : (
                      pageLevels.map((lvl, idx) => {
                        const draft = levelEdits[lvl.level_id] || {
                          name: lvl.name,
                          elevation_m: String(lvl.elevation_m),
                          story_height_m:
                            lvl.story_height_m !== null ? String(lvl.story_height_m) : "",
                        };
                        const dirty =
                          draft.name !== lvl.name ||
                          String(draft.elevation_m) !== String(lvl.elevation_m) ||
                          (lvl.story_height_m !== null &&
                            String(draft.story_height_m) !== String(lvl.story_height_m));
                        return (
                          <div
                            key={lvl.level_id}
                            className={`pdf-level-card status-${lvl.review_status}`}
                          >
                            <div className="pdf-level-top">
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
                              <button
                                type="button"
                                className="mini-btn on-reject"
                                disabled={busy}
                                onClick={() => handleDeleteLevel(lvl.level_id)}
                                title="Remove level"
                              >
                                ×
                              </button>
                            </div>
                            <div className="pdf-level-num-row">
                              <label className="pdf-field-chip">
                                <span>Elev (m)</span>
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
                                />
                              </label>
                              {idx > 0 ? (
                                <label className="pdf-field-chip">
                                  <span>ΔH (m)</span>
                                  <input
                                    type="number"
                                    step="0.05"
                                    value={draft.story_height_m}
                                    onChange={(e) =>
                                      setLevelEdits((prev) => ({
                                        ...prev,
                                        [lvl.level_id]: { ...draft, story_height_m: e.target.value },
                                      }))
                                    }
                                  />
                                </label>
                              ) : (
                                <small className="pdf-hint">Base Level</small>
                              )}
                            </div>
                            {lvl.warnings && lvl.warnings.length > 0 && (
                              <div className="pdf-level-warn">{lvl.warnings.join(" ")}</div>
                            )}
                            <div className="pdf-cand-actions">
                              {dirty && (
                                <button
                                  className="mini-btn"
                                  disabled={busy}
                                  onClick={() => {
                                    const patch = { name: draft.name };
                                    if (String(draft.elevation_m) !== String(lvl.elevation_m)) {
                                      patch.elevation_m = parseFloat(draft.elevation_m);
                                    } else if (
                                      idx > 0 &&
                                      String(draft.story_height_m) !== String(lvl.story_height_m)
                                    ) {
                                      patch.story_height_m = parseFloat(draft.story_height_m);
                                    }
                                    handleUpdateLevel(lvl.level_id, patch);
                                  }}
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
                      Writes engineer-approved structural levels into <code>ProjectState.levels</code>{" "}
                      sorted ascending by <code>elevation_m</code>.
                    </small>
                  </div>
                </>
              )}
              {!isElevationOrSection && (
                <>
                  <div className="pdf-review-header">
                    <div>
                      <label>STAGED REVIEW · PAGE {activePage.page_number}</label>
                      <h3>
                        {counts.approved} Approved · {counts.pending} Pending ·{" "}
                        {counts.approvedOpenings} Openings
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
                    <div className="pdf-review-section-head">
                      <h4>Grid Lines ({pageGrids.length})</h4>
                      {activeRegion && (
                        <button
                          type="button"
                          className="mini-btn on-reject"
                          disabled={
                            busy ||
                            !(doc.grid_candidates || []).some(
                              (g) => g.region_id === activeRegion.region_id
                            )
                          }
                          onClick={handleClearRegionGrids}
                          title={`Clear all grid lines in '${activeRegion.name}'`}
                        >
                          Clear Grids
                        </button>
                      )}
                    </div>
                    {pageGrids.length === 0 ? (
                      <p className="pdf-muted">
                        No grids on this page yet. Detect, trace manually, or stamp Master Grid.
                      </p>
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
                              · {g.source}
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
                              ✓
                            </button>
                            <button
                              className={`mini-btn ${g.status === "rejected" ? "on-reject" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(g.id, { status: "rejected" });
                              }}
                            >
                              −
                            </button>
                            <button
                              className="mini-btn on-reject"
                              disabled={busy}
                              title="Delete grid"
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleDeleteElement(g.id);
                              }}
                            >
                              ×
                            </button>
                          </div>
                        </div>
                      ))
                    )}

                    <h4>Columns ({pageCols.length})</h4>
                    {pageCols.length === 0 ? (
                      <p className="pdf-muted">
                        No columns yet. Switch to <strong>Place Column</strong> and snap to grid
                        intersections.
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
                              · {Math.round(c.width_mm)}×{Math.round(c.depth_mm)} mm
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
                              ✓
                            </button>
                            <button
                              className={`mini-btn ${c.status === "rejected" ? "on-reject" : ""}`}
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleReviewCandidate(c.id, { status: "rejected" });
                              }}
                            >
                              −
                            </button>
                            <button
                              className="mini-btn on-reject"
                              disabled={busy}
                              title="Delete column"
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleDeleteElement(c.id);
                              }}
                            >
                              ×
                            </button>
                          </div>
                        </div>
                      ))
                    )}

                    <h4>Floor Openings ({pageOpenings.length})</h4>
                    {pageOpenings.length === 0 ? (
                      <p className="pdf-muted">
                        No openings yet. Use <strong>Trace Opening (Polygon)</strong> for
                        stairwells, airwells, or shafts.
                      </p>
                    ) : (
                      pageOpenings.map((op) => (
                        <div
                          key={op.opening_id}
                          className="pdf-cand-row"
                          style={{ borderLeftColor: openingColor(op.category) }}
                        >
                          <div className="pdf-cand-info">
                            <strong>{op.label}</strong>
                            <small>
                              {op.category} · {op.polygon_px.length} pts
                              {op.floor_level ? ` · ${op.floor_level}` : ""}
                              {op.transferred ? " · Transferred" : ""}
                            </small>
                          </div>
                          <div className="pdf-cand-actions">
                            <button
                              className="mini-btn on-reject"
                              disabled={busy}
                              title="Delete opening"
                              onClick={() => handleDeleteOpening(op.opening_id)}
                            >
                              ×
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
                        <option value="replace_geometry">Replace project grids, columns &amp; openings</option>
                      </select>
                    </div>
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 6,
                        padding: "8px 10px",
                        borderRadius: 6,
                        background: "rgba(15, 23, 42, 0.7)",
                        border: "1px solid #1e293b",
                        marginBottom: 8,
                      }}
                    >
                      <label className="pdf-check-chip" style={{ margin: 0 }}>
                        <input
                          type="checkbox"
                          checked={autoAddRoof}
                          onChange={(e) => setAutoAddRoof(e.target.checked)}
                          disabled={busy}
                        />
                        <span>Auto-add Roof Beam level (+1 story) if no Roof Plan is included</span>
                      </label>
                      {autoAddRoof && (
                        <div className="pdf-inline-form" style={{ gap: 6 }}>
                          <select
                            className="pdf-select"
                            value={roofFramingStyle}
                            onChange={(e) => setRoofFramingStyle(e.target.value)}
                            disabled={busy}
                            aria-label="Roof framing style"
                            style={{ flex: 1 }}
                          >
                            <option value="full_grid">Full Grid (Roof Deck)</option>
                            <option value="perimeter_only">Perimeter Tie-Beams Only (Pitched Roof)</option>
                          </select>
                          <label className="pdf-field-chip" title="Default story height above top floor">
                            <span>+H (m)</span>
                            <input
                              type="number"
                              step="0.1"
                              min="1.5"
                              max="8.0"
                              value={defaultRoofHeightM}
                              onChange={(e) => setDefaultRoofHeightM(e.target.value)}
                              disabled={busy}
                              style={{ width: 64 }}
                            />
                          </label>
                        </div>
                      )}
                    </div>
                    <button
                      className="pdf-transfer-btn"
                      disabled={!canTransfer}
                      onClick={handleTransfer}
                    >
                      Transfer Approved to Project ({counts.approvedGrids}G · {counts.approvedCols}C
                      {counts.approvedOpenings > 0 ? ` · ${counts.approvedOpenings}O` : ""})
                    </button>
                    <small className="pdf-hint">
                      {!hasConfirmedScale
                        ? "Confirm scale calibration to unlock transfer."
                        : totalApprovedToTransfer === 0
                          ? "Approve at least one grid, column, or opening to enable transfer."
                          : "Transfers approved grids, columns, and polygon openings to ProjectState."}
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
