import React, { useEffect, useMemo, useRef, useState } from "react";

const MEMBER_COLORS = {
  column: "#38bdf8",
  primary_beam: "#45c9a8",
  secondary_beam: "#f59e0b",
  tie_beam: "#f59e0b",
  trimmer_beam: "#fb923c",
};

const OPENING_COLORS = {
  stairwell: "#f59e0b",
  airwell: "#38bdf8",
  shaft_other: "#c084fc",
};

function project3D(pt, center, yaw, pitch, scale, width, height) {
  const dx = pt.x - center.x;
  const dy = pt.y - center.y;
  const dz = pt.z - center.z;

  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);

  // Rotate around Y-up axis (yaw)
  const x1 = dx * cy - dz * sy;
  const z1 = dx * sy + dz * cy;
  // Rotate around X axis (pitch)
  const y2 = dy * cp - z1 * sp;
  const z2 = dy * sp + z1 * cp;

  // Gentle perspective foreshortening
  const camDist = 65;
  const persp = camDist / Math.max(camDist - z2, 12);

  return {
    sx: width * 0.5 + x1 * scale * persp,
    sy: height * 0.54 - y2 * scale * persp,
    depth: z2,
  };
}

export default function GeometryBuilder({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);
  const canvasRef = useRef(null);
  const orbitRef = useRef({ dragging: false, lastX: 0, lastY: 0 });

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [stdPreview, setStdPreview] = useState(null);

  // Generation options
  const [supportType, setSupportType] = useState("FIXED");
  const [includeBaseTies, setIncludeBaseTies] = useState(true);
  const [skipOpenings, setSkipOpenings] = useState(true);
  const [snapTol, setSnapTol] = useState("0.05");
  const [footingDepth, setFootingDepth] = useState("0.00");

  // Viewports & Interactive Editor state
  const [viewLayout, setViewLayout] = useState("split"); // split | 3d | 2d
  const [storyFilter, setStoryFilter] = useState("ALL");
  const [activeFloor, setActiveFloor] = useState("");
  const [editorMode, setEditorMode] = useState("inspect"); // inspect | add_beam | delete_member | toggle_support
  const [addBeamType, setAddBeamType] = useState("primary_beam");
  const [replicateAllFloors, setReplicateAllFloors] = useState(false);
  const [beamStartNodeId, setBeamStartNodeId] = useState(null);
  const [selectedEntity, setSelectedEntity] = useState(null);

  // 3D Orbit camera state
  const [yaw, setYaw] = useState(-0.68);
  const [pitch, setPitch] = useState(0.46);
  const [zoom3d, setZoom3d] = useState(1.0);

  async function apiJson(url, options) {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  async function loadProject() {
    try {
      const data = await apiJson(`/api/projects/${pid}`);
      setFullProject(data);
      const sm = data.structural_model;
      if (sm?.options) {
        setSupportType(sm.options.default_support_type || "FIXED");
        setIncludeBaseTies(sm.options.include_base_tie_beams ?? true);
        setSkipOpenings(sm.options.skip_beams_through_openings ?? true);
        setSnapTol(String(sm.options.snap_tolerance_m ?? 0.05));
        setFootingDepth(Number(sm.options.footing_depth_m ?? 0).toFixed(2));
      }
      const sortedLvls = (data.levels || []).slice().sort((a, b) => a.elevation_m - b.elevation_m);
      if (sortedLvls.length > 0) {
        setActiveFloor((prev) =>
          sortedLvls.some((l) => l.id === prev)
            ? prev
            : (sortedLvls[1]?.id || sortedLvls[0].id)
        );
      }
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    loadProject();
  }, [project.project_id]);

  const model = fullProject?.structural_model || {
    nodes: [],
    members: [],
    slab_panels: [],
    validation_issues: [],
    engineer_approved: false,
  };

  const sortedLevels = useMemo(
    () => (fullProject?.levels || []).slice().sort((a, b) => a.elevation_m - b.elevation_m),
    [fullProject?.levels]
  );

  const nodeMap = useMemo(() => {
    const m = new Map();
    for (const n of model.nodes || []) m.set(n.node_id, n);
    return m;
  }, [model.nodes]);

  const stats = useMemo(() => {
    const nodes = model.nodes || [];
    const members = model.members || [];
    const panels = model.slab_panels || [];
    const issues = model.validation_issues || [];
    return {
      nodes: nodes.length,
      columns: members.filter((m) => m.member_type === "column").length,
      beams: members.filter((m) => m.member_type !== "column").length,
      panels: panels.length,
      supports: nodes.filter((n) => n.is_support && n.support_type !== "NONE").length,
      errors: issues.filter((i) => i.severity === "error").length,
      warnings: issues.filter((i) => i.severity === "warning").length,
    };
  }, [model]);

  async function applyModelUpdate(updatedModel, msg = "") {
    setFullProject((prev) => (prev ? { ...prev, structural_model: updatedModel } : prev));
    if (msg) setNotice(msg);
    if (onUpdated) onUpdated(updatedModel);
  }

  async function handleGenerate() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/geometry/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          snap_tolerance_m: Math.max(0.005, parseFloat(snapTol) || 0.05),
          default_support_type: supportType,
          include_base_tie_beams: includeBaseTies,
          skip_beams_through_openings: skipOpenings,
          auto_detect_slab_panels: true,
          footing_depth_m: Math.min(6.0, Math.max(0.0, parseFloat(footingDepth) || 0.0)),
        }),
      });
      await applyModelUpdate(
        updated,
        `Generated 3D frame: ${updated.nodes.length} nodes, ${updated.members.length} members, ${updated.slab_panels.length} slab panel(s).`
      );
      setBeamStartNodeId(null);
      setStdPreview(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleValidate() {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/geometry/validate`, { method: "POST" });
      await applyModelUpdate(
        updated,
        `Integrity check complete: ${updated.validation_issues.filter((i) => i.severity === "error").length} error(s), ${updated.validation_issues.filter((i) => i.severity === "warning").length} warning(s).`
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApprove() {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/geometry/approve`, { method: "POST" });
      await applyModelUpdate(updated, "Analytical structural geometry approved by engineer.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleStaadPreview() {
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(`/api/projects/${pid}/staad-preview`, { method: "POST" });
      setStdPreview(res);
      setNotice(
        `STAAD.Pro preview written to ${res.path} (${res.nodes} nodes, ${res.members} members).`
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleAddBeam(startNid, endNid) {
    setBusy(true);
    setError("");
    const prevCount = (model.members || []).length;
    try {
      const updated = await apiJson(`/api/projects/${pid}/geometry/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_node: startNid,
          end_node: endNid,
          member_type: addBeamType,
          floor_level: activeFloor,
          replicate_all_floors: replicateAllFloors,
        }),
      });
      const addedCount = Math.max(1, (updated.members || []).length - prevCount);
      await applyModelUpdate(
        updated,
        replicateAllFloors && addedCount > 1
          ? `Added ${addBeamType} N${startNid}→N${endNid} and replicated across ${addedCount} matching floor(s).`
          : `Added ${addBeamType} from N${startNid} to N${endNid}.`
      );
      setBeamStartNodeId(null);
      setStdPreview(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteMember(memberId) {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/geometry/members/${memberId}`, {
        method: "DELETE",
      });
      await applyModelUpdate(updated, `Deleted member M${memberId}.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleSupport(node) {
    const nextSupport =
      node.support_type === "NONE"
        ? "FIXED"
        : node.support_type === "FIXED"
          ? "PINNED"
          : "NONE";
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/geometry/nodes/${node.node_id}/support`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ support_type: nextSupport }),
        }
      );
      await applyModelUpdate(updated, `Node N${node.node_id} support set to ${nextSupport}.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // Draw 3D HTML5 Canvas Wireframe
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || (viewLayout !== "split" && viewLayout !== "3d")) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width, 320);
    const h = Math.max(rect.height, 320);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#070c14";
    ctx.fillRect(0, 0, w, h);

    const nodes = model.nodes || [];
    const members = model.members || [];
    const openings = fullProject?.openings || [];

    if (nodes.length === 0) {
      ctx.fillStyle = "#6e829e";
      ctx.font = "13px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Click 'Generate 3D Frame' to build the analytical model.", w / 2, h / 2);
      return;
    }

    const xs = nodes.map((n) => n.x_m);
    const ys = nodes.map((n) => n.y_m);
    const zs = nodes.map((n) => n.z_m);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const minZ = Math.min(...zs);
    const maxZ = Math.max(...zs);

    const center = {
      x: (minX + maxX) * 0.5,
      y: (minY + maxY) * 0.5,
      z: (minZ + maxZ) * 0.5,
    };
    const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 4.0);
    const baseScale = (Math.min(w, h) * 0.62) / span;
    const scale = baseScale * zoom3d;

    const elevByLevel = new Map();
    for (const lv of sortedLevels) elevByLevel.set(lv.id.toUpperCase(), lv.elevation_m);

    // Subtle ground reference grid at minY
    ctx.strokeStyle = "rgba(56, 189, 248, 0.08)";
    ctx.lineWidth = 1;
    const gx0 = Math.floor(minX) - 1;
    const gx1 = Math.ceil(maxX) + 1;
    const gz0 = Math.floor(minZ) - 1;
    const gz1 = Math.ceil(maxZ) + 1;
    for (let gx = gx0; gx <= gx1; gx += 2) {
      const p1 = project3D({ x: gx, y: minY, z: gz0 }, center, yaw, pitch, scale, w, h);
      const p2 = project3D({ x: gx, y: minY, z: gz1 }, center, yaw, pitch, scale, w, h);
      ctx.beginPath();
      ctx.moveTo(p1.sx, p1.sy);
      ctx.lineTo(p2.sx, p2.sy);
      ctx.stroke();
    }
    for (let gz = gz0; gz <= gz1; gz += 2) {
      const p1 = project3D({ x: gx0, y: minY, z: gz }, center, yaw, pitch, scale, w, h);
      const p2 = project3D({ x: gx1, y: minY, z: gz }, center, yaw, pitch, scale, w, h);
      ctx.beginPath();
      ctx.moveTo(p1.sx, p1.sy);
      ctx.lineTo(p2.sx, p2.sy);
      ctx.stroke();
    }

    // Draw translucent floor opening polygons
    for (const op of openings) {
      if (storyFilter !== "ALL" && op.floor_level && op.floor_level !== storyFilter) continue;
      const yElev = elevByLevel.get((op.floor_level || "").toUpperCase());
      if (yElev === undefined || !op.polygon_m || op.polygon_m.length < 3) continue;
      const col = OPENING_COLORS[op.category] || "#c084fc";
      const projected = op.polygon_m.map(([px, pz]) =>
        project3D({ x: px, y: yElev, z: pz }, center, yaw, pitch, scale, w, h)
      );
      ctx.beginPath();
      projected.forEach((p, idx) => (idx === 0 ? ctx.moveTo(p.sx, p.sy) : ctx.lineTo(p.sx, p.sy)));
      ctx.closePath();
      ctx.fillStyle = col + "33";
      ctx.fill();
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }

    // Project & depth-sort members (never filter out manual/diagonal beams by member_type or story filter)
    const projectedMembers = [];
    const visibleManualNodeIds = new Set();
    for (const m of members) {
      if (storyFilter !== "ALL" && !m.manual_override && m.floor_level !== storyFilter) continue;
      const na = nodeMap.get(m.start_node);
      const nb = nodeMap.get(m.end_node);
      if (!na || !nb) continue;
      if (m.manual_override) {
        visibleManualNodeIds.add(na.node_id);
        visibleManualNodeIds.add(nb.node_id);
      }
      const pa = project3D({ x: na.x_m, y: na.y_m, z: na.z_m }, center, yaw, pitch, scale, w, h);
      const pb = project3D({ x: nb.x_m, y: nb.y_m, z: nb.z_m }, center, yaw, pitch, scale, w, h);
      projectedMembers.push({
        member: m,
        pa,
        pb,
        depth: (pa.depth + pb.depth) * 0.5,
      });
    }
    projectedMembers.sort((a, b) => a.depth - b.depth);

    for (const item of projectedMembers) {
      const { member, pa, pb } = item;
      ctx.strokeStyle = member.is_pedestal
        ? "#6366f1"
        : member.manual_override
          ? "#c084fc"
          : MEMBER_COLORS[member.member_type] || "#45c9a8";
      ctx.lineWidth = member.is_pedestal
        ? 3.4
        : member.manual_override
          ? 2.9
          : member.member_type === "column"
            ? 2.8
            : 2.1;
      ctx.beginPath();
      ctx.moveTo(pa.sx, pa.sy);
      ctx.lineTo(pb.sx, pb.sy);
      ctx.stroke();
    }

    // Draw nodes & support triangles
    for (const n of nodes) {
      const matchesStory =
        storyFilter === "ALL" ||
        n.floor_level === storyFilter ||
        n.floor_level === `FTG_${storyFilter}` ||
        visibleManualNodeIds.has(n.node_id);
      if (!matchesStory) {
        continue;
      }
      const p = project3D({ x: n.x_m, y: n.y_m, z: n.z_m }, center, yaw, pitch, scale, w, h);
      if (n.is_support && n.support_type !== "NONE") {
        ctx.fillStyle = "#f87171";
        ctx.strokeStyle = "#f87171";
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(p.sx, p.sy + 2);
        ctx.lineTo(p.sx - 6, p.sy + 12);
        ctx.lineTo(p.sx + 6, p.sy + 12);
        ctx.closePath();
        if (n.support_type === "FIXED") ctx.fill();
        else ctx.stroke();
      }
      ctx.fillStyle = n.is_support ? "#fca5a5" : "#eaf0f6";
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, 3.2, 0, Math.PI * 2);
      ctx.fill();
    }

    // Bottom-left 3D Axis Triad (X, Y-up, Z)
    const tx = 44;
    const ty = h - 42;
    const axes = [
      { vec: { x: 1, y: 0, z: 0 }, label: "X", color: "#f87171" },
      { vec: { x: 0, y: 1, z: 0 }, label: "Y (Up)", color: "#45c9a8" },
      { vec: { x: 0, y: 0, z: 1 }, label: "Z", color: "#38bdf8" },
    ];
    for (const ax of axes) {
      const originP = project3D({ x: 0, y: 0, z: 0 }, { x: 0, y: 0, z: 0 }, yaw, pitch, 26, tx * 2, ty * 2);
      const endP = project3D(ax.vec, { x: 0, y: 0, z: 0 }, yaw, pitch, 26, tx * 2, ty * 2);
      const ex = tx + (endP.sx - originP.sx);
      const ey = ty + (endP.sy - originP.sy);
      ctx.strokeStyle = ax.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(ex, ey);
      ctx.stroke();
      ctx.fillStyle = ax.color;
      ctx.font = "bold 10px Inter, system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(ax.label, ex + 3, ey + 3);
    }
  }, [model, fullProject?.openings, sortedLevels, nodeMap, yaw, pitch, zoom3d, storyFilter, viewLayout]);

  // 2D Floor Framing data for activeFloor
  const floorData = useMemo(() => {
    const nodes = (model.nodes || []).filter((n) => n.floor_level === activeFloor);
    const floorNodeIds = new Set(nodes.map((n) => n.node_id));
    const beams = (model.members || []).filter(
      (m) =>
        m.member_type !== "column" &&
        (m.floor_level === activeFloor ||
          (floorNodeIds.has(m.start_node) && floorNodeIds.has(m.end_node)))
    );
    const panels = (model.slab_panels || []).filter((p) => p.floor_level === activeFloor);
    const openings = (fullProject?.openings || []).filter(
      (o) => !o.floor_level || o.floor_level.toUpperCase() === (activeFloor || "").toUpperCase()
    );
    const allNodes = model.nodes || [];
    const xs = [...allNodes.map((n) => n.x_m), ...(fullProject?.grids?.x_grids || []).map((g) => g.coord_m)];
    const zs = [...allNodes.map((n) => n.z_m), ...(fullProject?.grids?.y_grids || []).map((g) => g.coord_m)];
    const minX = xs.length ? Math.min(...xs) : 0;
    const maxX = xs.length ? Math.max(...xs) : 10;
    const minZ = zs.length ? Math.min(...zs) : 0;
    const maxZ = zs.length ? Math.max(...zs) : 10;
    const pad = Math.max((maxX - minX) * 0.18, (maxZ - minZ) * 0.18, 1.5);
    return {
      nodes,
      beams,
      panels,
      openings,
      bounds: {
        minX: minX - pad,
        maxX: maxX + pad,
        minZ: minZ - pad,
        maxZ: maxZ + pad,
      },
    };
  }, [model, fullProject, activeFloor]);

  function toSvg2D(x_m, z_m) {
    const { minX, maxX, minZ, maxZ } = floorData.bounds;
    const W = 640;
    const H = 440;
    const spanX = Math.max(maxX - minX, 1);
    const spanZ = Math.max(maxZ - minZ, 1);
    const scale = Math.min((W - 60) / spanX, (H - 60) / spanZ);
    const ox = (W - spanX * scale) * 0.5;
    const oy = (H - spanZ * scale) * 0.5;
    return {
      x: ox + (x_m - minX) * scale,
      y: H - (oy + (z_m - minZ) * scale),
    };
  }

  function handleNode2DClick(node) {
    if (busy) return;
    if (editorMode === "toggle_support") {
      handleToggleSupport(node);
      return;
    }
    if (editorMode === "add_beam") {
      if (beamStartNodeId === null) {
        setBeamStartNodeId(node.node_id);
        setNotice(`Beam start: N${node.node_id}. Click destination node on ${activeFloor}.`);
      } else if (beamStartNodeId === node.node_id) {
        setBeamStartNodeId(null);
      } else {
        handleAddBeam(beamStartNodeId, node.node_id);
      }
      return;
    }
    setSelectedEntity({ type: "node", data: node });
  }

  function handleBeam2DClick(member) {
    if (busy) return;
    if (editorMode === "delete_member") {
      handleDeleteMember(member.member_id);
      return;
    }
    setSelectedEntity({ type: "member", data: member });
  }

  return (
    <section className="geom-builder">
      <div className="pdf-topbar">
        <div>
          <label>MODULE 03 · STRUCTURAL GEOMETRY GENERATION</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="pdf-topbar-actions">
          <button disabled={busy} onClick={handleGenerate}>
            Generate 3D Frame
          </button>
          <button
            className="secondary pdf-launch-btn"
            disabled={busy || stats.nodes === 0 || stats.errors > 0 || model.engineer_approved}
            onClick={handleApprove}
            title={
              stats.errors > 0
                ? "Resolve all error-level integrity checks before approving"
                : "Mark analytical geometry as engineer-approved"
            }
          >
            {model.engineer_approved ? "Geometry Approved ✓" : "Approve Geometry"}
          </button>
          <button className="secondary" disabled={busy} onClick={handleStaadPreview}>
            Preview STAAD.Pro (.std)
          </button>
          <button className="secondary" onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {error && <div className="pdf-alert error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {/* Top Generation Options & Layout Switcher */}
      <div className="geom-options-bar">
        <div className="pdf-inline-form">
          <label className="pdf-field-chip">
            <span>Support Type</span>
            <select
              className="pdf-select"
              value={supportType}
              onChange={(e) => setSupportType(e.target.value)}
            >
              <option value="FIXED">FIXED</option>
              <option value="PINNED">PINNED</option>
            </select>
          </label>
          <label className="pdf-check-chip">
            <input
              type="checkbox"
              checked={includeBaseTies}
              onChange={(e) => setIncludeBaseTies(e.target.checked)}
            />
            <span>Include Base Tie Beams</span>
          </label>
          <label className="pdf-check-chip">
            <input
              type="checkbox"
              checked={skipOpenings}
              onChange={(e) => setSkipOpenings(e.target.checked)}
            />
            <span>Skip Beams Through Openings</span>
          </label>
          <label className="pdf-field-chip">
            <span>Snap Tol (m)</span>
            <input
              type="number"
              step="0.01"
              min="0.01"
              max="0.5"
              value={snapTol}
              onChange={(e) => setSnapTol(e.target.value)}
            />
          </label>
          <label
            className="pdf-field-chip"
            title="Extends base columns downward to Top of Footing (e.g., 1.50 m) while keeping tie beams at grade"
          >
            <span>Footing Depth (m)</span>
            <input
              type="number"
              step="0.1"
              min="0"
              max="6"
              value={footingDepth}
              onChange={(e) => setFootingDepth(e.target.value)}
            />
          </label>
        </div>

        <div className="pdf-modes">
          <button
            className={`secondary ${viewLayout === "split" ? "active" : ""}`}
            onClick={() => setViewLayout("split")}
          >
            Split View
          </button>
          <button
            className={`secondary ${viewLayout === "3d" ? "active" : ""}`}
            onClick={() => setViewLayout("3d")}
          >
            3D Wireframe
          </button>
          <button
            className={`secondary ${viewLayout === "2d" ? "active" : ""}`}
            onClick={() => setViewLayout("2d")}
          >
            2D Floor Framing
          </button>
        </div>
      </div>

      {/* Main Split Workspace + Right Integrity Sidebar */}
      <div className="geom-workspace">
        <div className={`geom-viewports layout-${viewLayout}`}>
          {/* 3D Analytical Wireframe Viewport */}
          {(viewLayout === "split" || viewLayout === "3d") && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>3D Wireframe (STAAD Y-Up)</strong>
                  <small>Drag to orbit · Scroll to zoom</small>
                </div>
                <div className="pdf-inline-form">
                  <select
                    className="pdf-select"
                    value={storyFilter}
                    onChange={(e) => setStoryFilter(e.target.value)}
                    aria-label="Filter 3D story"
                  >
                    <option value="ALL">All Stories</option>
                    {sortedLevels.map((lv) => (
                      <option key={lv.id} value={lv.id}>
                        {lv.id} ({lv.elevation_m.toFixed(2)} m)
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => {
                      setYaw(-0.68);
                      setPitch(0.46);
                      setZoom3d(1.0);
                    }}
                  >
                    Reset
                  </button>
                </div>
              </div>
              <canvas
                ref={canvasRef}
                className="geom-canvas-3d"
                onMouseDown={(e) => {
                  orbitRef.current = { dragging: true, lastX: e.clientX, lastY: e.clientY };
                }}
                onMouseMove={(e) => {
                  if (!orbitRef.current.dragging) return;
                  const dx = e.clientX - orbitRef.current.lastX;
                  const dy = e.clientY - orbitRef.current.lastY;
                  orbitRef.current.lastX = e.clientX;
                  orbitRef.current.lastY = e.clientY;
                  setYaw((y) => y + dx * 0.01);
                  setPitch((p) => Math.max(-1.45, Math.min(1.45, p + dy * 0.01)));
                }}
                onMouseUp={() => {
                  orbitRef.current.dragging = false;
                }}
                onMouseLeave={() => {
                  orbitRef.current.dragging = false;
                }}
                onWheel={(e) => {
                  e.preventDefault();
                  const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
                  setZoom3d((z) => Math.min(4.0, Math.max(0.4, Number((z * f).toFixed(3)))));
                }}
              />
              <div className="geom-legend">
                <span>
                  <i style={{ background: "#38bdf8" }} /> Columns
                </span>
                <span>
                  <i style={{ background: "#45c9a8" }} /> Primary Beams
                </span>
                <span>
                  <i style={{ background: "#f59e0b" }} /> Tie / Secondary Beams
                </span>
                <span>
                  <i style={{ background: "#c084fc" }} /> Manual / Diagonal Beams
                </span>
                {((parseFloat(footingDepth) || 0) > 0 ||
                  (model.members || []).some((m) => m.is_pedestal)) && (
                  <span>
                    <i style={{ background: "#6366f1" }} /> Pedestals / Footing Drop
                  </span>
                )}
                <span>
                  <i style={{ background: "#f87171" }} /> Supports
                </span>
              </div>
            </div>
          )}

          {/* 2D Floor Framing Editor */}
          {(viewLayout === "split" || viewLayout === "2d") && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div className="geom-floor-tabs">
                  {sortedLevels.map((lv) => (
                    <button
                      key={lv.id}
                      type="button"
                      className={`mini-btn ${activeFloor === lv.id ? "on-approve" : ""}`}
                      onClick={() => {
                        setActiveFloor(lv.id);
                        setBeamStartNodeId(null);
                      }}
                    >
                      {lv.id} ({lv.elevation_m}m)
                    </button>
                  ))}
                </div>
                <div className="pdf-modes">
                  <button
                    className={`mini-btn ${editorMode === "inspect" ? "on-approve" : ""}`}
                    onClick={() => {
                      setEditorMode("inspect");
                      setBeamStartNodeId(null);
                    }}
                  >
                    Inspect
                  </button>
                  <button
                    className={`mini-btn ${editorMode === "add_beam" ? "on-approve" : ""}`}
                    onClick={() => {
                      setEditorMode("add_beam");
                      setBeamStartNodeId(null);
                    }}
                  >
                    Add Beam
                  </button>
                  <button
                    className={`mini-btn ${editorMode === "delete_member" ? "on-reject" : ""}`}
                    onClick={() => {
                      setEditorMode("delete_member");
                      setBeamStartNodeId(null);
                    }}
                  >
                    Delete Member
                  </button>
                  <button
                    className={`mini-btn ${editorMode === "toggle_support" ? "on-approve" : ""}`}
                    onClick={() => {
                      setEditorMode("toggle_support");
                      setBeamStartNodeId(null);
                    }}
                  >
                    Toggle Support
                  </button>
                </div>
              </div>

              {editorMode === "add_beam" && (
                <div className="geom-subbar">
                  <label className="pdf-field-chip">
                    <span>Beam Type</span>
                    <select
                      className="pdf-select"
                      value={addBeamType}
                      onChange={(e) => setAddBeamType(e.target.value)}
                    >
                      <option value="primary_beam">Primary Beam</option>
                      <option value="secondary_beam">Secondary Beam</option>
                      <option value="trimmer_beam">Trimmer Beam</option>
                      <option value="tie_beam">Tie Beam</option>
                    </select>
                  </label>
                  <label className="pdf-check-chip">
                    <input
                      type="checkbox"
                      checked={replicateAllFloors}
                      onChange={(e) => setReplicateAllFloors(e.target.checked)}
                    />
                    <span>Replicate to matching floors</span>
                  </label>
                  <small className="pdf-hint">
                    {beamStartNodeId
                      ? `Selected start N${beamStartNodeId} — click target node.`
                      : "Click start node, then click end node."}
                  </small>
                </div>
              )}

              <svg className="geom-svg-2d" viewBox="0 0 640 440">
                {/* Grid lines */}
                {(fullProject?.grids?.x_grids || []).map((g) => {
                  const p1 = toSvg2D(g.coord_m, floorData.bounds.minZ);
                  const p2 = toSvg2D(g.coord_m, floorData.bounds.maxZ);
                  return (
                    <g key={`xg-${g.label}`} opacity={0.45}>
                      <line
                        x1={p1.x}
                        y1={p1.y}
                        x2={p2.x}
                        y2={p2.y}
                        stroke="#475569"
                        strokeDasharray="4 4"
                      />
                      <text x={p2.x} y={16} textAnchor="middle" fill="#94a3b8" fontSize="10" fontWeight="700">
                        {g.label}
                      </text>
                    </g>
                  );
                })}
                {(fullProject?.grids?.y_grids || []).map((g) => {
                  const p1 = toSvg2D(floorData.bounds.minX, g.coord_m);
                  const p2 = toSvg2D(floorData.bounds.maxX, g.coord_m);
                  return (
                    <g key={`yg-${g.label}`} opacity={0.45}>
                      <line
                        x1={p1.x}
                        y1={p1.y}
                        x2={p2.x}
                        y2={p2.y}
                        stroke="#475569"
                        strokeDasharray="4 4"
                      />
                      <text x={14} y={p1.y + 3} fill="#94a3b8" fontSize="10" fontWeight="700">
                        {g.label}
                      </text>
                    </g>
                  );
                })}

                {/* Slab Panels */}
                {floorData.panels.map((sp) => {
                  const pts = sp.polygon_m.map(([x, z]) => toSvg2D(x, z));
                  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
                  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
                  const isVoid = sp.slab_action === "void";
                  return (
                    <g key={sp.panel_id}>
                      <polygon
                        points={pts.map((p) => `${p.x},${p.y}`).join(" ")}
                        fill={isVoid ? "#e06c75" : sp.slab_action === "one_way" ? "#38bdf8" : "#45c9a8"}
                        fillOpacity={isVoid ? 0.1 : 0.11}
                        stroke="#28405a"
                        strokeWidth="1"
                      />
                      <text x={cx} y={cy - 3} textAnchor="middle" fill="#9fb5cc" fontSize="10" fontWeight="800">
                        {sp.slab_action === "one_way" ? "1-WAY" : sp.slab_action === "two_way" ? "2-WAY" : "VOID"}
                      </text>
                      <text x={cx} y={cy + 10} textAnchor="middle" fill="#7c93ac" fontSize="9">
                        {sp.net_area_sqm.toFixed(1)} m²
                      </text>
                    </g>
                  );
                })}

                {/* Openings */}
                {floorData.openings.map((op) => {
                  const pts = op.polygon_m.map(([x, z]) => toSvg2D(x, z));
                  const col = OPENING_COLORS[op.category] || "#c084fc";
                  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
                  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
                  return (
                    <g key={op.id}>
                      <polygon
                        points={pts.map((p) => `${p.x},${p.y}`).join(" ")}
                        fill={col}
                        fillOpacity={0.28}
                        stroke={col}
                        strokeWidth="1.6"
                        strokeDasharray="4 3"
                      />
                      <text x={cx} y={cy + 3} textAnchor="middle" fill={col} fontSize="10" fontWeight="800">
                        {op.label}
                      </text>
                    </g>
                  );
                })}

                {/* Beams on Active Floor */}
                {floorData.beams.map((m) => {
                  const na = nodeMap.get(m.start_node);
                  const nb = nodeMap.get(m.end_node);
                  if (!na || !nb) return null;
                  const pa = toSvg2D(na.x_m, na.z_m);
                  const pb = toSvg2D(nb.x_m, nb.z_m);
                  const col = m.manual_override
                    ? "#c084fc"
                    : MEMBER_COLORS[m.member_type] || "#45c9a8";
                  const isSel = selectedEntity?.type === "member" && selectedEntity.data.member_id === m.member_id;
                  return (
                    <g
                      key={m.member_id}
                      className="geom-clickable"
                      onClick={() => handleBeam2DClick(m)}
                    >
                      <line x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y} stroke="transparent" strokeWidth="12" />
                      <line
                        x1={pa.x}
                        y1={pa.y}
                        x2={pb.x}
                        y2={pb.y}
                        stroke={editorMode === "delete_member" ? "#f87171" : col}
                        strokeWidth={isSel ? 4.5 : m.manual_override ? 3.3 : 2.8}
                      />
                      <text
                        x={(pa.x + pb.x) * 0.5}
                        y={(pa.y + pb.y) * 0.5 - 5}
                        textAnchor="middle"
                        fill="#cbd5e1"
                        fontSize="9"
                      >
                        {m.length_m.toFixed(2)}m
                      </text>
                    </g>
                  );
                })}

                {/* Nodes on Active Floor */}
                {floorData.nodes.map((n) => {
                  const p = toSvg2D(n.x_m, n.z_m);
                  const isStart = beamStartNodeId === n.node_id;
                  return (
                    <g
                      key={n.node_id}
                      className="geom-clickable"
                      onClick={() => handleNode2DClick(n)}
                    >
                      {n.is_support && n.support_type !== "NONE" && (
                        <polygon
                          points={`${p.x},${p.y + 3} ${p.x - 7},${p.y + 14} ${p.x + 7},${p.y + 14}`}
                          fill={n.support_type === "FIXED" ? "#f87171" : "none"}
                          stroke="#f87171"
                          strokeWidth="1.8"
                        />
                      )}
                      <circle
                        cx={p.x}
                        cy={p.y}
                        r={isStart ? 7 : 5.5}
                        fill={isStart ? "#45c9a8" : n.is_support ? "#fca5a5" : "#38bdf8"}
                        stroke="#0b1220"
                        strokeWidth="1.8"
                      />
                      <text x={p.x + 8} y={p.y - 6} fill="#eaf0f6" fontSize="9.5" fontWeight="700">
                        N{n.node_id}
                        {n.grid_ref ? ` (${n.grid_ref})` : ""}
                      </text>
                    </g>
                  );
                })}
              </svg>
            </div>
          )}
        </div>

        {/* Right Sidebar: Model Stats & Integrity Checks */}
        <aside className="pdf-review-col geom-sidebar">
          <div className="pdf-review-header">
            <div>
              <label>ANALYTICAL MODEL SUMMARY</label>
              <h3>
                {model.engineer_approved ? "Approved for STAAD Export" : "Pending Engineer Approval"}
              </h3>
            </div>
            <div className="geom-stats-grid">
              <div>
                <strong>{stats.nodes}</strong>
                <small>Nodes</small>
              </div>
              <div>
                <strong>{stats.columns}</strong>
                <small>Columns</small>
              </div>
              <div>
                <strong>{stats.beams}</strong>
                <small>Beams</small>
              </div>
              <div>
                <strong>{stats.panels}</strong>
                <small>Slab Panels</small>
              </div>
              <div>
                <strong>{stats.supports}</strong>
                <small>Supports</small>
              </div>
              <div>
                <strong>{stats.errors}</strong>
                <small>Errors</small>
              </div>
            </div>
          </div>

          {selectedEntity && (
            <div className="geom-selected-box">
              <strong>
                {selectedEntity.type === "node"
                  ? `Node N${selectedEntity.data.node_id} (${selectedEntity.data.floor_level})`
                  : `Member M${selectedEntity.data.member_id} (${selectedEntity.data.member_type})`}
              </strong>
              <small>
                {selectedEntity.type === "node"
                  ? `X=${selectedEntity.data.x_m}m · Y=${selectedEntity.data.y_m}m · Z=${selectedEntity.data.z_m}m · Support: ${selectedEntity.data.support_type}`
                  : `N${selectedEntity.data.start_node} → N${selectedEntity.data.end_node} · L=${selectedEntity.data.length_m}m`}
              </small>
            </div>
          )}

          <div className="geom-checks-head">
            <h4>Geometry Integrity Check ({(model.validation_issues || []).length})</h4>
            <button className="mini-btn" disabled={busy} onClick={handleValidate}>
              Re-Validate
            </button>
          </div>

          <div className="pdf-review-list">
            {(model.validation_issues || []).length === 0 ? (
              <p className="pdf-muted">
                {stats.nodes > 0
                  ? "No integrity issues detected."
                  : "Generate the 3D frame to run structural integrity checks."}
              </p>
            ) : (
              model.validation_issues.map((iss, idx) => (
                <div key={`${iss.code}-${idx}`} className={`geom-issue sev-${iss.severity}`}>
                  <div className="geom-issue-top">
                    <span className={`geom-sev-badge sev-${iss.severity}`}>
                      {iss.severity.toUpperCase()}
                    </span>
                    <code>{iss.code}</code>
                    {iss.floor_level && <small>{iss.floor_level}</small>}
                  </div>
                  <p>{iss.message}</p>
                </div>
              ))
            )}
          </div>
        </aside>
      </div>

      {/* Live STAAD.Pro (.std) Preview Drawer */}
      {stdPreview && (
        <div className="geom-std-drawer">
          <div className="geom-panel-head">
            <strong>STAAD.Pro Geometry Preview ({stdPreview.path})</strong>
            <div className="pdf-inline-form">
              <button
                type="button"
                className="mini-btn on-approve"
                onClick={() => navigator.clipboard?.writeText(stdPreview.std_preview || "")}
              >
                Copy .std
              </button>
              <button type="button" className="mini-btn" onClick={() => setStdPreview(null)}>
                Close
              </button>
            </div>
          </div>
          <pre className="geom-std-code">{stdPreview.std_preview}</pre>
        </div>
      )}
    </section>
  );
}
