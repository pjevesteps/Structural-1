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

export default function CadDrawings({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);
  const panRef = useRef({ dragging: false, startX: 0, startY: 0, origX: 0, origY: 0 });
  const svgHostRef = useRef(null);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [activeSheet, setActiveSheet] = useState("");
  const [preview, setPreview] = useState(null); // { sheet_no, title, layers, svg }
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

  async function loadProjectState() {
    setBusy(true);
    setError("");
    try {
      const p = await apiJson(`/api/projects/${pid}`);
      setFullProject(p);
      if (p.cad_drawings?.options) {
        setOptions({ ...DEFAULT_OPTIONS, ...p.cad_drawings.options });
      }
      if (p.cad_drawings?.generated_at && p.cad_drawings?.sheets?.length) {
        await loadSheetPreview(p.cad_drawings.sheets[0].sheet_no);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    loadProjectState();
  }, [project.project_id]);

  // Apply layer visibility toggles directly to rendered SVG <g data-layer="..."> nodes
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

  const sheets = cad?.sheets || [];
  const activeSheetMeta = useMemo(
    () => sheets.find((s) => s.sheet_no === activeSheet) || sheets[0] || null,
    [sheets, activeSheet]
  );

  const layerList = useMemo(() => {
    const set = new Set(cad?.layers || []);
    for (const l of preview?.layers || []) set.add(l);
    return Array.from(set);
  }, [cad?.layers, preview?.layers]);

  async function handleGenerate() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/cad/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          options: {
            ...options,
            plan_scale: Number(options.plan_scale),
            detail_scale: Number(options.detail_scale),
          },
        }),
      });
      setFullProject((prev) => (prev ? { ...prev, cad_drawings: updated } : prev));
      const firstSheet = updated.sheets?.[0]?.sheet_no || "";
      setZoom(1.0);
      setPan({ x: 0, y: 0 });
      if (firstSheet) {
        await loadSheetPreview(firstSheet);
      }
      setNotice(
        `Generated ${updated.sheets?.length || 0} AutoCAD sheet(s) (${updated.total_entities} CAD entities across ${updated.layers?.length || 0} layers).`
      );
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
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

  function handleDownloadDxf() {
    const a = document.createElement("a");
    a.href = `/api/projects/${pid}/cad/download/dxf`;
    a.download = `${project.project_id}_Structural_Plans.dxf`;
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
    setZoom((z) => Math.max(0.5, Math.min(4.0, Number((z * (e.deltaY < 0 ? 1.15 : 0.87)).toFixed(2)))));
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
          <button disabled={busy || !hasPrereqs} onClick={handleGenerate}>
            {cad?.generated_at ? "Regenerate CAD Drawings" : "Generate CAD Drawings"}
          </button>
          <button
            className="secondary cad-download-btn"
            disabled={busy || !hasPrereqs}
            onClick={handleDownloadDxf}
          >
            Download AutoCAD (.dxf)
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
          before generating AutoCAD .dxf drawings.
        </div>
      )}

      {/* Drawing Settings Bar */}
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
              <option value={100}>1:100</option>
              <option value={75}>1:75</option>
              <option value={50}>1:50</option>
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
            <span>Sheet Prefix</span>
            <input
              type="text"
              aria-label="Sheet Prefix"
              maxLength={6}
              value={options.sheet_prefix}
              onChange={(e) => setOptions({ ...options, sheet_prefix: e.target.value })}
            />
          </label>
        </div>

        <div className="loads-toggles-row">
          <span className="loads-bar-label">Sheets &amp; Sections:</span>
          {[
            ["include_general_notes", "General Notes"],
            ["include_foundation_plan", "Foundation Plan"],
            ["include_framing_plans", "Framing Plans"],
            ["include_schedules", "RC Schedules"],
            ["include_typical_details", "Typical Details"],
          ].map(([key, lbl]) => (
            <label key={key} className="loads-inline-field">
              <input
                type="checkbox"
                checked={options[key]}
                onChange={(e) => setOptions({ ...options, [key]: e.target.checked })}
              />
              <span>{lbl}</span>
            </label>
          ))}
        </div>
      </div>

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
                <code>.dxf</code> plans, schedules, and 1:25 typical RC details with live SVG
                inspection.
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

        {/* Right Sidebar: Drawing KPIs & CAD Layer Toggles */}
        <aside className="geom-sidebar">
          <div className="geom-checks-head">
            <h4>CAD Drawing Summary</h4>
          </div>
          <div className="geom-stats-grid loads-kpi-sidebar">
            <div>
              <strong>{sheets.length}</strong>
              <small>Sheets</small>
            </div>
            <div>
              <strong>{cad?.total_entities || 0}</strong>
              <small>DXF Entities</small>
            </div>
            <div>
              <strong>{cad?.options?.sheet_size || options.sheet_size}</strong>
              <small>Sheet Size</small>
            </div>
            <div>
              <strong>1:{cad?.options?.plan_scale || options.plan_scale}</strong>
              <small>Plan Scale</small>
            </div>
          </div>

          <div className="geom-checks-head" style={{ marginTop: "6px" }}>
            <h4>CAD Layers ({layerList.length})</h4>
            {layerList.length > 0 && (
              <button
                type="button"
                className="pdf-chip"
                onClick={() => setHiddenLayers({})}
              >
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
                <h4>Layout Notes ({cad.warnings.length})</h4>
              </div>
              {cad.warnings.map((w, idx) => (
                <div key={idx} className="geom-issue sev-info">
                  <div className="geom-issue-top">
                    <span className="geom-sev-badge sev-info">SCALE</span>
                    <code>CAD-FIT</code>
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
