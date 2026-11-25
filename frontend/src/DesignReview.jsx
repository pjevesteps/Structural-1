import React, { useEffect, useMemo, useRef, useState } from "react";

function project3D(pt, center, yaw, pitch, scale, width, height) {
  const dx = pt.x - center.x;
  const dy = pt.y - center.y;
  const dz = pt.z - center.z;
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);

  const x1 = dx * cy - dz * sy;
  const z1 = dx * sy + dz * cy;
  const y2 = dy * cp - z1 * sp;
  const z2 = dy * sp + z1 * cp;

  const camDist = 65;
  const persp = camDist / Math.max(camDist - z2, 12);
  return {
    sx: width * 0.5 + x1 * scale * persp,
    sy: height * 0.54 - y2 * scale * persp,
    depth: z2,
  };
}

function pointToSegmentDist(px, py, x1, y1, x2, y2) {
  const vx = x2 - x1;
  const vy = y2 - y1;
  const lenSq = vx * vx + vy * vy;
  if (lenSq < 1e-6) return Math.hypot(px - x1, py - y1);
  const t = Math.max(0, Math.min(1, ((px - x1) * vx + (py - y1) * vy) / lenSq));
  return Math.hypot(px - (x1 + t * vx), py - (y1 + t * vy));
}

function dcrColor(dcr, status) {
  if (status === "FAIL" || dcr > 0.95) return "#ef4444";
  if (dcr >= 0.7) return "#f59e0b";
  return "#10b981";
}

export default function DesignReview({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);
  const canvasRef = useRef(null);
  const fileInputRef = useRef(null);
  const orbitRef = useRef({ dragging: false, moved: false, lastX: 0, lastY: 0 });
  const projectedRef = useRef([]);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [autoLog, setAutoLog] = useState(null);
  const [enforceScwb, setEnforceScwb] = useState(false);

  // Pass C-2 Secondary Element Forms
  const [rwForm, setRwForm] = useState({
    wall_id: "RW-1",
    label: "Perimeter Basement Retaining Wall",
    retained_height_m: 2.8,
    total_length_m: 12.0,
    stem_thickness_mm: 250,
    base_width_m: 1.8,
    toe_width_m: 0.45,
    base_thickness_mm: 300,
    soil_unit_weight_kn_m3: 18.0,
    friction_angle_deg: 30.0,
    surcharge_kpa: 10.0,
    water_table_height_m: 0.0,
    allowable_bearing_kpa: 150.0,
    is_restrained_top: true,
  });
  const [stairForm, setStairForm] = useState({
    stair_id: "ST-1",
    flight_span_m: 3.6,
    riser_mm: 175,
    tread_mm: 275,
    width_m: 1.2,
    waist_thickness_mm: 150,
    live_load_kpa: 4.8,
  });
  const [trussForm, setTrussForm] = useState({
    truss_id: "TR-1",
    span_m: 8.0,
    truss_spacing_m: 3.0,
    pitch_deg: 18.0,
    truss_type: "Pratt",
    steel_fy_mpa: 248.0,
  });
  const [strapForm, setStrapForm] = useState({
    strap_id: "SF-1",
    exterior_node_id: 1,
    interior_node_id: 2,
  });

  // Active tab: util3d | drift | beams | columns | footings | smrf | slabs | retwalls | trusses
  const [activeTab, setActiveTab] = useState("util3d");

  // Design parameters state
  const [params, setParams] = useState({
    clear_cover_beam_mm: 40,
    clear_cover_column_mm: 40,
    clear_cover_footing_mm: 75,
    main_bar_dia_beam_mm: 16,
    main_bar_dia_col_mm: 20,
    stirrup_tie_dia_mm: 10,
    soil_bearing_qa_kpa: 150,
    footing_embedment_m: 1.5,
    soil_unit_weight_kn_m3: 18,
    include_staad_concrete_design: true,
  });

  // 3D Viewer state
  const [storyFilter, setStoryFilter] = useState("ALL");
  const [selectedMemberId, setSelectedMemberId] = useState(null);
  const [yaw, setYaw] = useState(-0.68);
  const [pitch, setPitch] = useState(0.46);
  const [zoom3d, setZoom3d] = useState(1.0);

  // Table filters
  const [beamFloor, setBeamFloor] = useState("ALL");
  const [beamSection, setBeamSection] = useState("ALL");
  const [beamStatus, setBeamStatus] = useState("ALL");
  const [colFloor, setColFloor] = useState("ALL");
  const [colSection, setColSection] = useState("ALL");
  const [colStatus, setColStatus] = useState("ALL");

  async function apiJson(url, opts) {
    const res = await fetch(url, opts);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  async function loadProjectState() {
    setBusy(true);
    setError("");
    try {
      const p = await apiJson(`/api/projects/${pid}`);
      setFullProject(p);
      if (p.analysis_design?.params) {
        const synced = { ...p.analysis_design.params };
        const pedDrop = p.structural_model?.options?.footing_depth_m || 0;
        if (pedDrop > 0 && !p.analysis_design.generated_at) {
          synced.footing_embedment_m = pedDrop;
        }
        setParams(synced);
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

  const model = fullProject?.structural_model;
  const sizing = fullProject?.member_sizing;
  const lg = fullProject?.load_generation;
  const ad = fullProject?.analysis_design;

  const hasPrereqs = Boolean(
    model?.nodes?.length &&
      model?.members?.length &&
      sizing?.member_assignments?.length &&
      lg?.generated_at
  );

  const beamByMid = useMemo(() => {
    const m = {};
    for (const b of ad?.beam_designs || []) m[b.member_id] = b;
    return m;
  }, [ad?.beam_designs]);

  const colByMid = useMemo(() => {
    const m = {};
    for (const c of ad?.column_designs || []) m[c.member_id] = c;
    return m;
  }, [ad?.column_designs]);

  const floorOptions = useMemo(() => {
    const set = new Set((fullProject?.levels || []).map((l) => l.id));
    for (const m of model?.members || []) {
      if (m.floor_level) set.add(m.floor_level);
    }
    return Array.from(set);
  }, [fullProject?.levels, model?.members]);

  const beamSections = useMemo(
    () => Array.from(new Set((ad?.beam_designs || []).map((b) => b.section_id))).sort(),
    [ad?.beam_designs]
  );
  const colSections = useMemo(
    () => Array.from(new Set((ad?.column_designs || []).map((c) => c.section_id))).sort(),
    [ad?.column_designs]
  );

  const filteredBeams = useMemo(
    () =>
      (ad?.beam_designs || []).filter((b) => {
        if (beamFloor !== "ALL" && b.floor_level !== beamFloor) return false;
        if (beamSection !== "ALL" && b.section_id !== beamSection) return false;
        if (beamStatus !== "ALL" && b.status !== beamStatus) return false;
        return true;
      }),
    [ad?.beam_designs, beamFloor, beamSection, beamStatus]
  );

  const filteredCols = useMemo(
    () =>
      (ad?.column_designs || []).filter((c) => {
        if (colFloor !== "ALL" && c.floor_level !== colFloor) return false;
        if (colSection !== "ALL" && c.section_id !== colSection) return false;
        if (colStatus !== "ALL" && c.status !== colStatus) return false;
        return true;
      }),
    [ad?.column_designs, colFloor, colSection, colStatus]
  );

  // Health KPIs
  const kpis = useMemo(() => {
    const beams = ad?.beam_designs || [];
    const cols = ad?.column_designs || [];
    const ftgs = ad?.footing_designs || [];
    const drifts = ad?.story_drifts || [];
    const joints = ad?.smrf_joint_checks || [];
    const beamPass = beams.filter((b) => b.status === "PASS").length;
    const colPass = cols.filter((c) => c.status === "PASS").length;
    const ftgPass = ftgs.filter((f) => f.status === "PASS").length;
    const jointPass = joints.filter(
      (j) => j.scwb_status === "PASS" && j.joint_shear_status === "PASS"
    ).length;
    const maxDrift = drifts.reduce(
      (acc, d) => Math.max(acc, d.drift_ratio_x, d.drift_ratio_z),
      0
    );
    const maxColDcr = cols.reduce((acc, c) => Math.max(acc, c.governing_dcr), 0);
    const maxBeamDcr = beams.reduce((acc, b) => Math.max(acc, b.governing_dcr), 0);
    return {
      beamPass,
      beamTotal: beams.length,
      colPass,
      colTotal: cols.length,
      ftgPass,
      ftgTotal: ftgs.length,
      jointPass,
      jointTotal: joints.length,
      maxDrift,
      maxColDcr,
      maxBeamDcr,
    };
  }, [ad]);

  const validationIssues = ad?.validation_issues || [];
  const errorCount = validationIssues.filter((i) => i.severity === "error").length;
  const warningCount = validationIssues.filter((i) => i.severity === "warning").length;

  // Draw 3D Utilization Canvas
  useEffect(() => {
    if (activeTab !== "util3d") return;
    const canvas = canvasRef.current;
    if (!canvas || !model?.nodes?.length) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(320, rect.width);
    const h = Math.max(360, rect.height);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#070c14";
    ctx.fillRect(0, 0, w, h);

    const nodes = model.nodes;
    const nodeById = {};
    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity,
      minZ = Infinity,
      maxZ = -Infinity;
    for (const n of nodes) {
      nodeById[n.node_id] = n;
      minX = Math.min(minX, n.x_m);
      maxX = Math.max(maxX, n.x_m);
      minY = Math.min(minY, n.y_m);
      maxY = Math.max(maxY, n.y_m);
      minZ = Math.min(minZ, n.z_m);
      maxZ = Math.max(maxZ, n.z_m);
    }
    const center = {
      x: (minX + maxX) * 0.5,
      y: (minY + maxY) * 0.5,
      z: (minZ + maxZ) * 0.5,
    };
    const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 4.0);
    const scale = (Math.min(w, h) * 0.62 * zoom3d) / span;

    const projNodes = {};
    for (const n of nodes) {
      projNodes[n.node_id] = project3D(
        { x: n.x_m, y: n.y_m, z: n.z_m },
        center,
        yaw,
        pitch,
        scale,
        w,
        h
      );
    }

    const segs = [];
    for (const m of model.members || []) {
      if (storyFilter !== "ALL" && m.floor_level !== storyFilter) continue;
      const pa = projNodes[m.start_node];
      const pb = projNodes[m.end_node];
      if (!pa || !pb) continue;
      const res = m.member_type === "column" ? colByMid[m.member_id] : beamByMid[m.member_id];
      const dcr = res ? res.governing_dcr : 0;
      const status = res ? res.status : "PASS";
      const color = res ? dcrColor(dcr, status) : "#475569";
      segs.push({
        member: m,
        res,
        pa,
        pb,
        depth: (pa.depth + pb.depth) * 0.5,
        color,
      });
    }
    segs.sort((a, b) => a.depth - b.depth);
    projectedRef.current = segs;

    for (const seg of segs) {
      const isSel = seg.member.member_id === selectedMemberId;
      ctx.beginPath();
      ctx.moveTo(seg.pa.sx, seg.pa.sy);
      ctx.lineTo(seg.pb.sx, seg.pb.sy);
      ctx.strokeStyle = isSel ? "#ffffff" : seg.color;
      ctx.lineWidth = isSel ? 4.5 : seg.member.member_type === "column" ? 3.2 : 2.2;
      ctx.stroke();
    }

    // Draw support triangles
    for (const n of nodes) {
      if (!n.is_support) continue;
      const p = projNodes[n.node_id];
      if (!p) continue;
      ctx.fillStyle = n.support_type === "FIXED" ? "#38bdf8" : "#f59e0b";
      ctx.beginPath();
      ctx.moveTo(p.sx, p.sy);
      ctx.lineTo(p.sx - 5, p.sy + 9);
      ctx.lineTo(p.sx + 5, p.sy + 9);
      ctx.closePath();
      ctx.fill();
    }

    // XYZ triad in bottom-left
    const orig = { x: 0, y: 0, z: 0 };
    const oP = project3D(orig, orig, yaw, pitch, 24, 100, 100);
    const ox = 46;
    const oy = h - 42;
    for (const [vec, col, lbl] of [
      [{ x: 1, y: 0, z: 0 }, "#f87171", "X"],
      [{ x: 0, y: 1, z: 0 }, "#34d399", "Y"],
      [{ x: 0, y: 0, z: 1 }, "#60a5fa", "Z"],
    ]) {
      const p = project3D(vec, orig, yaw, pitch, 24, 100, 100);
      const dx = p.sx - oP.sx;
      const dy = p.sy - oP.sy;
      ctx.beginPath();
      ctx.moveTo(ox, oy);
      ctx.lineTo(ox + dx, oy + dy);
      ctx.strokeStyle = col;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = col;
      ctx.font = "bold 10px sans-serif";
      ctx.fillText(lbl, ox + dx * 1.25 - 3, oy + dy * 1.25 + 3);
    }
  }, [activeTab, model, ad, beamByMid, colByMid, storyFilter, selectedMemberId, yaw, pitch, zoom3d]);

  function onCanvasPointerDown(e) {
    orbitRef.current = { dragging: true, moved: false, lastX: e.clientX, lastY: e.clientY };
  }
  function onCanvasPointerMove(e) {
    if (!orbitRef.current.dragging) return;
    const dx = e.clientX - orbitRef.current.lastX;
    const dy = e.clientY - orbitRef.current.lastY;
    if (Math.hypot(dx, dy) > 2) orbitRef.current.moved = true;
    orbitRef.current.lastX = e.clientX;
    orbitRef.current.lastY = e.clientY;
    setYaw((y) => y + dx * 0.01);
    setPitch((p) => Math.max(-1.35, Math.min(1.35, p + dy * 0.01)));
  }
  function onCanvasPointerUp(e) {
    const wasDrag = orbitRef.current.moved;
    orbitRef.current.dragging = false;
    if (wasDrag) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    let best = null;
    let bestDist = 12;
    for (const seg of projectedRef.current) {
      const d = pointToSegmentDist(px, py, seg.pa.sx, seg.pa.sy, seg.pb.sx, seg.pb.sy);
      if (d < bestDist) {
        bestDist = d;
        best = seg.member.member_id;
      }
    }
    setSelectedMemberId(best);
  }
  function onCanvasWheel(e) {
    e.preventDefault();
    setZoom3d((z) => Math.max(0.4, Math.min(3.5, z * (e.deltaY < 0 ? 1.1 : 0.91))));
  }

  async function handleRunDesign() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/design/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params }),
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updated } : prev));
      setNotice("3D DSM elastic analysis, NSCP 2015 drift checks, and RC member design completed.");
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleAutoResolve() {
    setBusy(true);
    setError("");
    setNotice("");
    setAutoLog(null);
    try {
      const res = await apiJson(`/api/projects/${pid}/design/auto-resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_iterations: 6, enforce_scwb: enforceScwb }),
      });
      setAutoLog(res);
      setFullProject((prev) => (prev ? { ...prev, analysis_design: res.analysis_design } : prev));
      await loadProjectState();
      setNotice(
        res.converged
          ? `Auto-Resolve converged in ${res.iterations_run} iteration(s): 0 failing members or drifts.`
          : `Auto-Resolve stopped after ${res.iterations_run} iteration(s) with failures remaining.`
      );
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUploadAnl(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const updated = await apiJson(`/api/projects/${pid}/design/upload-anl`, {
        method: "POST",
        body: fd,
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updated } : prev));
      setNotice(`Imported STAAD.Pro results from ${file.name} and updated NSCP 2015 RC design.`);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function handleExportCsv() {
    const a = document.createElement("a");
    a.href = `/api/projects/${pid}/design/export-csv`;
    a.download = `${project.project_id}_rc_schedules.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function handleApproveDesign() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/design/approve`, {
        method: "POST",
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updated } : prev));
      setNotice("Engineer approval recorded for Module 06 Analysis & RC Design.");
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function postSecondary(endpoint, body, msg) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/design/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updated } : prev));
      setNotice(msg);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteSecondary(endpoint, msg) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/design/${endpoint}`, {
        method: "DELETE",
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updated } : prev));
      setNotice(msg);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const selectedBeam = selectedMemberId != null ? beamByMid[selectedMemberId] : null;
  const selectedCol = selectedMemberId != null ? colByMid[selectedMemberId] : null;

  return (
    <section className="design-builder">
      {/* Header */}
      <div className="geom-header">
        <div>
          <label>MODULE 06 · ANALYSIS BRIDGE, NSCP 2015 DRIFT CHECKS &amp; RC DESIGN</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="geom-header-actions">
          <button disabled={busy || !hasPrereqs} onClick={handleRunDesign}>
            {ad?.generated_at ? "Re-Run Analysis & RC Design" : "Run Analysis & RC Design"}
          </button>
          <button
            className="secondary design-autoresolve-btn"
            disabled={busy || !hasPrereqs}
            onClick={handleAutoResolve}
          >
            Auto-Resolve Failures
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".anl,.txt"
            style={{ display: "none" }}
            onChange={handleUploadAnl}
          />
          <button
            className="secondary design-anl-btn"
            disabled={busy || !model?.nodes?.length}
            onClick={() => fileInputRef.current?.click()}
          >
            Upload STAAD .anl
          </button>
          <button
            className="secondary loads-download-btn"
            disabled={busy || !ad?.generated_at}
            onClick={handleExportCsv}
          >
            Export Schedules (CSV)
          </button>
          <button
            className="secondary"
            disabled={busy || !ad?.generated_at || errorCount > 0}
            onClick={handleApproveDesign}
          >
            {ad?.engineer_approved ? "Approved ✓" : "Approve Design"}
          </button>
          <button className="secondary" disabled={busy} onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {!hasPrereqs && fullProject && (
        <div className="pdf-error">
          Complete Modules 03 (Geometry), 04 (Member Sizing), and 05 (NSCP Loads) before running
          internal analysis &amp; RC design.
        </div>
      )}
      {error && <div className="pdf-error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}
      {autoLog && (
        <div className="notice auto-resolve-log">
          <strong>
            Auto-Resolve {autoLog.converged ? "converged" : "did not fully converge"} —{" "}
            {autoLog.iterations_run} iteration(s)
          </strong>
          <ul className="auto-resolve-list">
            {autoLog.history.map((h) => (
              <li key={h.iteration}>
                Iter {h.iteration}: {h.fail_beams} beam / {h.fail_columns} column / {h.fail_footings}{" "}
                footing / {h.fail_drifts} drift failures (max drift ratio {h.max_drift_ratio})
                {h.actions?.length ? ` — ${h.actions.join("; ")}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Design Parameter Bar */}
      <div className="geom-toolbar design-param-bar">
        <label className="design-param-field">
          <span>Beam Cover (mm)</span>
          <input
            type="number"
            min="20"
            max="75"
            value={params.clear_cover_beam_mm}
            onChange={(e) =>
              setParams((p) => ({ ...p, clear_cover_beam_mm: parseFloat(e.target.value) || 40 }))
            }
          />
        </label>
        <label className="design-param-field">
          <span>Col Cover (mm)</span>
          <input
            type="number"
            min="20"
            max="75"
            value={params.clear_cover_column_mm}
            onChange={(e) =>
              setParams((p) => ({ ...p, clear_cover_column_mm: parseFloat(e.target.value) || 40 }))
            }
          />
        </label>
        <label className="design-param-field">
          <span>Ftg Cover (mm)</span>
          <input
            type="number"
            min="50"
            max="100"
            value={params.clear_cover_footing_mm}
            onChange={(e) =>
              setParams((p) => ({
                ...p,
                clear_cover_footing_mm: parseFloat(e.target.value) || 75,
              }))
            }
          />
        </label>
        <label className="design-param-field">
          <span>Beam Bar (mm)</span>
          <select
            value={String(params.main_bar_dia_beam_mm)}
            onChange={(e) =>
              setParams((p) => ({ ...p, main_bar_dia_beam_mm: parseInt(e.target.value, 10) }))
            }
          >
            {[12, 16, 20, 25, 28].map((d) => (
              <option key={d} value={d}>
                {d} mm
              </option>
            ))}
          </select>
        </label>
        <label className="design-param-field">
          <span>Col Bar (mm)</span>
          <select
            value={String(params.main_bar_dia_col_mm)}
            onChange={(e) =>
              setParams((p) => ({ ...p, main_bar_dia_col_mm: parseInt(e.target.value, 10) }))
            }
          >
            {[16, 20, 25, 28, 32].map((d) => (
              <option key={d} value={d}>
                {d} mm
              </option>
            ))}
          </select>
        </label>
        <label className="design-param-field">
          <span>Tie/Stirrup (mm)</span>
          <select
            value={String(params.stirrup_tie_dia_mm)}
            onChange={(e) =>
              setParams((p) => ({ ...p, stirrup_tie_dia_mm: parseInt(e.target.value, 10) }))
            }
          >
            <option value="10">10 mm</option>
            <option value="12">12 mm</option>
          </select>
        </label>
        <label className="design-param-field">
          <span>Soil qa (kPa)</span>
          <input
            type="number"
            step="10"
            min="30"
            value={params.soil_bearing_qa_kpa}
            onChange={(e) =>
              setParams((p) => ({ ...p, soil_bearing_qa_kpa: parseFloat(e.target.value) || 150 }))
            }
          />
        </label>
        <label className="design-param-field">
          <span>Embedment (m)</span>
          <input
            type="number"
            step="0.1"
            min="0"
            value={params.footing_embedment_m}
            onChange={(e) =>
              setParams((p) => ({ ...p, footing_embedment_m: parseFloat(e.target.value) || 1.5 }))
            }
          />
        </label>
        <label className="pdf-check">
          <input
            type="checkbox"
            checked={params.include_staad_concrete_design}
            onChange={(e) =>
              setParams((p) => ({ ...p, include_staad_concrete_design: e.target.checked }))
            }
          />
          STAAD ACI Design Block
        </label>
        <label className="pdf-check" title="Upsize columns during Auto-Resolve if Sum(Mnc)/Sum(Mnb) < 1.20 or joint shear fails">
          <input
            type="checkbox"
            checked={enforceScwb}
            onChange={(e) => setEnforceScwb(e.target.checked)}
          />
          Enforce SCWB &ge; 1.20
        </label>
      </div>

      {/* Main Split Workspace */}
      <div className="geom-workspace">
        <div className="sizing-main-col">
          {/* 9 Workspace Tabs */}
          <div className="geom-floor-tabs loads-tabs">
            {[
              ["util3d", "1. 3D Utilization View"],
              ["drift", "2. Story Drift & Reactions"],
              ["beams", `3. Beam Schedule (${kpis.beamTotal})`],
              ["columns", `4. Column Schedule (${kpis.colTotal})`],
              ["footings", `5. Footing Schedule (${kpis.ftgTotal})`],
              ["smrf", `6. SMRF Joints (SCWB) (${kpis.jointTotal})`],
              ["slabs", `7. Slabs & Stairs (${(ad?.slab_designs || []).length}/${(ad?.stair_designs || []).length})`],
              ["retwalls", `8. Retaining Walls (${(ad?.retaining_wall_designs || []).length})`],
              ["trusses", `9. Roof Trusses & Straps (${(ad?.roof_truss_designs || []).length}/${(ad?.strap_footing_designs || []).length})`],
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={activeTab === key ? "" : "secondary"}
                onClick={() => setActiveTab(key)}
              >
                {label}
              </button>
            ))}
          </div>

          {/* TAB 1: 3D UTILIZATION VIEW */}
          {activeTab === "util3d" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>3D Utilization Wireframe (Color-Coded by Governing DCR)</strong>
                  <small>
                    Drag to orbit · Scroll to zoom · Click any beam or column to inspect Mu, Vu,
                    Pu, DCR &amp; rebar callout.
                  </small>
                </div>
                <div className="geom-floor-tabs">
                  <select
                    className="loads-search"
                    value={storyFilter}
                    onChange={(e) => setStoryFilter(e.target.value)}
                  >
                    <option value="ALL">All Floors</option>
                    {floorOptions.map((lv) => (
                      <option key={lv} value={lv}>
                        {lv}
                      </option>
                    ))}
                  </select>
                  <button
                    className="secondary mini"
                    onClick={() => {
                      setYaw(-0.68);
                      setPitch(0.46);
                      setZoom3d(1.0);
                    }}
                  >
                    Reset View
                  </button>
                </div>
              </div>

              <canvas
                ref={canvasRef}
                className="geom-canvas-3d"
                onPointerDown={onCanvasPointerDown}
                onPointerMove={onCanvasPointerMove}
                onPointerUp={onCanvasPointerUp}
                onPointerLeave={() => {
                  orbitRef.current.dragging = false;
                }}
                onWheel={onCanvasWheel}
              />

              <div className="geom-legend">
                <span>
                  <i style={{ background: "#10b981" }} /> DCR &lt; 0.70 (Safe)
                </span>
                <span>
                  <i style={{ background: "#f59e0b" }} /> 0.70 – 0.95 (High Util)
                </span>
                <span>
                  <i style={{ background: "#ef4444" }} /> &gt; 0.95 / FAIL
                </span>
                <span>
                  <i style={{ background: "#475569" }} /> Not Designed Yet
                </span>
              </div>

              {(selectedBeam || selectedCol) && (
                <div className="design-inspect-card">
                  {selectedBeam && (
                    <>
                      <div className="design-inspect-top">
                        <strong>
                          Beam M{selectedBeam.member_id} · {selectedBeam.section_id} (
                          {selectedBeam.width_mm}×{selectedBeam.depth_mm} mm,{" "}
                          {selectedBeam.floor_level})
                        </strong>
                        <span
                          className={`design-status-pill ${
                            selectedBeam.status === "PASS" ? "pass" : "fail"
                          }`}
                        >
                          {selectedBeam.status} · DCR {selectedBeam.governing_dcr.toFixed(2)}
                        </span>
                      </div>
                      <div className="design-inspect-grid">
                        <span>
                          Mu(-Start / +Mid / -End): <b>{selectedBeam.mu_neg_start_knm}</b> /{" "}
                          <b>{selectedBeam.mu_pos_mid_knm}</b> /{" "}
                          <b>{selectedBeam.mu_neg_end_knm} kNm</b>
                        </span>
                        <span>
                          Vu Max: <b>{selectedBeam.vu_max_kn} kN</b> ({selectedBeam.governing_combo})
                        </span>
                        <span>
                          Rebar (Top-S / Bot-M / Top-E): <b>{selectedBeam.bars_top_start}</b> /{" "}
                          <b>{selectedBeam.bars_bot_mid}</b> / <b>{selectedBeam.bars_top_end}</b>
                        </span>
                        <span>
                          Stirrups: <b>{selectedBeam.stirrups_support}</b> (supp) ·{" "}
                          <b>{selectedBeam.stirrups_midspan}</b> (mid)
                        </span>
                      </div>
                    </>
                  )}
                  {selectedCol && (
                    <>
                      <div className="design-inspect-top">
                        <strong>
                          Column M{selectedCol.member_id} · {selectedCol.section_id} (
                          {selectedCol.width_mm}×{selectedCol.depth_mm} mm,{" "}
                          {selectedCol.floor_level})
                        </strong>
                        <span
                          className={`design-status-pill ${
                            selectedCol.status === "PASS" ? "pass" : "fail"
                          }`}
                        >
                          {selectedCol.status} · P-M DCR {selectedCol.pm_dcr.toFixed(2)}
                        </span>
                      </div>
                      <div className="design-inspect-grid">
                        <span>
                          Pu (Max / Min): <b>{selectedCol.pu_max_kn}</b> /{" "}
                          <b>{selectedCol.pu_min_kn} kN</b>
                        </span>
                        <span>
                          Mux / Muy: <b>{selectedCol.mux_max_knm}</b> /{" "}
                          <b>{selectedCol.muy_max_knm} kNm</b> · Vu: <b>{selectedCol.vu_max_kn} kN</b>
                        </span>
                        <span>
                          Main Bars: <b>{selectedCol.main_bars}</b> (&rho; ={" "}
                          {(selectedCol.rho_provided * 100).toFixed(2)}%)
                        </span>
                        <span>
                          Ties: <b>{selectedCol.ties_confined}</b> (lo) ·{" "}
                          <b>{selectedCol.ties_mid}</b> (mid)
                        </span>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          )}

          {/* TAB 2: STORY DRIFT & SUPPORT REACTIONS */}
          {activeTab === "drift" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>NSCP 2015 Section 208.6.4 · Inelastic Story Drift (&Delta;M = 0.7 R &Delta;s)</strong>
                    <small>
                      Allowable drift: 0.025h (T &lt; 0.7 s) or 0.020h (T &ge; 0.7 s) · Table 208-9
                      soft-story check included.
                    </small>
                  </div>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Story</th>
                        <th>h (m)</th>
                        <th>&Delta;s X / Z (mm)</th>
                        <th>&Delta;M X / Z (mm)</th>
                        <th>Allow (mm)</th>
                        <th>Ratio X / Z</th>
                        <th>Drift Bar</th>
                        <th>Soft?</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...(ad?.story_drifts || [])].reverse().map((d) => {
                        const maxR = Math.max(d.drift_ratio_x, d.drift_ratio_z);
                        return (
                          <tr key={d.floor_level}>
                            <td>
                              <b>{d.floor_level}</b>
                            </td>
                            <td>{d.story_height_m.toFixed(2)}</td>
                            <td>
                              {d.delta_s_x_mm.toFixed(2)} / {d.delta_s_z_mm.toFixed(2)}
                            </td>
                            <td>
                              {d.delta_m_x_mm.toFixed(1)} / {d.delta_m_z_mm.toFixed(1)}
                            </td>
                            <td>{d.allowable_drift_mm.toFixed(1)}</td>
                            <td>
                              <b>{d.drift_ratio_x.toFixed(2)}</b> /{" "}
                              <b>{d.drift_ratio_z.toFixed(2)}</b>
                            </td>
                            <td className="loads-bar-cell">
                              <div className="loads-bar-track">
                                <div
                                  className="loads-bar-fill"
                                  style={{
                                    width: `${Math.min(100, maxR * 100)}%`,
                                    background: dcrColor(maxR, d.status),
                                  }}
                                />
                              </div>
                            </td>
                            <td>
                              {d.soft_story_flag ? (
                                <span className="design-status-pill warn">SOFT</span>
                              ) : (
                                "—"
                              )}
                            </td>
                            <td>
                              <span
                                className={`design-status-pill ${
                                  d.status === "PASS" ? "pass" : "fail"
                                }`}
                              >
                                {d.status}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                      {!(ad?.story_drifts || []).length && (
                        <tr>
                          <td colSpan={9} className="loads-empty">
                            Run Analysis &amp; RC Design or upload a STAAD .anl file to inspect
                            story drifts.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>Support Reactions Envelope (ASD Service &amp; LRFD Factored)</strong>
                    <small>Negative Min ASD Fy indicates net uplift.</small>
                  </div>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Node</th>
                        <th>Grid / Floor</th>
                        <th>Max ASD Fy (kN)</th>
                        <th>Min ASD Fy (kN)</th>
                        <th>Max LRFD Pu (kN)</th>
                        <th>Max Mux / Muz (kNm)</th>
                        <th>Max V (kN)</th>
                        <th>Governing LRFD</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ad?.support_reactions || []).map((r) => (
                        <tr key={r.node_id}>
                          <td>
                            <code>N{r.node_id}</code>
                          </td>
                          <td>
                            {r.grid_ref || "—"} · {r.floor_level}
                          </td>
                          <td>
                            <b>{r.max_asd_fy_kn.toFixed(1)}</b>
                          </td>
                          <td style={{ color: r.min_asd_fy_kn < 0 ? "#fca5a5" : undefined }}>
                            {r.min_asd_fy_kn.toFixed(1)}
                          </td>
                          <td>
                            <b>{r.max_lrfd_pu_kn.toFixed(1)}</b>
                          </td>
                          <td>
                            {r.max_lrfd_mux_knm.toFixed(1)} / {r.max_lrfd_muz_knm.toFixed(1)}
                          </td>
                          <td>{r.max_lrfd_v_kn.toFixed(1)}</td>
                          <td>
                            <small>{r.governing_lrfd_combo}</small>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* TAB 3: BEAM DESIGN SCHEDULE */}
          {activeTab === "beams" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>RC Beam Reinforcement Schedule (NSCP 2015 / ACI 318)</strong>
                  <small>Flexural reinforcement &amp; SMRF 2-leg closed stirrups.</small>
                </div>
                <div className="geom-floor-tabs">
                  <select
                    className="loads-search"
                    value={beamFloor}
                    onChange={(e) => setBeamFloor(e.target.value)}
                  >
                    <option value="ALL">All Floors</option>
                    {floorOptions.map((lv) => (
                      <option key={lv} value={lv}>
                        {lv}
                      </option>
                    ))}
                  </select>
                  <select
                    className="loads-search"
                    value={beamSection}
                    onChange={(e) => setBeamSection(e.target.value)}
                  >
                    <option value="ALL">All Sections</option>
                    {beamSections.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <select
                    className="loads-search"
                    value={beamStatus}
                    onChange={(e) => setBeamStatus(e.target.value)}
                  >
                    <option value="ALL">All Status</option>
                    <option value="PASS">PASS</option>
                    <option value="FAIL">FAIL</option>
                  </select>
                </div>
              </div>
              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>Member</th>
                      <th>Section</th>
                      <th>Floor</th>
                      <th>Mu(- / + / -) kNm</th>
                      <th>Vu (kN)</th>
                      <th>Top Start</th>
                      <th>Bot Mid</th>
                      <th>Top End</th>
                      <th>Stirrups (Supp / Mid)</th>
                      <th>DCR</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredBeams.map((b) => (
                      <tr key={b.member_id} onClick={() => setSelectedMemberId(b.member_id)}>
                        <td>
                          <code>M{b.member_id}</code>
                        </td>
                        <td>
                          <b>{b.section_id}</b> ({b.width_mm}×{b.depth_mm})
                        </td>
                        <td>{b.floor_level}</td>
                        <td>
                          {b.mu_neg_start_knm.toFixed(1)} / {b.mu_pos_mid_knm.toFixed(1)} /{" "}
                          {b.mu_neg_end_knm.toFixed(1)}
                        </td>
                        <td>{b.vu_max_kn.toFixed(1)}</td>
                        <td>
                          <b>{b.bars_top_start}</b>
                        </td>
                        <td>
                          <b>{b.bars_bot_mid}</b>
                        </td>
                        <td>
                          <b>{b.bars_top_end}</b>
                        </td>
                        <td>
                          <small>
                            {b.stirrups_support} | {b.stirrups_midspan}
                          </small>
                        </td>
                        <td style={{ color: dcrColor(b.governing_dcr, b.status) }}>
                          <b>{b.governing_dcr.toFixed(2)}</b>
                        </td>
                        <td>
                          <span
                            className={`design-status-pill ${b.status === "PASS" ? "pass" : "fail"}`}
                          >
                            {b.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {filteredBeams.length === 0 && (
                      <tr>
                        <td colSpan={11} className="loads-empty">
                          No beam design rows match the current filter.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB 4: COLUMN DESIGN SCHEDULE */}
          {activeTab === "columns" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>RC Column Schedule (Biaxial P-M Interaction &amp; SMRF Confinement)</strong>
                  <small>Checked across all LRFD combinations.</small>
                </div>
                <div className="geom-floor-tabs">
                  <select
                    className="loads-search"
                    value={colFloor}
                    onChange={(e) => setColFloor(e.target.value)}
                  >
                    <option value="ALL">All Floors</option>
                    {floorOptions.map((lv) => (
                      <option key={lv} value={lv}>
                        {lv}
                      </option>
                    ))}
                  </select>
                  <select
                    className="loads-search"
                    value={colSection}
                    onChange={(e) => setColSection(e.target.value)}
                  >
                    <option value="ALL">All Sections</option>
                    {colSections.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <select
                    className="loads-search"
                    value={colStatus}
                    onChange={(e) => setColStatus(e.target.value)}
                  >
                    <option value="ALL">All Status</option>
                    <option value="PASS">PASS</option>
                    <option value="FAIL">FAIL</option>
                  </select>
                </div>
              </div>
              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>Member</th>
                      <th>Section</th>
                      <th>Floor</th>
                      <th>Pu Max (kN)</th>
                      <th>Mux / Muy (kNm)</th>
                      <th>Main Bars</th>
                      <th>&rho; (%)</th>
                      <th>Ties (Conf / Mid)</th>
                      <th>P-M DCR</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredCols.map((c) => (
                      <tr key={c.member_id} onClick={() => setSelectedMemberId(c.member_id)}>
                        <td>
                          <code>M{c.member_id}</code>
                        </td>
                        <td>
                          <b>{c.section_id}</b> ({c.width_mm}×{c.depth_mm})
                        </td>
                        <td>{c.floor_level}</td>
                        <td>{c.pu_max_kn.toFixed(1)}</td>
                        <td>
                          {c.mux_max_knm.toFixed(1)} / {c.muy_max_knm.toFixed(1)}
                        </td>
                        <td>
                          <b>{c.main_bars}</b>
                        </td>
                        <td>{(c.rho_provided * 100).toFixed(2)}%</td>
                        <td>
                          <small>
                            {c.ties_confined} | {c.ties_mid}
                          </small>
                        </td>
                        <td style={{ color: dcrColor(c.governing_dcr, c.status) }}>
                          <b>{c.pm_dcr.toFixed(2)}</b>
                        </td>
                        <td>
                          <span
                            className={`design-status-pill ${c.status === "PASS" ? "pass" : "fail"}`}
                          >
                            {c.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {filteredCols.length === 0 && (
                      <tr>
                        <td colSpan={10} className="loads-empty">
                          No column design rows match the current filter.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB 5: FOOTING SCHEDULE */}
          {activeTab === "footings" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>Isolated Footing Schedule (Bearing, One-Way &amp; Punching Shear)</strong>
                  <small>
                    Grouped by identical B &times; L &times; t dimensions (qa ={" "}
                    {params.soil_bearing_qa_kpa} kPa).
                  </small>
                </div>
              </div>
              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>Tag</th>
                      <th>Node / Grid</th>
                      <th>Base Elev</th>
                      <th>P_serv (kN)</th>
                      <th>B &times; L &times; t</th>
                      <th>q_act / q_all (kPa)</th>
                      <th>Punching DCR</th>
                      <th>Bottom Bars</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(ad?.footing_designs || []).map((f) => (
                      <tr key={f.node_id}>
                        <td>
                          <b>{f.footing_tag}</b>
                        </td>
                        <td>
                          <code>N{f.node_id}</code> {f.grid_ref ? `(${f.grid_ref})` : ""}
                        </td>
                        <td>{f.base_elevation_m.toFixed(2)} m</td>
                        <td>{f.p_service_kn.toFixed(1)}</td>
                        <td>
                          <b>
                            {f.b_m.toFixed(2)} &times; {f.l_m.toFixed(2)} m &times;{" "}
                            {f.thickness_mm.toFixed(0)} mm
                          </b>
                        </td>
                        <td>
                          {f.q_actual_kpa.toFixed(1)} / {f.q_allow_kpa.toFixed(0)}
                        </td>
                        <td style={{ color: dcrColor(f.punching_shear_dcr, f.status) }}>
                          <b>{f.punching_shear_dcr.toFixed(2)}</b>
                        </td>
                        <td>
                          <b>{f.bottom_bars_x}</b>
                        </td>
                        <td>
                          <span
                            className={`design-status-pill ${f.status === "PASS" ? "pass" : "fail"}`}
                          >
                            {f.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {!(ad?.footing_designs || []).length && (
                      <tr>
                        <td colSpan={9} className="loads-empty">
                          Run Analysis &amp; RC Design to generate isolated footing sizing.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB 6: SMRF JOINTS (SCWB & JOINT SHEAR) */}
          {activeTab === "smrf" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>NSCP Sec. 418 SMRF Beam-Column Joint &amp; SCWB Verification</strong>
                  <small>
                    Strong-Column/Weak-Beam (&Sigma;Mnc / &Sigma;Mnb &ge; 1.20, Sec. 418.7.3) &amp;
                    Horizontal Joint Shear (Vu &le; &phi;Vn, Sec. 418.8.4) · {kpis.jointPass}/
                    {kpis.jointTotal} PASS
                  </small>
                </div>
              </div>
              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>Node</th>
                      <th>Level</th>
                      <th>Grid</th>
                      <th>Sum Mnc (kN·m)</th>
                      <th>Sum Mnb (kN·m)</th>
                      <th>SCWB Ratio &ge; 1.20</th>
                      <th>Joint Shear DCR</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(ad?.smrf_joint_checks || []).map((j) => {
                      const bothPass = j.scwb_status === "PASS" && j.joint_shear_status === "PASS";
                      return (
                        <tr key={j.joint_node_id}>
                          <td>
                            <code>N{j.joint_node_id}</code>
                          </td>
                          <td>{j.floor_level}</td>
                          <td>{j.grid_ref || "—"}</td>
                          <td>{j.sum_mnc_knm.toFixed(1)}</td>
                          <td>{j.sum_mnb_knm.toFixed(1)}</td>
                          <td
                            style={{
                              color: j.scwb_status === "PASS" ? "#10b981" : "#ef4444",
                            }}
                          >
                            <b>{j.scwb_ratio.toFixed(2)}</b>
                          </td>
                          <td
                            style={{
                              color: dcrColor(j.joint_shear_dcr, j.joint_shear_status),
                            }}
                            title={`Vu = ${j.joint_shear_vu_kn.toFixed(1)} kN / phi Vn = ${j.joint_shear_phi_vn_kn.toFixed(1)} kN`}
                          >
                            <b>{j.joint_shear_dcr.toFixed(2)}</b>
                          </td>
                          <td>
                            <span
                              className={`design-status-pill ${bothPass ? "pass" : "fail"}`}
                            >
                              {bothPass ? "PASS" : "FAIL"}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                    {!(ad?.smrf_joint_checks || []).length && (
                      <tr>
                        <td colSpan={8} className="loads-empty">
                          Run Analysis &amp; RC Design to evaluate elevated SMRF beam-column joints.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB 7: SLABS & STAIRS */}
          {activeTab === "slabs" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>2-Way / 1-Way RC Slab Schedule (ACI 318 Coefficient Method)</strong>
                    <small>Auto-generated for all active slab panels on every Module 06 run.</small>
                  </div>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Panel</th>
                        <th>Floor</th>
                        <th>Lx × Ly (m)</th>
                        <th>t (mm)</th>
                        <th>Short Cont (-)</th>
                        <th>Short Disc (-)</th>
                        <th>Short Pos (+)</th>
                        <th>Long Neg (-)</th>
                        <th>Long Pos (+)</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ad?.slab_designs || []).map((s) => (
                        <tr key={`${s.floor_level}-${s.panel_id}`}>
                          <td><b>{s.panel_id}</b></td>
                          <td>{s.floor_level}</td>
                          <td>{s.lx_m.toFixed(2)} × {s.ly_m.toFixed(2)}</td>
                          <td>{s.thickness_mm.toFixed(0)}</td>
                          <td>{s.short_span_cont_neg}</td>
                          <td>{s.short_span_disc_neg}</td>
                          <td><b>{s.short_span_pos}</b></td>
                          <td>{s.long_span_neg}</td>
                          <td>{s.long_span_pos}</td>
                          <td>
                            <span className={`design-status-pill ${s.status === "PASS" ? "pass" : "fail"}`}>
                              {s.status}
                            </span>
                          </td>
                        </tr>
                      ))}
                      {!(ad?.slab_designs || []).length && (
                        <tr>
                          <td colSpan={10} className="loads-empty">
                            Run Analysis &amp; RC Design to auto-generate slab reinforcement schedules.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>Waist-Slab RC Stair Design (NSCP 2015)</strong>
                    <small>Add or update stair flights to design main/distribution rebar &amp; check L/20 deflection.</small>
                  </div>
                </div>
                <div className="geom-toolbar design-param-bar" style={{ margin: "10px 12px" }}>
                  <label className="design-param-field">
                    <span>ID</span>
                    <input value={stairForm.stair_id} onChange={(e) => setStairForm({ ...stairForm, stair_id: e.target.value })} />
                  </label>
                  <label className="design-param-field">
                    <span>Span (m)</span>
                    <input type="number" step="0.1" value={stairForm.flight_span_m} onChange={(e) => setStairForm({ ...stairForm, flight_span_m: parseFloat(e.target.value) || 3.6 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Riser (mm)</span>
                    <input type="number" value={stairForm.riser_mm} onChange={(e) => setStairForm({ ...stairForm, riser_mm: parseFloat(e.target.value) || 175 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Tread (mm)</span>
                    <input type="number" value={stairForm.tread_mm} onChange={(e) => setStairForm({ ...stairForm, tread_mm: parseFloat(e.target.value) || 275 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Waist (mm)</span>
                    <input type="number" step="25" value={stairForm.waist_thickness_mm} onChange={(e) => setStairForm({ ...stairForm, waist_thickness_mm: parseFloat(e.target.value) || 150 })} />
                  </label>
                  <label className="design-param-field">
                    <span>LL (kPa)</span>
                    <input type="number" step="0.2" value={stairForm.live_load_kpa} onChange={(e) => setStairForm({ ...stairForm, live_load_kpa: parseFloat(e.target.value) || 4.8 })} />
                  </label>
                  <button
                    disabled={busy}
                    onClick={() => postSecondary("stairs", stairForm, `Designed RC stair ${stairForm.stair_id}.`)}
                  >
                    Design Stair
                  </button>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Span (m)</th>
                        <th>Riser/Tread</th>
                        <th>Waist (mm)</th>
                        <th>Mu (kN·m/m)</th>
                        <th>Main Bars</th>
                        <th>Dist Bars</th>
                        <th>Deflection</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ad?.stair_designs || []).map((st) => (
                        <tr key={st.stair_id}>
                          <td><b>{st.stair_id}</b></td>
                          <td>{st.flight_span_m.toFixed(2)}</td>
                          <td>{st.riser_mm.toFixed(0)} / {st.tread_mm.toFixed(0)}</td>
                          <td>{st.waist_thickness_mm.toFixed(0)}</td>
                          <td>{st.mu_knm_per_m.toFixed(2)}</td>
                          <td><b>{st.main_bars}</b></td>
                          <td>{st.dist_bars}</td>
                          <td>
                            <span className={`design-status-pill ${st.deflection_status === "PASS" ? "pass" : "fail"}`}>
                              {st.deflection_status}
                            </span>
                          </td>
                          <td>
                            <button
                              className="secondary mini"
                              disabled={busy}
                              onClick={() => deleteSecondary(`stairs/${encodeURIComponent(st.stair_id)}`, `Removed stair ${st.stair_id}.`)}
                            >
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* TAB 8: RETAINING WALLS */}
          {activeTab === "retwalls" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>NSCP Sec. 209 Basement / Cantilever Retaining Walls</strong>
                  <small>Rankine Ka / Jaky K0 + Mononobe-Okabe Kae seismic thrust, stability &amp; RC stem/base rebar.</small>
                </div>
              </div>
              <div className="geom-toolbar design-param-bar" style={{ margin: "10px 12px" }}>
                <label className="design-param-field">
                  <span>Wall ID</span>
                  <input value={rwForm.wall_id} onChange={(e) => setRwForm({ ...rwForm, wall_id: e.target.value })} />
                </label>
                <label className="design-param-field">
                  <span>Height H (m)</span>
                  <input type="number" step="0.1" value={rwForm.retained_height_m} onChange={(e) => setRwForm({ ...rwForm, retained_height_m: parseFloat(e.target.value) || 2.8 })} />
                </label>
                <label className="design-param-field">
                  <span>Length (m)</span>
                  <input type="number" step="0.5" value={rwForm.total_length_m} onChange={(e) => setRwForm({ ...rwForm, total_length_m: parseFloat(e.target.value) || 12.0 })} />
                </label>
                <label className="design-param-field">
                  <span>Stem (mm)</span>
                  <input type="number" step="25" value={rwForm.stem_thickness_mm} onChange={(e) => setRwForm({ ...rwForm, stem_thickness_mm: parseFloat(e.target.value) || 250 })} />
                </label>
                <label className="design-param-field">
                  <span>Base B (m)</span>
                  <input type="number" step="0.1" value={rwForm.base_width_m} onChange={(e) => setRwForm({ ...rwForm, base_width_m: parseFloat(e.target.value) || 1.8 })} />
                </label>
                <label className="design-param-field">
                  <span>Phi (deg)</span>
                  <input type="number" step="1" value={rwForm.friction_angle_deg} onChange={(e) => setRwForm({ ...rwForm, friction_angle_deg: parseFloat(e.target.value) || 30 })} />
                </label>
                <label className="design-param-field">
                  <span>Surcharge (kPa)</span>
                  <input type="number" step="1" value={rwForm.surcharge_kpa} onChange={(e) => setRwForm({ ...rwForm, surcharge_kpa: parseFloat(e.target.value) || 10 })} />
                </label>
                <label className="pdf-check">
                  <input type="checkbox" checked={rwForm.is_restrained_top} onChange={(e) => setRwForm({ ...rwForm, is_restrained_top: e.target.checked })} />
                  Restrained Top
                </label>
                <button
                  disabled={busy}
                  onClick={() => postSecondary("retaining-walls", rwForm, `Designed retaining wall ${rwForm.wall_id}.`)}
                >
                  Design Wall
                </button>
              </div>
              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>Wall</th>
                      <th>H × L (m)</th>
                      <th>Ka / K0 / Kae</th>
                      <th>Stem Mu (kN·m/m)</th>
                      <th>FS OT / Slide</th>
                      <th>q_toe (kPa)</th>
                      <th>Stem Vert</th>
                      <th>Base Bars</th>
                      <th>Status</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(ad?.retaining_wall_designs || []).map((r) => (
                      <tr key={r.wall_id}>
                        <td><b>{r.wall_id}</b></td>
                        <td>{r.retained_height_m.toFixed(2)} × {r.total_length_m.toFixed(1)}</td>
                        <td>{r.ka.toFixed(2)} / {r.k0.toFixed(2)} / {r.k_ae_seismic.toFixed(2)}</td>
                        <td>{r.stem_mu_knm_per_m.toFixed(1)}</td>
                        <td>{r.fs_overturning.toFixed(2)} / {r.fs_sliding.toFixed(2)}</td>
                        <td>{r.max_toe_bearing_kpa.toFixed(1)}</td>
                        <td><b>{r.stem_vertical_bars}</b></td>
                        <td>{r.base_heel_toe_bars}</td>
                        <td>
                          <span className={`design-status-pill ${r.status === "PASS" ? "pass" : "fail"}`}>
                            {r.status}
                          </span>
                        </td>
                        <td>
                          <button
                            className="secondary mini"
                            disabled={busy}
                            onClick={() => deleteSecondary(`retaining-walls/${encodeURIComponent(r.wall_id)}`, `Removed wall ${r.wall_id}.`)}
                          >
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* TAB 9: ROOF TRUSSES & STRAP FOOTINGS */}
          {activeTab === "trusses" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>Steel Roof Truss Design (NSCP Ch. 5 / Double-Angle Catalog)</strong>
                    <small>Designs top chord, bottom chord, web members, DCRs, and structural steel tonnage for Module 08 BOM.</small>
                  </div>
                </div>
                <div className="geom-toolbar design-param-bar" style={{ margin: "10px 12px" }}>
                  <label className="design-param-field">
                    <span>ID</span>
                    <input value={trussForm.truss_id} onChange={(e) => setTrussForm({ ...trussForm, truss_id: e.target.value })} />
                  </label>
                  <label className="design-param-field">
                    <span>Span (m)</span>
                    <input type="number" step="0.5" value={trussForm.span_m} onChange={(e) => setTrussForm({ ...trussForm, span_m: parseFloat(e.target.value) || 8.0 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Spacing (m)</span>
                    <input type="number" step="0.25" value={trussForm.truss_spacing_m} onChange={(e) => setTrussForm({ ...trussForm, truss_spacing_m: parseFloat(e.target.value) || 3.0 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Pitch (deg)</span>
                    <input type="number" step="1" value={trussForm.pitch_deg} onChange={(e) => setTrussForm({ ...trussForm, pitch_deg: parseFloat(e.target.value) || 18 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Type</span>
                    <select value={trussForm.truss_type} onChange={(e) => setTrussForm({ ...trussForm, truss_type: e.target.value })}>
                      <option value="Pratt">Pratt</option>
                      <option value="Howe">Howe</option>
                      <option value="Fink">Fink</option>
                    </select>
                  </label>
                  <button
                    disabled={busy}
                    onClick={() => postSecondary("roof-trusses", trussForm, `Designed steel roof truss ${trussForm.truss_id}.`)}
                  >
                    Design Truss
                  </button>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Type / Span</th>
                        <th>Top Chord</th>
                        <th>Bottom Chord</th>
                        <th>Webs</th>
                        <th>Chord / Web DCR</th>
                        <th>Est. Steel (kg)</th>
                        <th>Status</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ad?.roof_truss_designs || []).map((t) => (
                        <tr key={t.truss_id}>
                          <td><b>{t.truss_id}</b></td>
                          <td>{t.truss_type} · {t.span_m.toFixed(1)}m @ {t.pitch_deg.toFixed(0)}°</td>
                          <td><b>{t.top_chord_section}</b></td>
                          <td>{t.bottom_chord_section}</td>
                          <td>{t.web_section}</td>
                          <td>{t.max_chord_dcr.toFixed(2)} / {t.max_web_dcr.toFixed(2)}</td>
                          <td>{t.estimated_steel_kg.toFixed(1)}</td>
                          <td>
                            <span className={`design-status-pill ${t.status === "PASS" ? "pass" : "fail"}`}>
                              {t.status}
                            </span>
                          </td>
                          <td>
                            <button
                              className="secondary mini"
                              disabled={busy}
                              onClick={() => deleteSecondary(`roof-trusses/${encodeURIComponent(t.truss_id)}`, `Removed truss ${t.truss_id}.`)}
                            >
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>Eccentric Property-Line Strap Footings</strong>
                    <small>Connects an eccentric exterior support node to an interior support node via a rigid strap beam.</small>
                  </div>
                </div>
                <div className="geom-toolbar design-param-bar" style={{ margin: "10px 12px" }}>
                  <label className="design-param-field">
                    <span>Strap ID</span>
                    <input value={strapForm.strap_id} onChange={(e) => setStrapForm({ ...strapForm, strap_id: e.target.value })} />
                  </label>
                  <label className="design-param-field">
                    <span>Exterior Node</span>
                    <input type="number" min="1" value={strapForm.exterior_node_id} onChange={(e) => setStrapForm({ ...strapForm, exterior_node_id: parseInt(e.target.value, 10) || 1 })} />
                  </label>
                  <label className="design-param-field">
                    <span>Interior Node</span>
                    <input type="number" min="1" value={strapForm.interior_node_id} onChange={(e) => setStrapForm({ ...strapForm, interior_node_id: parseInt(e.target.value, 10) || 2 })} />
                  </label>
                  <button
                    disabled={busy}
                    onClick={() => postSecondary("strap-footings", strapForm, `Designed strap footing ${strapForm.strap_id}.`)}
                  >
                    Design Strap Footing
                  </button>
                </div>
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Nodes (Ext → Int)</th>
                        <th>Ext Pad (m)</th>
                        <th>Int Pad (m)</th>
                        <th>Strap Beam (mm)</th>
                        <th>Top / Bot Bars</th>
                        <th>Stirrups</th>
                        <th>Status</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(ad?.strap_footing_designs || []).map((sf) => (
                        <tr key={sf.strap_id}>
                          <td><b>{sf.strap_id}</b></td>
                          <td>N{sf.exterior_node_id} → N{sf.interior_node_id}</td>
                          <td>{sf.ext_pad_b_m.toFixed(2)} × {sf.ext_pad_l_m.toFixed(2)}</td>
                          <td>{sf.int_pad_b_m.toFixed(2)} × {sf.int_pad_l_m.toFixed(2)}</td>
                          <td>{sf.strap_b_mm.toFixed(0)} × {sf.strap_h_mm.toFixed(0)}</td>
                          <td><b>{sf.strap_top_bars}</b> / {sf.strap_bot_bars}</td>
                          <td>{sf.strap_stirrups}</td>
                          <td>
                            <span className={`design-status-pill ${sf.status === "PASS" ? "pass" : "fail"}`}>
                              {sf.status}
                            </span>
                          </td>
                          <td>
                            <button
                              className="secondary mini"
                              disabled={busy}
                              onClick={() => deleteSecondary(`strap-footings/${encodeURIComponent(sf.strap_id)}`, `Removed strap ${sf.strap_id}.`)}
                            >
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Right Sidebar */}
        <aside className="geom-sidebar">
          <div className="geom-panel">
            <div className="geom-panel-head">
              <div>
                <strong>Design Health Summary</strong>
                <small>
                  {ad?.generated_at
                    ? `Source: ${ad.source === "staad_anl" ? `STAAD .anl (${ad.source_filename})` : "Internal 3D DSM"}`
                    : "Not yet analyzed"}
                </small>
              </div>
            </div>
            <div style={{ padding: "10px 12px" }}>
              <div className="geom-stats-grid loads-kpi-sidebar">
                <div>
                  <strong>
                    {kpis.beamPass}/{kpis.beamTotal}
                  </strong>
                  <small>Beams Pass</small>
                </div>
                <div>
                  <strong>
                    {kpis.colPass}/{kpis.colTotal}
                  </strong>
                  <small>Cols Pass</small>
                </div>
                <div>
                  <strong>
                    {kpis.ftgPass}/{kpis.ftgTotal}
                  </strong>
                  <small>Footings Pass</small>
                </div>
                <div>
                  <strong style={{ color: dcrColor(kpis.maxDrift, kpis.maxDrift > 1 ? "FAIL" : "PASS") }}>
                    {ad?.generated_at ? `${(kpis.maxDrift * 100).toFixed(0)}%` : "—"}
                  </strong>
                  <small>Max Drift Ratio</small>
                </div>
                <div>
                  <strong style={{ color: dcrColor(kpis.maxBeamDcr, kpis.maxBeamDcr > 1 ? "FAIL" : "PASS") }}>
                    {ad?.generated_at ? kpis.maxBeamDcr.toFixed(2) : "—"}
                  </strong>
                  <small>Max Beam DCR</small>
                </div>
                <div>
                  <strong style={{ color: dcrColor(kpis.maxColDcr, kpis.maxColDcr > 1 ? "FAIL" : "PASS") }}>
                    {ad?.generated_at ? kpis.maxColDcr.toFixed(2) : "—"}
                  </strong>
                  <small>Max Col DCR</small>
                </div>
              </div>
            </div>
          </div>

          <div className="geom-panel">
            <div className="geom-panel-head">
              <div className="geom-checks-head" style={{ width: "100%" }}>
                <h4>NSCP 2015 Design Review</h4>
                <small>
                  {errorCount} fail · {warningCount} warn
                </small>
              </div>
            </div>
            <div
              style={{
                padding: "10px 12px",
                display: "flex",
                flexDirection: "column",
                gap: "8px",
              }}
            >
              {validationIssues.length === 0 ? (
                <div className="geom-issue">
                  <div className="geom-issue-top">
                    <span className="geom-sev-badge sev-info">PASS</span>
                    <code>OK</code>
                  </div>
                  <p>
                    {ad?.generated_at
                      ? "All drift, beam, column, and footing checks passed NSCP 2015 limits. Ready for engineer sign-off."
                      : "Run Analysis & RC Design or upload a STAAD.Pro .anl output file to evaluate member utilization."}
                  </p>
                </div>
              ) : (
                validationIssues.map((iss, idx) => (
                  <div key={`${iss.code}-${idx}`} className={`geom-issue sev-${iss.severity}`}>
                    <div className="geom-issue-top">
                      <span className={`geom-sev-badge sev-${iss.severity}`}>
                        {iss.severity === "error" ? "FAIL" : iss.severity.toUpperCase()}
                      </span>
                      <code>{iss.code}</code>
                    </div>
                    <p>{iss.message}</p>
                  </div>
                ))
              )}
            </div>
          </div>
        </aside>
      </div>
    </section>
  );
}
