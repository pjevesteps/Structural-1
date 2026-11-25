import React, { useEffect, useMemo, useRef, useState } from "react";

const DEFAULT_OPTIONS = {
  sheet_size: "A1",
  plan_scale: 100,
  detail_scale: 25,
  sheet_prefix: "S",
  include_general_notes: true,
  include_foundation_plan: true,
  include_framing_plans: true,
  include_schedules: true,
  include_typical_details: true,
  preset: "a1_permit_set",
  dwg_version: "ACAD2013",
  sheets: [],
};

const LAYER_META = {
  "S-TITLEBLK": { color: "#e2e8f0", label: "Title Block & Border" },
  "S-GRID": { color: "#f87171", label: "Gridlines (Center)" },
  "S-GRID-BUBB": { color: "#22d3ee", label: "Grid Bubbles" },
  "S-DIM": { color: "#4ade80", label: "Dimensions" },
  "S-FTG": { color: "#e879f9", label: "Footings & TOF" },
  "S-COL": { color: "#22d3ee", label: "Columns" },
  "S-COL-HATCH": { color: "#64748b", label: "Column Hatch" },
  "S-BEAM": { color: "#facc15", label: "Beams & Tie Beams" },
  "S-SLAB": { color: "#60a5fa", label: "Slab Tags" },
  "S-OPENING": { color: "#f87171", label: "Floor Openings" },
  "S-REBAR": { color: "#4ade80", label: "Rebar Details" },
  "S-SCHED": { color: "#e2e8f0", label: "Schedule Tables" },
  "S-TEXT": { color: "#cbd5e1", label: "Annotations & Notes" },
};

const STATIC_SCHED_DETAIL_ITEMS = [
  { key: "schedule_footings", label: "Footing Schedule", group: "Schedules & Details" },
  { key: "schedule_columns", label: "Column Schedule", group: "Schedules & Details" },
  { key: "schedule_beams", label: "Beam Schedule", group: "Schedules & Details" },
  { key: "schedule_slabs_stairs", label: "Slabs & Stairs Schedule", group: "Schedules & Details" },
  { key: "detail_footing_section", label: "Footing Detail", group: "Schedules & Details" },
  { key: "detail_column_section", label: "Column Section", group: "Schedules & Details" },
  { key: "detail_beam_section", label: "Beam Section", group: "Schedules & Details" },
  { key: "detail_beam_longitudinal", label: "Beam Longitudinal (SMRF)", group: "Schedules & Details" },
  { key: "detail_column_longitudinal", label: "Column Longitudinal (SMRF)", group: "Schedules & Details" },
  { key: "detail_retaining_wall", label: "Retaining Wall Section", group: "Schedules & Details" },
  { key: "detail_roof_truss", label: "Steel Roof Truss Detail", group: "Schedules & Details" },
];

function renumberSheets(sheetList, prefix) {
  const cleanPrefix = (prefix || "S").trim().toUpperCase() || "S";
  return sheetList.map((sh, idx) => ({
    ...sh,
    sheet_no: `${cleanPrefix}-${idx + 1}`,
  }));
}

export default function CadDrawings({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);
  const panRef = useRef({ dragging: false, startX: 0, startY: 0, origX: 0, origY: 0 });
  const svgHostRef = useRef(null);
  const stateEpochRef = useRef(0);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [dwgStatus, setDwgStatus] = useState({
    available: false,
    backend: null,
    executable_path: null,
    install_hint: "",
  });
  const [composerOpen, setComposerOpen] = useState(true);
  const [activeSheet, setActiveSheet] = useState("");
  const [preview, setPreview] = useState(null);
  const [hiddenLayers, setHiddenLayers] = useState({});
  const [zoom, setZoom] = useState(1.0);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  async function apiJson(url, opts) {
    const res = await fetch(url, opts);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  async function loadSheetPreview(sheetNo) {
    try {
      const qs = sheetNo ? `?sheet_no=${encodeURIComponent(sheetNo)}` : "";
      const data = await apiJson(`/api/projects/${pid}/cad/preview-svg${qs}`);
      setPreview(data);
      setActiveSheet(data.sheet_no);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    let cancelled = false;
    const epoch = ++stateEpochRef.current;

    async function loadProjectState() {
      setBusy(true);
      setError("");
      try {
        const [p, dwg] = await Promise.all([
          apiJson(`/api/projects/${pid}`),
          apiJson(`/api/projects/${pid}/cad/dwg-status`).catch(() => ({
            available: false,
            backend: null,
            executable_path: null,
            install_hint: "",
          })),
        ]);
        if (cancelled || stateEpochRef.current !== epoch) return;
        setFullProject(p);
        setDwgStatus(dwg);
        if (p.cad_drawings?.options) {
          setOptions({ ...DEFAULT_OPTIONS, ...p.cad_drawings.options });
        }
        if (p.cad_drawings?.generated_at && p.cad_drawings?.sheets?.length) {
          await loadSheetPreview(p.cad_drawings.sheets[0].sheet_no);
        }
      } catch (err) {
        if (!cancelled && stateEpochRef.current === epoch) {
          setError(err.message);
        }
      } finally {
        if (!cancelled && stateEpochRef.current === epoch) {
          setBusy(false);
        }
      }
    }

    loadProjectState();
    return () => {
      cancelled = true;
    };
  }, [project.project_id]);

  useEffect(() => {
    const host = svgHostRef.current;
    if (!host) return;
    const groups = host.querySelectorAll("g[data-layer]");
    groups.forEach((g) => {
      const lname = g.getAttribute("data-layer");
      g.style.display = hiddenLayers[lname] ? "none" : "";
    });
  }, [preview, hiddenLayers]);

  const model = fullProject?.structural_model;
  const sizing = fullProject?.member_sizing;
  const ad = fullProject?.analysis_design;
  const cad = fullProject?.cad_drawings;

  const hasPrereqs = Boolean(
    model?.nodes?.length &&
      model?.members?.length &&
      sizing?.member_assignments?.length &&
      ad?.generated_at
  );

  // Selectable Plan items derived from backend available_items or project.levels
  const planCheckItems = useMemo(() => {
    const base = [
      { key: "general_notes", label: "General Notes" },
      { key: "foundation_plan", label: "Foundation Plan" },
    ];
    const fromApi = (cad?.available_items || []).filter((it) => it.key.startsWith("floor_plan:"));
    if (fromApi.length > 0) {
      return [...base, ...fromApi.map((it) => ({ key: it.key, label: it.label }))];
    }
    const lvls = [...(fullProject?.levels || [])].sort((a, b) => a.elevation_m - b.elevation_m);
    for (const lv of lvls) {
      base.push({
        key: `floor_plan:${lv.id}`,
        label: `${lv.id} Framing (EL. ${lv.elevation_m >= 0 ? "+" : ""}${lv.elevation_m.toFixed(2)}m)`,
      });
    }
    return base;
  }, [cad?.available_items, fullProject?.levels]);

  const sheets = cad?.sheets || [];
  const summaryByNo = useMemo(() => {
    const m = {};
    for (const s of sheets) m[s.sheet_no] = s;
    return m;
  }, [sheets]);

  const activeSheetMeta = useMemo(
    () => sheets.find((s) => s.sheet_no === activeSheet) || sheets[0] || null,
    [sheets, activeSheet]
  );

  const activeSheetConfig = useMemo(
    () =>
      (options.sheets || []).find((s) => s.sheet_no === activeSheet) ||
      (options.sheets || [])[0] ||
      null,
    [options.sheets, activeSheet]
  );

  const effectiveActiveScale = activeSheetConfig?.plan_scale || options.plan_scale;

  const layerList = useMemo(() => {
    const set = new Set(cad?.layers || []);
    for (const l of preview?.layers || []) set.add(l);
    return Array.from(set);
  }, [cad?.layers, preview?.layers]);

  async function runGenerateWithOptions(nextOptions, preferredSheet = null) {
    stateEpochRef.current += 1;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        options: {
          ...nextOptions,
          plan_scale: Number(nextOptions.plan_scale),
          detail_scale: Number(nextOptions.detail_scale),
          sheets: (nextOptions.sheets || []).map((s) => ({
            sheet_no: s.sheet_no,
            title: s.title || s.sheet_no,
            plan_scale: s.plan_scale ? Number(s.plan_scale) : null,
            items: s.items || [],
          })),
        },
      };
      const updated = await apiJson(`/api/projects/${pid}/cad/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setFullProject((prev) => (prev ? { ...prev, cad_drawings: updated } : prev));
      if (updated.options) {
        setOptions({ ...DEFAULT_OPTIONS, ...updated.options });
      }
      const targetSheet =
        preferredSheet && updated.sheets?.some((s) => s.sheet_no === preferredSheet)
          ? preferredSheet
          : updated.sheets?.[0]?.sheet_no || "";
      setZoom(1.0);
      setPan({ x: 0, y: 0 });
      if (targetSheet) {
        await loadSheetPreview(targetSheet);
      }
      const overflowCnt = (updated.sheets || []).filter((s) => s.overflow).length;
      setNotice(
        `Generated ${updated.sheets?.length || 0} AutoCAD sheet(s) (${updated.total_entities} CAD entities` +
          (overflowCnt > 0 ? `, ${overflowCnt} sheet(s) auto-fitted due to scale overflow` : "") +
          ")."
      );
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function handleApplyPreset(presetName) {
    const next = {
      ...options,
      preset: presetName,
      sheet_size: presetName === "a3_individual_floors" ? "A3" : "A1",
      plan_scale: presetName === "a3_individual_floors" ? 100 : 100,
      sheets: [], // let backend auto-compose the preset from actual levels
    };
    setOptions(next);
    if (hasPrereqs) {
      runGenerateWithOptions(next);
    }
  }

  function handleAddSheet() {
    const current = options.sheets || [];
    const prefix = (options.sheet_prefix || "S").trim().toUpperCase() || "S";
    const nextNo = `${prefix}-${current.length + 1}`;
    const nextSheets = [
      ...current,
      {
        sheet_no: nextNo,
        title: `CUSTOM STRUCTURAL SHEET ${current.length + 1}`,
        plan_scale: null,
        items: ["foundation_plan"],
      },
    ];
    setOptions({ ...options, preset: "custom", sheets: nextSheets });
  }

  function updateSheet(idx, patch) {
    const next = (options.sheets || []).map((s, i) => (i === idx ? { ...s, ...patch } : s));
    setOptions({ ...options, preset: "custom", sheets: next });
  }

  function toggleSheetItem(idx, itemKey) {
    const sh = (options.sheets || [])[idx];
    if (!sh) return;
    const exists = sh.items.includes(itemKey);
    const nextItems = exists ? sh.items.filter((k) => k !== itemKey) : [...sh.items, itemKey];
    updateSheet(idx, { items: nextItems });
  }

  function moveSheet(idx, delta) {
    const list = [...(options.sheets || [])];
    const target = idx + delta;
    if (target < 0 || target >= list.length) return;
    const [item] = list.splice(idx, 1);
    list.splice(target, 0, item);
    setOptions({
      ...options,
      preset: "custom",
      sheets: renumberSheets(list, options.sheet_prefix),
    });
  }

  function removeSheet(idx) {
    const list = (options.sheets || []).filter((_, i) => i !== idx);
    setOptions({
      ...options,
      preset: "custom",
      sheets: renumberSheets(list, options.sheet_prefix),
    });
  }

  async function handleSelectSheet(sheetNo) {
    setBusy(true);
    setError("");
    try {
      await loadSheetPreview(sheetNo);
      setZoom(1.0);
      setPan({ x: 0, y: 0 });
    } finally {
      setBusy(false);
    }
  }

  function handleDownloadDwg() {
    if (!dwgStatus?.available) {
      setError(
        dwgStatus?.install_hint ||
          "Native .dwg export requires ODA File Converter or AutoCAD Core Console. Install ODA File Converter or download the .dxf file directly."
      );
      return;
    }
    setError("");
    triggerDownload("download/dwg", `${project.project_id}_Structural_Plans.dwg`);
  }

  function triggerDownload(endpoint, filename) {
    const a = document.createElement("a");
    a.href = `/api/projects/${pid}/cad/${endpoint}`;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function toggleLayer(lname) {
    setHiddenLayers((prev) => ({ ...prev, [lname]: !prev[lname] }));
  }

  function onViewportPointerDown(e) {
    panRef.current = {
      dragging: true,
      startX: e.clientX,
      startY: e.clientY,
      origX: pan.x,
      origY: pan.y,
    };
  }
  function onViewportPointerMove(e) {
    if (!panRef.current.dragging) return;
    setPan({
      x: panRef.current.origX + (e.clientX - panRef.current.startX),
      y: panRef.current.origY + (e.clientY - panRef.current.startY),
    });
  }
  function onViewportPointerUp() {
    panRef.current.dragging = false;
  }
  function onViewportWheel(e) {
    e.preventDefault();
    setZoom((z) =>
      Math.max(0.5, Math.min(4.0, Number((z * (e.deltaY < 0 ? 1.15 : 0.87)).toFixed(2))))
    );
  }

  return (
    <section className="cad-builder">
      {/* Header */}
      <div className="geom-header">
        <div>
          <label>MODULE 07 · STRUCTURAL DRAWINGS (AUTOCAD .DXF PLANS, SCHEDULES &amp; DETAILS)</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="geom-header-actions">
          <button
            disabled={busy || !hasPrereqs}
            onClick={() => runGenerateWithOptions(options, activeSheet)}
          >
            {cad?.generated_at ? "Regenerate CAD Drawings" : "Generate CAD Drawings"}
          </button>
          <button
            className="secondary cad-dwg-btn"
            disabled={busy || !hasPrereqs}
            title={
              dwgStatus?.available
                ? `Export binary ${options.dwg_version} .dwg via ${dwgStatus.backend}`
                : dwgStatus?.install_hint || "Install ODA File Converter for native .dwg export"
            }
            onClick={handleDownloadDwg}
          >
            Download AutoCAD (.dwg)
          </button>
          <button
            className="secondary cad-download-btn"
            disabled={busy || !hasPrereqs}
            onClick={() => triggerDownload("download/dxf", `${project.project_id}_Structural_Plans.dxf`)}
          >
            Download DXF (.dxf)
          </button>
          <button
            className="secondary report-pdf-btn"
            disabled={busy || !hasPrereqs}
            onClick={() => triggerDownload("download/pdf", `${project.project_id}_Structural_Plans.pdf`)}
          >
            Download PDF (.pdf)
          </button>
          <button
            className="secondary loads-download-btn"
            disabled={busy || !hasPrereqs}
            onClick={() =>
              triggerDownload("download/zip", `${project.project_id}_Structural_Plans_Sheets.zip`)
            }
          >
            Download Sheets (.zip)
          </button>
          <button className="secondary" disabled={busy} onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {notice && <div className="notice">{notice}</div>}
      {error && <div className="notice error">{error}</div>}
      {!hasPrereqs && fullProject && (
        <div className="notice error">
          Complete Modules 03 (3D Geometry), 04 (Member Sizing), and 06 (Analysis &amp; RC Design)
          before generating AutoCAD .dxf / vector PDF structural drawings.
        </div>
      )}

      {/* Global Settings + Quick Presets Bar */}
      <div className="sizing-bulk-bar loads-preset-bar">
        <div className="loads-presets-row">
          <label className="design-param-field">
            <span>Sheet Size</span>
            <select
              aria-label="Sheet Size"
              value={options.sheet_size}
              onChange={(e) => setOptions({ ...options, sheet_size: e.target.value })}
            >
              <option value="A1">A1 (841×594)</option>
              <option value="A2">A2 (594×420)</option>
              <option value="A3">A3 (420×297)</option>
            </select>
          </label>
          <label className="design-param-field">
            <span>Plan Scale</span>
            <select
              aria-label="Plan Scale"
              value={options.plan_scale}
              onChange={(e) => setOptions({ ...options, plan_scale: Number(e.target.value) })}
            >
              <option value={50}>1:50</option>
              <option value={75}>1:75</option>
              <option value={100}>1:100</option>
              <option value={150}>1:150</option>
              <option value={200}>1:200</option>
            </select>
          </label>
          <label className="design-param-field">
            <span>Detail Scale</span>
            <select
              aria-label="Detail Scale"
              value={options.detail_scale}
              onChange={(e) => setOptions({ ...options, detail_scale: Number(e.target.value) })}
            >
              <option value={25}>1:25</option>
              <option value={20}>1:20</option>
              <option value={30}>1:30</option>
            </select>
          </label>
          <label className="design-param-field">
            <span>DWG Version</span>
            <select
              aria-label="DWG Version"
              value={options.dwg_version || "ACAD2013"}
              onChange={(e) => setOptions({ ...options, dwg_version: e.target.value })}
            >
              <option value="ACAD2013">ACAD2013</option>
              <option value="ACAD2018">ACAD2018</option>
              <option value="ACAD2010">ACAD2010</option>
            </select>
          </label>
          <label className="design-param-field">
            <span>Prefix</span>
            <input
              type="text"
              aria-label="Sheet Prefix"
              maxLength={6}
              value={options.sheet_prefix}
              onChange={(e) => {
                const pref = e.target.value;
                setOptions({
                  ...options,
                  sheet_prefix: pref,
                  sheets: renumberSheets(options.sheets || [], pref),
                });
              }}
            />
          </label>

          <div className="cad-preset-btns">
            <button
              type="button"
              className={options.preset === "a1_permit_set" ? "pdf-chip active" : "pdf-chip"}
              disabled={busy}
              onClick={() => handleApplyPreset("a1_permit_set")}
            >
              A1 Standard Permit Set
            </button>
            <button
              type="button"
              className={options.preset === "a3_individual_floors" ? "pdf-chip active" : "pdf-chip"}
              disabled={busy}
              onClick={() => handleApplyPreset("a3_individual_floors")}
            >
              A3 Individual Floor Set
            </button>
            <button
              type="button"
              className="pdf-chip"
              disabled={busy}
              onClick={handleAddSheet}
            >
              + Add Sheet
            </button>
            <button
              type="button"
              className="pdf-chip"
              onClick={() => setComposerOpen((v) => !v)}
            >
              {composerOpen ? "Hide Sheet Composer" : "Show Sheet Composer"}
            </button>
          </div>
        </div>
      </div>

      {/* Per-Page Sheet Composer Panel */}
      {composerOpen && (
        <div className="geom-panel cad-composer-panel">
          <div className="geom-panel-head">
            <div>
              <strong>Per-Page Sheet Composer</strong>
              <small>
                Assign exact plans, schedules, and 1:25 SMRF details to each drawing sheet, override
                scale per page, then click Regenerate CAD Drawings.
              </small>
            </div>
            <button
              className="secondary"
              disabled={busy || !hasPrereqs}
              onClick={() => runGenerateWithOptions(options, activeSheet)}
            >
              Apply Sheet Layout
            </button>
          </div>

          {(options.sheets || []).length === 0 ? (
            <div className="loads-empty">
              Click <strong>Generate CAD Drawings</strong> or choose a Quick Preset above to
              populate customizable sheets (<code>S-1</code>, <code>S-2</code>, <code>S-3</code>…).
            </div>
          ) : (
            <div className="cad-composer-grid">
              {options.sheets.map((sh, idx) => {
                const summary = summaryByNo[sh.sheet_no];
                const isOverflow = Boolean(summary?.overflow);
                return (
                  <div
                    key={`${sh.sheet_no}-${idx}`}
                    className={
                      activeSheet === sh.sheet_no ? "cad-sheet-card active" : "cad-sheet-card"
                    }
                  >
                    <div className="cad-sheet-card-head">
                      <span className="cad-sheet-badge">{sh.sheet_no}</span>
                      <input
                        type="text"
                        className="cad-sheet-title-input"
                        aria-label={`Title for ${sh.sheet_no}`}
                        value={sh.title}
                        onChange={(e) => updateSheet(idx, { title: e.target.value })}
                      />
                      <select
                        className="cad-sheet-scale-sel"
                        aria-label={`Scale for ${sh.sheet_no}`}
                        value={sh.plan_scale || ""}
                        onChange={(e) =>
                          updateSheet(idx, {
                            plan_scale: e.target.value ? Number(e.target.value) : null,
                          })
                        }
                      >
                        <option value="">Inherit (1:{options.plan_scale})</option>
                        <option value={50}>1:50</option>
                        <option value={75}>1:75</option>
                        <option value={100}>1:100</option>
                        <option value={150}>1:150</option>
                        <option value={200}>1:200</option>
                      </select>
                      {summary && (
                        <span
                          className={
                            isOverflow
                              ? "design-status-pill warn"
                              : "design-status-pill pass"
                          }
                        >
                          {isOverflow
                            ? "Overflow — reduce scale or split sheet"
                            : "Fits Sheet"}
                        </span>
                      )}
                      <div className="cad-sheet-move-btns">
                        <button
                          type="button"
                          className="pdf-chip"
                          title="Move sheet up"
                          disabled={idx === 0}
                          onClick={() => moveSheet(idx, -1)}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="pdf-chip"
                          title="Move sheet down"
                          disabled={idx === options.sheets.length - 1}
                          onClick={() => moveSheet(idx, 1)}
                        >
                          ↓
                        </button>
                        <button
                          type="button"
                          className="pdf-chip"
                          title="Remove sheet"
                          disabled={options.sheets.length <= 1}
                          onClick={() => removeSheet(idx)}
                        >
                          ×
                        </button>
                      </div>
                    </div>

                    <div className="cad-sheet-groups">
                      <div>
                        <small className="cad-group-lbl">Plans &amp; Notes</small>
                        <div className="cad-item-checks">
                          {planCheckItems.map((it) => (
                            <label key={it.key} className="cad-check-chip">
                              <input
                                type="checkbox"
                                checked={sh.items.includes(it.key)}
                                onChange={() => toggleSheetItem(idx, it.key)}
                              />
                              <span>{it.label}</span>
                            </label>
                          ))}
                        </div>
                      </div>
                      <div>
                        <small className="cad-group-lbl">Schedules &amp; 1:25 Details</small>
                        <div className="cad-item-checks">
                          {STATIC_SCHED_DETAIL_ITEMS.map((it) => (
                            <label key={it.key} className="cad-check-chip">
                              <input
                                type="checkbox"
                                checked={sh.items.includes(it.key)}
                                onChange={() => toggleSheetItem(idx, it.key)}
                              />
                              <span>{it.label}</span>
                            </label>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Main CAD Workspace */}
      <div className="geom-workspace">
        <div className="sizing-main-col">
          <div className="geom-panel">
            <div className="geom-panel-head">
              <div className="geom-floor-tabs">
                {sheets.length > 0 ? (
                  sheets.map((s) => (
                    <button
                      key={s.sheet_no}
                      className={activeSheet === s.sheet_no ? "pdf-chip active" : "pdf-chip"}
                      disabled={busy}
                      onClick={() => handleSelectSheet(s.sheet_no)}
                    >
                      {s.sheet_no} · {s.title.split(" ").slice(0, 3).join(" ")}
                      {s.overflow ? " ⚠" : ""}
                    </button>
                  ))
                ) : (
                  <small>No CAD sheets generated yet.</small>
                )}
              </div>
              <div className="cad-zoom-controls">
                <button
                  type="button"
                  className="pdf-chip"
                  title="Zoom out"
                  onClick={() => setZoom((z) => Math.max(0.5, Number((z - 0.25).toFixed(2))))}
                >
                  −
                </button>
                <span className="cad-zoom-readout">{Math.round(zoom * 100)}%</span>
                <button
                  type="button"
                  className="pdf-chip"
                  title="Zoom in"
                  onClick={() => setZoom((z) => Math.min(4.0, Number((z + 0.25).toFixed(2))))}
                >
                  +
                </button>
                <button
                  type="button"
                  className="pdf-chip"
                  onClick={() => {
                    setZoom(1.0);
                    setPan({ x: 0, y: 0 });
                  }}
                >
                  Fit Sheet
                </button>
              </div>
            </div>

            {preview?.svg ? (
              <div
                className="cad-viewport"
                onPointerDown={onViewportPointerDown}
                onPointerMove={onViewportPointerMove}
                onPointerUp={onViewportPointerUp}
                onPointerLeave={onViewportPointerUp}
                onWheel={onViewportWheel}
              >
                <div
                  ref={svgHostRef}
                  className="cad-viewport-inner"
                  style={{
                    transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                  }}
                  dangerouslySetInnerHTML={{ __html: preview.svg }}
                />
              </div>
            ) : (
              <div className="loads-empty">
                Click <strong>Generate CAD Drawings</strong> to build multi-sheet AutoCAD R2010{" "}
                <code>.dxf</code> &amp; true-scale vector <code>.pdf</code> plans, schedules, and
                1:25 SMRF details.
              </div>
            )}

            {activeSheetMeta && (
              <div className="geom-legend">
                <strong>
                  {activeSheetMeta.sheet_no} — {activeSheetMeta.title}
                </strong>
                <span>({activeSheetMeta.entity_count} entities)</span>
                <span>Drag to pan · Scroll or +/- to zoom · Toggle layers on right →</span>
              </div>
            )}
          </div>
        </div>

        {/* Right Sidebar: Live Drawing Summary & Layer Toggles */}
        <aside className="geom-sidebar">
          <div className="geom-checks-head">
            <h4>CAD Drawing Summary</h4>
            <span
              className={
                dwgStatus?.available ? "design-status-pill pass" : "design-status-pill warn"
              }
              title={dwgStatus?.install_hint || ""}
            >
              {dwgStatus?.available ? `DWG Ready (${dwgStatus.backend})` : "DXF Ready · No DWG Bin"}
            </span>
          </div>
          <div className="geom-stats-grid loads-kpi-sidebar">
            <div>
              <strong>{options.sheets?.length || sheets.length}</strong>
              <small>Sheets</small>
            </div>
            <div>
              <strong>{cad?.total_entities || 0}</strong>
              <small>DXF Entities</small>
            </div>
            <div>
              <strong>{options.sheet_size}</strong>
              <small>Sheet Size</small>
            </div>
            <div>
              <strong>1:{effectiveActiveScale}</strong>
              <small>Active Scale</small>
            </div>
          </div>

          <div className="geom-checks-head" style={{ marginTop: "6px" }}>
            <h4>CAD Layers ({layerList.length})</h4>
            {layerList.length > 0 && (
              <button type="button" className="pdf-chip" onClick={() => setHiddenLayers({})}>
                Show All
              </button>
            )}
          </div>
          <div className="cad-layer-list">
            {layerList.map((lname) => {
              const meta = LAYER_META[lname] || { color: "#e2e8f0", label: lname };
              const visible = !hiddenLayers[lname];
              return (
                <label key={lname} className="cad-layer-item">
                  <input
                    type="checkbox"
                    checked={visible}
                    onChange={() => toggleLayer(lname)}
                  />
                  <span className="sizing-swatch" style={{ background: meta.color }} />
                  <code>{lname}</code>
                  <small>{meta.label}</small>
                </label>
              );
            })}
          </div>

          {activeSheetMeta?.contents?.length > 0 && (
            <>
              <div className="geom-checks-head" style={{ marginTop: "6px" }}>
                <h4>Sheet Contents ({activeSheetMeta.sheet_no})</h4>
              </div>
              <div className="report-checklist">
                {activeSheetMeta.contents.map((item) => (
                  <div key={item} className="report-check-row ok">
                    <span>✓</span>
                    <small>{item}</small>
                  </div>
                ))}
              </div>
            </>
          )}

          {(cad?.warnings || []).length > 0 && (
            <>
              <div className="geom-checks-head" style={{ marginTop: "6px" }}>
                <h4>Layout Warnings ({cad.warnings.length})</h4>
              </div>
              {cad.warnings.map((w, idx) => (
                <div key={idx} className="geom-issue sev-warning">
                  <div className="geom-issue-top">
                    <span className="geom-sev-badge sev-warning">FIT</span>
                    <code>CAD-SCALE</code>
                  </div>
                  <p>{w}</p>
                </div>
              ))}
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
