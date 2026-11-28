import React, { useEffect, useMemo, useRef, useState } from "react";

const CATEGORY_OPTIONS = [
  { value: "column", label: "Column" },
  { value: "primary_beam", label: "Primary Beam" },
  { value: "secondary_beam", label: "Secondary Beam" },
  { value: "tie_beam", label: "Tie Beam" },
  { value: "trimmer_beam", label: "Trimmer Beam" },
];

const FC_PRESETS = ["21", "28", "35"];

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

export default function MemberSizing({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);
  const canvasRef = useRef(null);
  const orbitRef = useRef({ dragging: false, moved: false, lastX: 0, lastY: 0 });
  const projectedRef = useRef([]);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [stdPreview, setStdPreview] = useState(null);

  // Active center tab: viewer | schedule | slabs
  const [activeTab, setActiveTab] = useState("viewer");

  // Material & Design Basis form state
  const [fcPreset, setFcPreset] = useState("28");
  const [fcCustom, setFcCustom] = useState("28");
  const [fyMain, setFyMain] = useState("415");
  const [fyTie, setFyTie] = useState("275");
  const [smrfZone4, setSmrfZone4] = useState(true);
  const [crackedMods, setCrackedMods] = useState(true);
  const [pinSecondary, setPinSecondary] = useState(true);

  // 3D Viewer state
  const [storyFilter, setStoryFilter] = useState("ALL");
  const [highlightSectionId, setHighlightSectionId] = useState("");
  const [yaw, setYaw] = useState(-0.68);
  const [pitch, setPitch] = useState(0.46);
  const [zoom3d, setZoom3d] = useState(1.0);

  // Section Catalog Add/Edit state
  const [showAddSection, setShowAddSection] = useState(false);
  const [secForm, setSecForm] = useState({
    section_id: "",
    category: "column",
    width_mm: "400",
    depth_mm: "400",
    color_hex: "#38bdf8",
    cracked_ix: "0.70",
    cracked_iy: "0.70",
    cracked_iz: "0.70",
  });
  const [editingSectionId, setEditingSectionId] = useState(null);

  // Member Schedule filter & bulk assign state
  const [filterFloor, setFilterFloor] = useState("ALL");
  const [filterType, setFilterType] = useState("ALL");
  const [filterSection, setFilterSection] = useState("ALL");
  const [selectedMemberIds, setSelectedMemberIds] = useState([]);
  const [bulkSectionId, setBulkSectionId] = useState("");
  const [bulkBeta, setBulkBeta] = useState("KEEP"); // KEEP | 0 | 90
  const [bulkRelStart, setBulkRelStart] = useState("KEEP"); // KEEP | true | false
  const [bulkRelEnd, setBulkRelEnd] = useState("KEEP"); // KEEP | true | false

  // Slab & Retaining Wall inline edits
  const [slabDrafts, setSlabDrafts] = useState({});
  const [rwDrafts, setRwDrafts] = useState({});

  async function apiJson(url, options) {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  function syncFormsFromSizing(sz) {
    if (!sz) return;
    if (sz.material) {
      const fcStr = String(sz.material.fc_mpa);
      if (FC_PRESETS.includes(fcStr)) {
        setFcPreset(fcStr);
      } else {
        setFcPreset("custom");
      }
      setFcCustom(fcStr);
      setFyMain(String(sz.material.fy_main_mpa ?? 415));
      setFyTie(String(sz.material.fy_tie_mpa ?? 275));
    }
    if (sz.options) {
      setSmrfZone4(sz.options.seismic_smrf_zone4 ?? true);
      setCrackedMods(sz.options.include_cracked_modifiers ?? true);
      setPinSecondary(sz.options.pin_secondary_beams ?? true);
    }
    if (Array.isArray(sz.sections) && sz.sections.length > 0) {
      setBulkSectionId((prev) =>
        sz.sections.some((s) => s.section_id === prev) ? prev : sz.sections[0].section_id
      );
    }
  }

  async function loadProject() {
    try {
      const data = await apiJson(`/api/projects/${pid}`);
      setFullProject(data);
      syncFormsFromSizing(data.member_sizing);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    loadProject();
  }, [project.project_id]);

  const model = fullProject?.structural_model || { nodes: [], members: [], slab_panels: [] };
  const sizing = fullProject?.member_sizing || {
    material: {
      name: "CONCRETE",
      fc_mpa: 28,
      fy_main_mpa: 415,
      fy_tie_mpa: 275,
      ec_kn_m2: 24870062,
      poisson: 0.17,
      density_kn_m3: 23.54,
      alpha: 1e-5,
      damp: 0.05,
    },
    options: {
      seismic_smrf_zone4: true,
      include_cracked_modifiers: true,
      pin_secondary_beams: true,
      beam_span_depth_ratio: 12,
      round_increment_mm: 50,
    },
    sections: [],
    member_assignments: [],
    slab_assignments: [],
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

  const secMap = useMemo(() => {
    const m = new Map();
    for (const s of sizing.sections || []) m.set(s.section_id, s);
    return m;
  }, [sizing.sections]);

  const assignMap = useMemo(() => {
    const m = new Map();
    for (const a of sizing.member_assignments || []) m.set(a.member_id, a);
    return m;
  }, [sizing.member_assignments]);

  const panelMap = useMemo(() => {
    const m = new Map();
    for (const p of model.slab_panels || []) m.set(p.panel_id, p);
    return m;
  }, [model.slab_panels]);

  const sectionCounts = useMemo(() => {
    const counts = {};
    for (const a of sizing.member_assignments || []) {
      counts[a.section_id] = (counts[a.section_id] || 0) + 1;
    }
    return counts;
  }, [sizing.member_assignments]);

  const effectiveFc = useMemo(() => {
    const raw = fcPreset === "custom" ? parseFloat(fcCustom) : parseFloat(fcPreset);
    return Number.isFinite(raw) && raw > 0 ? raw : 28.0;
  }, [fcPreset, fcCustom]);

  const liveEcMpa = useMemo(() => Math.round(4700 * Math.sqrt(effectiveFc)), [effectiveFc]);

  // Assignment summary & estimated concrete volume (m^3)
  const summary = useMemo(() => {
    const members = model.members || [];
    let assigned = 0;
    let memberVol = 0;
    for (const m of members) {
      const a = assignMap.get(m.member_id);
      const sec = a ? secMap.get(a.section_id) : null;
      if (sec) {
        assigned += 1;
        memberVol += m.length_m * (sec.width_mm / 1000) * (sec.depth_mm / 1000);
      }
    }
    let slabVol = 0;
    for (const sa of sizing.slab_assignments || []) {
      const p = panelMap.get(sa.panel_id);
      if (p && sa.thickness_mm > 0 && p.slab_action !== "void") {
        slabVol += p.net_area_sqm * (sa.thickness_mm / 1000);
      }
    }
    const issues = sizing.validation_issues || [];
    return {
      totalMembers: members.length,
      assigned,
      unassigned: Math.max(0, members.length - assigned),
      sections: (sizing.sections || []).length,
      concreteVolM3: memberVol + slabVol,
      errors: issues.filter((i) => i.severity === "error").length,
      warnings: issues.filter((i) => i.severity === "warning").length,
    };
  }, [model.members, assignMap, secMap, sizing.slab_assignments, sizing.sections, sizing.validation_issues, panelMap]);

  const filteredMembers = useMemo(() => {
    return (model.members || []).filter((m) => {
      if (filterFloor !== "ALL" && m.floor_level !== filterFloor) return false;
      if (filterType !== "ALL" && m.member_type !== filterType) return false;
      const a = assignMap.get(m.member_id);
      const sid = a?.section_id || "__UNASSIGNED__";
      if (filterSection !== "ALL" && sid !== filterSection) return false;
      return true;
    });
  }, [model.members, filterFloor, filterType, filterSection, assignMap]);

  async function applySizingUpdate(updatedSizing, msg = "") {
    setFullProject((prev) => (prev ? { ...prev, member_sizing: updatedSizing } : prev));
    syncFormsFromSizing(updatedSizing);
    if (msg) setNotice(msg);
    setStdPreview(null);
    if (onUpdated) onUpdated(updatedSizing);
  }

  function buildMaterialPayload() {
    return {
      ...(sizing.material || {}),
      name: sizing.material?.name || "CONCRETE",
      fc_mpa: effectiveFc,
      fy_main_mpa: Math.max(100, parseFloat(fyMain) || 415),
      fy_tie_mpa: Math.max(100, parseFloat(fyTie) || 275),
      ec_kn_m2: Number((4700 * Math.sqrt(effectiveFc) * 1000).toFixed(2)),
    };
  }

  function buildOptionsPayload() {
    return {
      ...(sizing.options || {}),
      seismic_smrf_zone4: smrfZone4,
      include_cracked_modifiers: crackedMods,
      pin_secondary_beams: pinSecondary,
    };
  }

  async function handleAutoSize() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/sizing/auto-size`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          material: buildMaterialPayload(),
          options: buildOptionsPayload(),
          preserve_manual_overrides: false,
        }),
      });
      await applySizingUpdate(
        updated,
        `Auto-sized ${updated.sections.length} section(s) across ${updated.member_assignments.length} member(s) and ${updated.slab_assignments.length} slab panel(s).`
      );
      await loadProject();
      setRwDrafts({});
      setSelectedMemberIds([]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyMaterialBasis() {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/sizing/material`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          material: buildMaterialPayload(),
          options: buildOptionsPayload(),
        }),
      });
      await applySizingUpdate(
        updated,
        `Updated design basis: f'c=${updated.material.fc_mpa} MPa (Ec=${Math.round(updated.material.ec_kn_m2 / 1000)} MPa).`
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function startEditSection(sec) {
    setEditingSectionId(sec.section_id);
    setShowAddSection(true);
    setSecForm({
      section_id: sec.section_id,
      category: sec.category,
      width_mm: String(sec.width_mm),
      depth_mm: String(sec.depth_mm),
      color_hex: sec.color_hex || "#38bdf8",
      cracked_ix: String(sec.cracked_ix),
      cracked_iy: String(sec.cracked_iy),
      cracked_iz: String(sec.cracked_iz),
    });
  }

  function startNewSection() {
    setEditingSectionId(null);
    setShowAddSection(true);
    setSecForm({
      section_id: `S${(sizing.sections?.length || 0) + 1}`,
      category: "primary_beam",
      width_mm: "300",
      depth_mm: "500",
      color_hex: "#a78bfa",
      cracked_ix: "0.35",
      cracked_iy: "0.35",
      cracked_iz: "0.35",
    });
  }

  async function handleSaveSection(e) {
    e.preventDefault();
    const sid = secForm.section_id.trim().toUpperCase();
    const w = parseFloat(secForm.width_mm);
    const d = parseFloat(secForm.depth_mm);
    if (!sid || !Number.isFinite(w) || !Number.isFinite(d)) {
      setError("Enter valid section ID, width, and depth.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/sizing/sections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          section_id: sid,
          label: `${sid} (${Math.round(w)}x${Math.round(d)})`,
          category: secForm.category,
          width_mm: w,
          depth_mm: d,
          material_name: sizing.material?.name || "CONCRETE",
          color_hex: secForm.color_hex || "#38bdf8",
          cracked_ix: parseFloat(secForm.cracked_ix) || 0.7,
          cracked_iy: parseFloat(secForm.cracked_iy) || 0.7,
          cracked_iz: parseFloat(secForm.cracked_iz) || 0.7,
        }),
      });
      await applySizingUpdate(updated, `Saved section ${sid} (${Math.round(w)}×${Math.round(d)} mm).`);
      setShowAddSection(false);
      setEditingSectionId(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteSection(sectionId) {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/sizing/sections/${encodeURIComponent(sectionId)}`,
        { method: "DELETE" }
      );
      await applySizingUpdate(updated, `Deleted section ${sectionId}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleBulkAssign() {
    if (selectedMemberIds.length === 0 || !bulkSectionId) return;
    setBusy(true);
    setError("");
    try {
      const body = {
        member_ids: selectedMemberIds,
        section_id: bulkSectionId,
      };
      if (bulkBeta !== "KEEP") body.beta_angle_deg = parseFloat(bulkBeta);
      if (bulkRelStart !== "KEEP") body.release_start_moment = bulkRelStart === "true";
      if (bulkRelEnd !== "KEEP") body.release_end_moment = bulkRelEnd === "true";

      const updated = await apiJson(`/api/projects/${pid}/sizing/assign`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await applySizingUpdate(
        updated,
        `Assigned ${selectedMemberIds.length} member(s) to section ${bulkSectionId}.`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveSlab(sa) {
    const draft = slabDrafts[sa.panel_id];
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(
        `/api/projects/${pid}/sizing/slabs/${encodeURIComponent(sa.panel_id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slab_tag: draft.slab_tag,
            thickness_mm: parseFloat(draft.thickness_mm),
          }),
        }
      );
      await applySizingUpdate(updated, `Updated slab ${sa.panel_id} (${draft.thickness_mm} mm).`);
      setSlabDrafts((prev) => {
        const next = { ...prev };
        delete next[sa.panel_id];
        return next;
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveRetainingWall(rw) {
    const draft = rwDrafts[rw.wall_id];
    if (!draft) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        ...rw,
        retained_height_m: parseFloat(draft.retained_height_m),
        total_length_m: parseFloat(draft.total_length_m),
        stem_thickness_mm: parseFloat(draft.stem_thickness_mm),
        base_width_m: parseFloat(draft.base_width_m),
        toe_width_m: parseFloat(draft.toe_width_m),
        base_thickness_mm: parseFloat(draft.base_thickness_mm),
        friction_angle_deg: parseFloat(draft.friction_angle_deg),
        surcharge_kpa: parseFloat(draft.surcharge_kpa),
        allowable_bearing_kpa: parseFloat(draft.allowable_bearing_kpa),
        is_restrained_top: Boolean(draft.is_restrained_top),
      };
      const updatedAd = await apiJson(`/api/projects/${pid}/design/retaining-walls`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setFullProject((prev) => (prev ? { ...prev, analysis_design: updatedAd } : prev));
      setRwDrafts((prev) => {
        const next = { ...prev };
        delete next[rw.wall_id];
        return next;
      });
      setNotice(`Updated retaining wall ${rw.wall_id} preliminary properties and synced design.`);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleApprove() {
    setBusy(true);
    setError("");
    try {
      const updated = await apiJson(`/api/projects/${pid}/sizing/approve`, { method: "POST" });
      await applySizingUpdate(updated, "Member properties and preliminary sizing approved by engineer.");
    } catch (err) {
      setError(err.message);
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
        `STAAD.Pro file exported to ${res.path} (${res.nodes} nodes, ${res.members} members, source: ${res.source}).`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function toggleMemberSelection(mid) {
    setSelectedMemberIds((prev) =>
      prev.includes(mid) ? prev.filter((id) => id !== mid) : [...prev, mid]
    );
  }

  // Render 3D Wireframe colored by Section Tag
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || activeTab !== "viewer") return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width, 320);
    const h = Math.max(rect.height, 340);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#070c14";
    ctx.fillRect(0, 0, w, h);

    const nodes = model.nodes || [];
    const members = model.members || [];
    if (nodes.length === 0 || members.length === 0) {
      ctx.fillStyle = "#6e829e";
      ctx.font = "13px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Generate the 3D frame in Module 03 first.", w / 2, h / 2);
      projectedRef.current = [];
      return;
    }

    const xs = nodes.map((n) => n.x_m);
    const ys = nodes.map((n) => n.y_m);
    const zs = nodes.map((n) => n.z_m);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const minZ = Math.min(...zs), maxZ = Math.max(...zs);
    const center = { x: (minX + maxX) * 0.5, y: (minY + maxY) * 0.5, z: (minZ + maxZ) * 0.5 };
    const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 4.0);
    const scale = ((Math.min(w, h) * 0.62) / span) * zoom3d;

    const selectedSet = new Set(selectedMemberIds);
    const projected = [];

    for (const m of members) {
      if (storyFilter !== "ALL" && m.floor_level !== storyFilter) continue;
      const na = nodeMap.get(m.start_node);
      const nb = nodeMap.get(m.end_node);
      if (!na || !nb) continue;
      const pa = project3D({ x: na.x_m, y: na.y_m, z: na.z_m }, center, yaw, pitch, scale, w, h);
      const pb = project3D({ x: nb.x_m, y: nb.y_m, z: nb.z_m }, center, yaw, pitch, scale, w, h);
      const asgn = assignMap.get(m.member_id);
      const sec = asgn ? secMap.get(asgn.section_id) : null;
      projected.push({
        member: m,
        asgn,
        sec,
        pa,
        pb,
        depth: (pa.depth + pb.depth) * 0.5,
      });
    }
    projected.sort((a, b) => a.depth - b.depth);
    projectedRef.current = projected;

    for (const item of projected) {
      const { member, asgn, sec, pa, pb } = item;
      const isSelected = selectedSet.has(member.member_id);
      const isDimmed =
        highlightSectionId && (!asgn || asgn.section_id !== highlightSectionId) && !isSelected;

      ctx.strokeStyle = isSelected
        ? "#ffffff"
        : sec?.color_hex || "#64748b";
      ctx.globalAlpha = isDimmed ? 0.2 : 1.0;
      ctx.lineWidth = isSelected
        ? 4.2
        : member.member_type === "column"
          ? 3.0
          : 2.3;
      ctx.beginPath();
      ctx.moveTo(pa.sx, pa.sy);
      ctx.lineTo(pb.sx, pb.sy);
      ctx.stroke();

      // Draw small hollow circles near pinned beam ends
      if (asgn && (asgn.release_start_moment || asgn.release_end_moment)) {
        ctx.fillStyle = "#070c14";
        ctx.strokeStyle = "#f59e0b";
        ctx.lineWidth = 1.4;
        if (asgn.release_start_moment) {
          const rx = pa.sx + (pb.sx - pa.sx) * 0.12;
          const ry = pa.sy + (pb.sy - pa.sy) * 0.12;
          ctx.beginPath();
          ctx.arc(rx, ry, 3.0, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
        if (asgn.release_end_moment) {
          const rx = pb.sx + (pa.sx - pb.sx) * 0.12;
          const ry = pb.sy + (pa.sy - pb.sy) * 0.12;
          ctx.beginPath();
          ctx.arc(rx, ry, 3.0, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
      }
    }
    ctx.globalAlpha = 1.0;

    for (const n of nodes) {
      if (storyFilter !== "ALL" && n.floor_level !== storyFilter) continue;
      const p = project3D({ x: n.x_m, y: n.y_m, z: n.z_m }, center, yaw, pitch, scale, w, h);
      ctx.fillStyle = n.is_support ? "#fca5a5" : "#cbd5e1";
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }, [
    model,
    sizing,
    activeTab,
    nodeMap,
    secMap,
    assignMap,
    storyFilter,
    highlightSectionId,
    selectedMemberIds,
    yaw,
    pitch,
    zoom3d,
  ]);

  function handleCanvasClick(e) {
    if (orbitRef.current.moved) {
      orbitRef.current.moved = false;
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    let best = null;
    let bestDist = 10;
    for (const item of projectedRef.current) {
      const d = pointToSegmentDist(mx, my, item.pa.sx, item.pa.sy, item.pb.sx, item.pb.sy);
      if (d < bestDist) {
        bestDist = d;
        best = item;
      }
    }
    if (best) {
      toggleMemberSelection(best.member.member_id);
      setNotice(
        `Toggled M${best.member.member_id} (${best.member.member_type}, ${best.asgn?.section_id || "Unassigned"}).`
      );
    }
  }

  return (
    <section className="sizing-builder">
      <div className="pdf-topbar">
        <div>
          <label>MODULE 04 · PRELIMINARY MEMBER SIZING &amp; PROPERTIES</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="pdf-topbar-actions">
          <button disabled={busy || summary.totalMembers === 0} onClick={handleAutoSize}>
            Auto-Size Preliminary Sections
          </button>
          <button
            className="secondary pdf-launch-btn"
            disabled={
              busy ||
              summary.totalMembers === 0 ||
              summary.errors > 0 ||
              sizing.engineer_approved
            }
            onClick={handleApprove}
            title={
              summary.errors > 0
                ? "Resolve all error-level property checks before approving"
                : "Mark preliminary member sizing and properties as engineer-approved"
            }
          >
            {sizing.engineer_approved ? "Properties Approved ✓" : "Approve Properties"}
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

      {/* Design Basis & Concrete Material Bar */}
      <div className="geom-options-bar">
        <div className="pdf-inline-form">
          <label className="pdf-field-chip">
            <span>f&apos;c (MPa)</span>
            <select
              className="pdf-select pdf-compact-select"
              value={fcPreset}
              onChange={(e) => {
                setFcPreset(e.target.value);
                if (e.target.value !== "custom") setFcCustom(e.target.value);
              }}
            >
              <option value="21">21 MPa (3000 psi)</option>
              <option value="28">28 MPa (4000 psi)</option>
              <option value="35">35 MPa (5000 psi)</option>
              <option value="custom">Custom...</option>
            </select>
          </label>
          {fcPreset === "custom" && (
            <input
              type="number"
              className="pdf-select"
              style={{ width: 72 }}
              min="15"
              max="80"
              step="0.5"
              value={fcCustom}
              onChange={(e) => setFcCustom(e.target.value)}
              aria-label="Custom f'c (MPa)"
            />
          )}
          <span className="pdf-cursor-readout" title="4700 × sqrt(f'c)">
            Ec = {liveEcMpa.toLocaleString()} MPa
          </span>
          <label className="pdf-field-chip">
            <span>fy Main (MPa)</span>
            <input
              type="number"
              min="230"
              max="600"
              step="5"
              value={fyMain}
              onChange={(e) => setFyMain(e.target.value)}
            />
          </label>
          <label className="pdf-field-chip">
            <span>fy Tie</span>
            <input
              type="number"
              min="230"
              max="500"
              step="5"
              value={fyTie}
              onChange={(e) => setFyTie(e.target.value)}
            />
          </label>
          <label className="pdf-check-chip">
            <input
              type="checkbox"
              checked={smrfZone4}
              onChange={(e) => setSmrfZone4(e.target.checked)}
            />
            <span>NSCP 2015 SMRF (Zone 4)</span>
          </label>
          <label className="pdf-check-chip">
            <input
              type="checkbox"
              checked={crackedMods}
              onChange={(e) => setCrackedMods(e.target.checked)}
            />
            <span>Cracked Modifiers (0.70 / 0.35 Ig)</span>
          </label>
          <label className="pdf-check-chip">
            <input
              type="checkbox"
              checked={pinSecondary}
              onChange={(e) => setPinSecondary(e.target.checked)}
            />
            <span>Pin Secondary Beams</span>
          </label>
          <button
            type="button"
            className="mini-btn on-approve"
            disabled={busy}
            onClick={handleApplyMaterialBasis}
          >
            Apply Basis
          </button>
        </div>

        <div className="pdf-modes">
          <button
            className={`secondary ${activeTab === "viewer" ? "active" : ""}`}
            onClick={() => setActiveTab("viewer")}
          >
            3D &amp; Sections ({summary.sections})
          </button>
          <button
            className={`secondary ${activeTab === "schedule" ? "active" : ""}`}
            onClick={() => setActiveTab("schedule")}
          >
            Member Schedule ({summary.totalMembers})
          </button>
          <button
            className={`secondary ${activeTab === "slabs" ? "active" : ""}`}
            onClick={() => setActiveTab("slabs")}
          >
            Slabs ({(sizing.slab_assignments || []).length})
          </button>
          <button
            className={`secondary ${activeTab === "retwalls" ? "active" : ""}`}
            onClick={() => setActiveTab("retwalls")}
          >
            Retaining Walls ({(fullProject?.analysis_design?.retaining_wall_inputs || []).length})
          </button>
        </div>
      </div>

      {/* Bulk Assign Floating Strip when members are selected */}
      {selectedMemberIds.length > 0 && (
        <div className="sizing-bulk-bar">
          <strong>{selectedMemberIds.length} member(s) selected</strong>
          <div className="pdf-inline-form">
            <select
              className="pdf-select"
              value={bulkSectionId}
              onChange={(e) => setBulkSectionId(e.target.value)}
              aria-label="Bulk assign section"
            >
              {(sizing.sections || []).map((s) => (
                <option key={s.section_id} value={s.section_id}>
                  {s.label} ({s.category})
                </option>
              ))}
            </select>
            <select
              className="pdf-select"
              value={bulkBeta}
              onChange={(e) => setBulkBeta(e.target.value)}
              aria-label="Bulk beta angle"
            >
              <option value="KEEP">Beta: Keep</option>
              <option value="0">Beta: 0°</option>
              <option value="90">Beta: 90°</option>
            </select>
            <select
              className="pdf-select"
              value={bulkRelStart}
              onChange={(e) => setBulkRelStart(e.target.value)}
              aria-label="Start moment release"
            >
              <option value="KEEP">Start Rel: Keep</option>
              <option value="true">Start: Pinned (MY MZ)</option>
              <option value="false">Start: Fixed</option>
            </select>
            <select
              className="pdf-select"
              value={bulkRelEnd}
              onChange={(e) => setBulkRelEnd(e.target.value)}
              aria-label="End moment release"
            >
              <option value="KEEP">End Rel: Keep</option>
              <option value="true">End: Pinned (MY MZ)</option>
              <option value="false">End: Fixed</option>
            </select>
            <button disabled={busy || !bulkSectionId} onClick={handleBulkAssign}>
              Assign to Selected
            </button>
            <button
              type="button"
              className="mini-btn"
              onClick={() => setSelectedMemberIds([])}
            >
              Clear
            </button>
          </div>
        </div>
      )}

      {/* Main Workspace + Right Summary / NSCP Sidebar */}
      <div className="geom-workspace">
        <div className="sizing-main-col">
          {activeTab === "viewer" && (
            <div className="sizing-split">
              {/* 3D Color-Coded Viewer */}
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>3D Property Viewer (Colored by Section)</strong>
                    <small>Drag to orbit · Scroll to zoom · Click member to select</small>
                  </div>
                  <div className="pdf-inline-form">
                    <select
                      className="pdf-select"
                      value={storyFilter}
                      onChange={(e) => setStoryFilter(e.target.value)}
                    >
                      <option value="ALL">All Stories</option>
                      {sortedLevels.map((lv) => (
                        <option key={lv.id} value={lv.id}>
                          {lv.id} ({lv.elevation_m} m)
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
                        setHighlightSectionId("");
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
                    orbitRef.current = {
                      dragging: true,
                      moved: false,
                      lastX: e.clientX,
                      lastY: e.clientY,
                    };
                  }}
                  onMouseMove={(e) => {
                    if (!orbitRef.current.dragging) return;
                    const dx = e.clientX - orbitRef.current.lastX;
                    const dy = e.clientY - orbitRef.current.lastY;
                    if (Math.hypot(dx, dy) > 3) orbitRef.current.moved = true;
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
                  onClick={handleCanvasClick}
                  onWheel={(e) => {
                    e.preventDefault();
                    const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
                    setZoom3d((z) => Math.min(4.0, Math.max(0.4, Number((z * f).toFixed(3)))));
                  }}
                />
                <div className="geom-legend">
                  {(sizing.sections || []).map((s) => (
                    <span
                      key={s.section_id}
                      className={`sizing-legend-chip ${highlightSectionId === s.section_id ? "active" : ""}`}
                      onClick={() =>
                        setHighlightSectionId((prev) =>
                          prev === s.section_id ? "" : s.section_id
                        )
                      }
                      title="Click to highlight this section in 3D"
                    >
                      <i style={{ background: s.color_hex }} /> {s.label} ({sectionCounts[s.section_id] || 0})
                    </span>
                  ))}
                  {summary.unassigned > 0 && (
                    <span>
                      <i style={{ background: "#64748b" }} /> Unassigned ({summary.unassigned})
                    </span>
                  )}
                </div>
              </div>

              {/* Section Catalog Panel */}
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>Section Catalog ({(sizing.sections || []).length})</strong>
                    <small>Click a section to highlight in 3D or edit dimensions</small>
                  </div>
                  <button
                    type="button"
                    className="mini-btn on-approve"
                    disabled={busy}
                    onClick={startNewSection}
                  >
                    + Add Section
                  </button>
                </div>

                {showAddSection && (
                  <form className="sizing-sec-form" onSubmit={handleSaveSection}>
                    <div className="pdf-inline-form">
                      <label className="pdf-field-chip">
                        <span>Tag</span>
                        <input
                          type="text"
                          value={secForm.section_id}
                          disabled={Boolean(editingSectionId)}
                          onChange={(e) => setSecForm({ ...secForm, section_id: e.target.value })}
                          style={{ width: 64 }}
                          required
                        />
                      </label>
                      <label className="pdf-field-chip">
                        <span>Category</span>
                        <select
                          className="pdf-select pdf-compact-select"
                          value={secForm.category}
                          onChange={(e) => {
                            const cat = e.target.value;
                            const isCol = cat === "column";
                            setSecForm({
                              ...secForm,
                              category: cat,
                              cracked_ix: isCol ? "0.70" : "0.35",
                              cracked_iy: isCol ? "0.70" : "0.35",
                              cracked_iz: isCol ? "0.70" : "0.35",
                            });
                          }}
                        >
                          {CATEGORY_OPTIONS.map((c) => (
                            <option key={c.value} value={c.value}>
                              {c.label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="pdf-field-chip">
                        <span>b (mm)</span>
                        <input
                          type="number"
                          step="25"
                          value={secForm.width_mm}
                          onChange={(e) => setSecForm({ ...secForm, width_mm: e.target.value })}
                        />
                      </label>
                      <label className="pdf-field-chip">
                        <span>h (mm)</span>
                        <input
                          type="number"
                          step="25"
                          value={secForm.depth_mm}
                          onChange={(e) => setSecForm({ ...secForm, depth_mm: e.target.value })}
                        />
                      </label>
                      <label className="pdf-field-chip">
                        <span>Cracked I</span>
                        <input
                          type="number"
                          step="0.05"
                          min="0.1"
                          max="1.0"
                          value={secForm.cracked_iz}
                          onChange={(e) =>
                            setSecForm({
                              ...secForm,
                              cracked_ix: e.target.value,
                              cracked_iy: e.target.value,
                              cracked_iz: e.target.value,
                            })
                          }
                        />
                      </label>
                      <input
                        type="color"
                        value={secForm.color_hex}
                        onChange={(e) => setSecForm({ ...secForm, color_hex: e.target.value })}
                        title="Section display color"
                      />
                      <button type="submit" className="mini-btn on-approve" disabled={busy}>
                        Save
                      </button>
                      <button
                        type="button"
                        className="mini-btn"
                        onClick={() => {
                          setShowAddSection(false);
                          setEditingSectionId(null);
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                )}

                <div className="sizing-sec-list">
                  {(sizing.sections || []).length === 0 ? (
                    <p className="pdf-muted" style={{ padding: 14 }}>
                      No sections defined yet. Click <strong>Auto-Size Preliminary Sections</strong>{" "}
                      to generate NSCP 2015 concrete sections automatically.
                    </p>
                  ) : (
                    sizing.sections.map((sec) => {
                      const cnt = sectionCounts[sec.section_id] || 0;
                      const isHi = highlightSectionId === sec.section_id;
                      return (
                        <div
                          key={sec.section_id}
                          className={`sizing-sec-card ${isHi ? "active" : ""}`}
                          style={{ borderLeftColor: sec.color_hex }}
                          onClick={() =>
                            setHighlightSectionId((prev) =>
                              prev === sec.section_id ? "" : sec.section_id
                            )
                          }
                        >
                          <div className="sizing-sec-main">
                            <strong>
                              <span
                                className="sizing-swatch"
                                style={{ background: sec.color_hex }}
                              />
                              {sec.label}
                            </strong>
                            <small>
                              {sec.category.replace("_", " ")} · {cnt} member{cnt === 1 ? "" : "s"} ·{" "}
                              Icr ({sec.cracked_ix}/{sec.cracked_iy}/{sec.cracked_iz})
                            </small>
                          </div>
                          <div className="pdf-cand-actions">
                            <button
                              type="button"
                              className="mini-btn"
                              disabled={busy}
                              onClick={(ev) => {
                                ev.stopPropagation();
                                startEditSection(sec);
                              }}
                            >
                              Edit
                            </button>
                            <button
                              type="button"
                              className="mini-btn on-reject"
                              disabled={busy || cnt > 0}
                              title={
                                cnt > 0
                                  ? "Reassign members before deleting this section"
                                  : "Delete section"
                              }
                              onClick={(ev) => {
                                ev.stopPropagation();
                                handleDeleteSection(sec.section_id);
                              }}
                            >
                              ×
                            </button>
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            </div>
          )}

          {activeTab === "schedule" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div className="pdf-inline-form">
                  <select
                    className="pdf-select"
                    value={filterFloor}
                    onChange={(e) => setFilterFloor(e.target.value)}
                  >
                    <option value="ALL">All Floors</option>
                    {sortedLevels.map((lv) => (
                      <option key={lv.id} value={lv.id}>
                        {lv.id}
                      </option>
                    ))}
                  </select>
                  <select
                    className="pdf-select"
                    value={filterType}
                    onChange={(e) => setFilterType(e.target.value)}
                  >
                    <option value="ALL">All Types</option>
                    {CATEGORY_OPTIONS.map((c) => (
                      <option key={c.value} value={c.value}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <select
                    className="pdf-select"
                    value={filterSection}
                    onChange={(e) => setFilterSection(e.target.value)}
                  >
                    <option value="ALL">All Sections</option>
                    <option value="__UNASSIGNED__">Unassigned</option>
                    {(sizing.sections || []).map((s) => (
                      <option key={s.section_id} value={s.section_id}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="pdf-inline-form">
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => setSelectedMemberIds(filteredMembers.map((m) => m.member_id))}
                  >
                    Select Filtered ({filteredMembers.length})
                  </button>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => setSelectedMemberIds([])}
                  >
                    Clear
                  </button>
                </div>
              </div>

              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th style={{ width: 28 }}></th>
                      <th>ID</th>
                      <th>Type</th>
                      <th>Floor</th>
                      <th>Grid</th>
                      <th>L (m)</th>
                      <th>Section</th>
                      <th>b × h (mm)</th>
                      <th>Beta</th>
                      <th>Releases</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredMembers.map((m) => {
                      const a = assignMap.get(m.member_id);
                      const sec = a ? secMap.get(a.section_id) : null;
                      const isChecked = selectedMemberIds.includes(m.member_id);
                      const relText =
                        a?.release_start_moment || a?.release_end_moment
                          ? `${a.release_start_moment ? "S" : ""}${a.release_start_moment && a.release_end_moment ? "/" : ""}${a.release_end_moment ? "E" : ""} MY MZ`
                          : "Fixed";
                      return (
                        <tr
                          key={m.member_id}
                          className={isChecked ? "selected" : ""}
                          onClick={() => toggleMemberSelection(m.member_id)}
                        >
                          <td>
                            <input
                              type="checkbox"
                              checked={isChecked}
                              onChange={() => {}}
                            />
                          </td>
                          <td>
                            <strong>M{m.member_id}</strong>
                          </td>
                          <td>{m.member_type.replace("_", " ")}</td>
                          <td>{m.floor_level}</td>
                          <td>{m.grid_ref || "—"}</td>
                          <td>{m.length_m.toFixed(2)}</td>
                          <td>
                            {sec ? (
                              <span
                                className="sizing-tag-pill"
                                style={{ borderColor: sec.color_hex, color: sec.color_hex }}
                              >
                                {sec.section_id}
                              </span>
                            ) : (
                              <span className="pdf-muted">Unassigned</span>
                            )}
                          </td>
                          <td>{sec ? `${Math.round(sec.width_mm)} × ${Math.round(sec.depth_mm)}` : "—"}</td>
                          <td>{a ? `${a.beta_angle_deg}°` : "0°"}</td>
                          <td>{relText}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {activeTab === "slabs" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>Slab Panel Thickness Schedule</strong>
                  <small>NSCP 2015 preliminary minimum thickness vs. assigned slab thickness</small>
                </div>
              </div>
              <div className="sizing-table-wrap">
                {(sizing.slab_assignments || []).length === 0 ? (
                  <p className="pdf-muted" style={{ padding: 14 }}>
                    No slab panels found. Generate slab panels in Module 03 and run Auto-Size.
                  </p>
                ) : (
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Panel</th>
                        <th>Floor</th>
                        <th>lx × ly (m)</th>
                        <th>Action</th>
                        <th>NSCP Min t (mm)</th>
                        <th>Slab Tag</th>
                        <th>Assigned t (mm)</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {sizing.slab_assignments.map((sa) => {
                        const panel = panelMap.get(sa.panel_id);
                        const draft = slabDrafts[sa.panel_id] || {
                          slab_tag: sa.slab_tag,
                          thickness_mm: String(sa.thickness_mm),
                        };
                        const dirty =
                          draft.slab_tag !== sa.slab_tag ||
                          String(draft.thickness_mm) !== String(sa.thickness_mm);
                        const isVoid = panel?.slab_action === "void" || sa.slab_tag === "VOID";
                        return (
                          <tr key={sa.panel_id}>
                            <td>
                              <strong>{sa.panel_id}</strong>
                            </td>
                            <td>{sa.floor_level}</td>
                            <td>
                              {panel ? `${panel.lx_m.toFixed(2)} × ${panel.ly_m.toFixed(2)}` : "—"}
                            </td>
                            <td>{panel ? panel.slab_action.toUpperCase() : "—"}</td>
                            <td>{isVoid ? "—" : `${sa.min_code_thickness_mm.toFixed(1)} mm`}</td>
                            <td>
                              <input
                                type="text"
                                className="region-level-input"
                                style={{ width: 64 }}
                                value={draft.slab_tag}
                                disabled={busy || isVoid}
                                onChange={(e) =>
                                  setSlabDrafts((prev) => ({
                                    ...prev,
                                    [sa.panel_id]: { ...draft, slab_tag: e.target.value },
                                  }))
                                }
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 76 }}
                                step="5"
                                min="0"
                                value={draft.thickness_mm}
                                disabled={busy || isVoid}
                                onChange={(e) =>
                                  setSlabDrafts((prev) => ({
                                    ...prev,
                                    [sa.panel_id]: { ...draft, thickness_mm: e.target.value },
                                  }))
                                }
                              />
                            </td>
                            <td>
                              {dirty && (
                                <button
                                  type="button"
                                  className="mini-btn on-approve"
                                  disabled={busy}
                                  onClick={() => handleSaveSlab(sa)}
                                >
                                  Save
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}

          {activeTab === "retwalls" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>Retaining Wall Preliminary Properties</strong>
                  <small>
                    NSCP 2015 preliminary sizing (auto-sized from retained height H; edits sync with
                    Module 06 &amp; Module 08)
                  </small>
                </div>
              </div>
              <div className="sizing-table-wrap">
                {(fullProject?.analysis_design?.retaining_wall_inputs || []).length === 0 ? (
                  <p className="pdf-muted" style={{ padding: 14 }}>
                    No retaining walls defined yet. Place retaining walls in Module 03 or add them in
                    Module 06 (Tab 8).
                  </p>
                ) : (
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Wall ID</th>
                        <th>H (m)</th>
                        <th>L (m)</th>
                        <th>Stem (mm)</th>
                        <th>Base B (m)</th>
                        <th>Toe (m)</th>
                        <th>Base t (mm)</th>
                        <th>&phi; (&deg;)</th>
                        <th>Surch (kPa)</th>
                        <th>qa (kPa)</th>
                        <th>Top Restr.</th>
                        <th>Status</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {(fullProject?.analysis_design?.retaining_wall_inputs || []).map((rw) => {
                        const rwDesign = (
                          fullProject?.analysis_design?.retaining_wall_designs || []
                        ).find((d) => d.wall_id === rw.wall_id);
                        const draft = rwDrafts[rw.wall_id] || {
                          retained_height_m: String(rw.retained_height_m),
                          total_length_m: String(rw.total_length_m),
                          stem_thickness_mm: String(rw.stem_thickness_mm),
                          base_width_m: String(rw.base_width_m),
                          toe_width_m: String(rw.toe_width_m),
                          base_thickness_mm: String(rw.base_thickness_mm),
                          friction_angle_deg: String(rw.friction_angle_deg),
                          surcharge_kpa: String(rw.surcharge_kpa),
                          allowable_bearing_kpa: String(rw.allowable_bearing_kpa),
                          is_restrained_top: Boolean(rw.is_restrained_top),
                        };
                        const dirty =
                          String(draft.retained_height_m) !== String(rw.retained_height_m) ||
                          String(draft.total_length_m) !== String(rw.total_length_m) ||
                          String(draft.stem_thickness_mm) !== String(rw.stem_thickness_mm) ||
                          String(draft.base_width_m) !== String(rw.base_width_m) ||
                          String(draft.toe_width_m) !== String(rw.toe_width_m) ||
                          String(draft.base_thickness_mm) !== String(rw.base_thickness_mm) ||
                          String(draft.friction_angle_deg) !== String(rw.friction_angle_deg) ||
                          String(draft.surcharge_kpa) !== String(rw.surcharge_kpa) ||
                          String(draft.allowable_bearing_kpa) !== String(rw.allowable_bearing_kpa) ||
                          Boolean(draft.is_restrained_top) !== Boolean(rw.is_restrained_top);
                        const updateField = (key, val) =>
                          setRwDrafts((prev) => ({
                            ...prev,
                            [rw.wall_id]: { ...draft, [key]: val },
                          }));
                        return (
                          <tr key={rw.wall_id}>
                            <td>
                              <strong>{rw.wall_id}</strong>
                              {rw.floor_level ? (
                                <div className="pdf-muted" style={{ fontSize: 11 }}>
                                  {rw.floor_level}
                                </div>
                              ) : null}
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 64 }}
                                step="0.1"
                                min="0.5"
                                value={draft.retained_height_m}
                                disabled={busy}
                                onChange={(e) => updateField("retained_height_m", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 64 }}
                                step="0.5"
                                min="0.5"
                                value={draft.total_length_m}
                                disabled={busy}
                                onChange={(e) => updateField("total_length_m", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 68 }}
                                step="25"
                                min="150"
                                value={draft.stem_thickness_mm}
                                disabled={busy}
                                onChange={(e) => updateField("stem_thickness_mm", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 64 }}
                                step="0.1"
                                min="0.5"
                                value={draft.base_width_m}
                                disabled={busy}
                                onChange={(e) => updateField("base_width_m", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 60 }}
                                step="0.05"
                                min="0"
                                value={draft.toe_width_m}
                                disabled={busy}
                                onChange={(e) => updateField("toe_width_m", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 68 }}
                                step="25"
                                min="200"
                                value={draft.base_thickness_mm}
                                disabled={busy}
                                onChange={(e) => updateField("base_thickness_mm", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 56 }}
                                step="1"
                                min="15"
                                max="45"
                                value={draft.friction_angle_deg}
                                disabled={busy}
                                onChange={(e) => updateField("friction_angle_deg", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 60 }}
                                step="1"
                                min="0"
                                value={draft.surcharge_kpa}
                                disabled={busy}
                                onChange={(e) => updateField("surcharge_kpa", e.target.value)}
                              />
                            </td>
                            <td>
                              <input
                                type="number"
                                className="region-level-input"
                                style={{ width: 64 }}
                                step="10"
                                min="30"
                                value={draft.allowable_bearing_kpa}
                                disabled={busy}
                                onChange={(e) => updateField("allowable_bearing_kpa", e.target.value)}
                              />
                            </td>
                            <td style={{ textAlign: "center" }}>
                              <input
                                type="checkbox"
                                checked={Boolean(draft.is_restrained_top)}
                                disabled={busy}
                                onChange={(e) => updateField("is_restrained_top", e.target.checked)}
                              />
                            </td>
                            <td>
                              {rwDesign ? (
                                <span
                                  className={`status-pill ${rwDesign.status === "PASS" ? "pass" : "fail"}`}
                                >
                                  {rwDesign.status}
                                </span>
                              ) : (
                                <span className="pdf-muted">—</span>
                              )}
                            </td>
                            <td>
                              {dirty && (
                                <button
                                  type="button"
                                  className="mini-btn on-approve"
                                  disabled={busy}
                                  onClick={() => handleSaveRetainingWall(rw)}
                                >
                                  Save
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right Sidebar: Assignment Summary & NSCP 2015 Property Checks */}
        <aside className="pdf-review-col geom-sidebar">
          <div className="pdf-review-header">
            <div>
              <label>PROPERTY ASSIGNMENT SUMMARY</label>
              <h3>
                {sizing.engineer_approved ? "Approved for Analysis" : "Preliminary — Review Required"}
              </h3>
            </div>
            <div className="geom-stats-grid">
              <div>
                <strong>{summary.assigned}</strong>
                <small>Assigned</small>
              </div>
              <div>
                <strong>{summary.unassigned}</strong>
                <small>Unassigned</small>
              </div>
              <div>
                <strong>{summary.sections}</strong>
                <small>Sections</small>
              </div>
              <div>
                <strong>{summary.concreteVolM3.toFixed(1)}</strong>
                <small>Conc. m³</small>
              </div>
              <div>
                <strong>{summary.errors}</strong>
                <small>Errors</small>
              </div>
              <div>
                <strong>{summary.warnings}</strong>
                <small>Warnings</small>
              </div>
            </div>
          </div>

          <div className="geom-checks-head">
            <h4>NSCP 2015 Property &amp; Code Check ({(sizing.validation_issues || []).length})</h4>
          </div>

          <div className="pdf-review-list">
            {(sizing.validation_issues || []).length === 0 ? (
              <p className="pdf-muted">
                {summary.assigned > 0
                  ? "All preliminary NSCP 2015 dimensional and completeness checks passed."
                  : "Click Auto-Size Preliminary Sections to assign properties and run checks."}
              </p>
            ) : (
              sizing.validation_issues.map((iss, idx) => (
                <div key={`${iss.code}-${idx}`} className={`geom-issue sev-${iss.severity}`}>
                  <div className="geom-issue-top">
                    <span className={`geom-sev-badge sev-${iss.severity}`}>
                      {iss.severity.toUpperCase()}
                    </span>
                    <code>{iss.code}</code>
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
            <strong>STAAD.Pro Full Property Preview ({stdPreview.path})</strong>
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
