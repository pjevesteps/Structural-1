import React, { useEffect, useMemo, useRef, useState } from "react";
import Member3DDetailViewer from "./Member3DDetailViewer";

function statusColor(dcr, status) {
  if (status === "FAIL" || dcr > 1.0) return "#ef4444";
  if (dcr > 0.75) return "#f59e0b";
  return "#10b981";
}

function parseBars(callout, fallbackN = 4, fallbackD = 16) {
  const m = String(callout || "").match(/(\d+)\s*[-xX]\s*(\d+)/);
  if (!m) return { n: fallbackN, d: fallbackD };
  return { n: Math.max(2, parseInt(m[1], 10)), d: Math.max(10, parseInt(m[2], 10)) };
}

function project3D(pt, center, yaw, pitch, scale, width, height, panX = 0, panY = 0) {
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

  const camDist = 70;
  const persp = camDist / Math.max(camDist - z2, 12);
  return {
    sx: width * 0.5 + panX + x1 * scale * persp,
    sy: height * 0.54 + panY - y2 * scale * persp,
    depth: z2,
  };
}

function ptSegDist(px, py, x1, y1, x2, y2) {
  const vx = x2 - x1;
  const vy = y2 - y1;
  const l2 = vx * vx + vy * vy;
  if (l2 < 1e-6) return Math.hypot(px - x1, py - y1);
  const t = Math.max(0, Math.min(1, ((px - x1) * vx + (py - y1) * vy) / l2));
  return Math.hypot(px - (x1 + t * vx), py - (y1 + t * vy));
}

function pointInPolygon(px, py, pts) {
  if (!pts || pts.length < 3) return false;
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i].sx;
    const yi = pts[i].sy;
    const xj = pts[j].sx;
    const yj = pts[j].sy;
    const intersect =
      yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi + 1e-9) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function CrossSectionBlueprint({ entity, data }) {
  if (!entity || !data) return null;
  const W = 280;
  const H = 186;
  const cx = W * 0.5;
  const cy = H * 0.48;

  if (entity.type === "beam") {
    const bw = data.width_mm || 300;
    const bh = data.depth_mm || 500;
    const sc = Math.min(120 / bw, 112 / bh);
    const rw = bw * sc;
    const rh = bh * sc;
    const x0 = cx - rw * 0.5;
    const y0 = cy - rh * 0.5;
    const cov = Math.max(7, 40 * sc);
    const top = parseBars(data.bars_top_start, 3, 16);
    const bot = parseBars(data.bars_bot_mid, 3, 16);
    const rDot = Math.max(2.6, Math.min(4.8, (top.d * sc) * 0.5));
    const drawRow = (count, yPos) => {
      const n = Math.min(count, 8);
      const sx0 = x0 + cov + rDot + 1;
      const sx1 = x0 + rw - cov - rDot - 1;
      return Array.from({ length: n }, (_, i) => {
        const px = n === 1 ? cx : sx0 + (i / (n - 1)) * (sx1 - sx0);
        return <circle key={`${yPos}-${i}`} cx={px} cy={yPos} r={rDot} fill="#4ade80" />;
      });
    };
    return (
      <svg className="pdv-blueprint-svg" viewBox={`0 0 ${W} ${H}`} aria-label="Beam cross-section blueprint">
        <rect x={x0} y={y0} width={rw} height={rh} fill="#111c2d" stroke="#facc15" strokeWidth="1.8" />
        <rect x={x0 + cov} y={y0 + cov} width={rw - 2 * cov} height={rh - 2 * cov} rx="3" fill="none" stroke="#38bdf8" strokeWidth="1.2" />
        {/* 135-deg seismic hook */}
        <line x1={x0 + cov} y1={y0 + cov} x2={x0 + cov + 8} y2={y0 + cov + 8} stroke="#38bdf8" strokeWidth="1.2" />
        <line x1={x0 + cov + 3} y1={y0 + cov} x2={x0 + cov + 10} y2={y0 + cov + 6} stroke="#38bdf8" strokeWidth="1.2" />
        {drawRow(top.n, y0 + cov + rDot + 1.5)}
        {drawRow(bot.n, y0 + rh - cov - rDot - 1.5)}
        {/* Dimension ticks */}
        <line x1={x0} y1={y0 + rh + 14} x2={x0 + rw} y2={y0 + rh + 14} stroke="#64748b" strokeWidth="1" />
        <text x={cx} y={y0 + rh + 26} textAnchor="middle" fill="#94a3b8" fontSize="10">{bw.toFixed(0)} mm</text>
        <line x1={x0 - 14} y1={y0} x2={x0 - 14} y2={y0 + rh} stroke="#64748b" strokeWidth="1" />
        <text x={x0 - 19} y={cy + 3} textAnchor="end" fill="#94a3b8" fontSize="10">{bh.toFixed(0)}</text>
        <text x={x0 + rw + 8} y={y0 + cov + 6} fill="#4ade80" fontSize="9.5">Top: {data.bars_top_start}</text>
        <text x={x0 + rw + 8} y={y0 + rh - cov} fill="#4ade80" fontSize="9.5">Bot: {data.bars_bot_mid}</text>
        <text x={cx} y={14} textAnchor="middle" fill="#cbd5e1" fontSize="10" fontWeight="600">
          BEAM SECTION · {data.section_id} ({data.stirrups_support})
        </text>
      </svg>
    );
  }

  if (entity.type === "column") {
    const bw = data.width_mm || 400;
    const bh = data.depth_mm || 400;
    const sc = Math.min(115 / bw, 115 / bh);
    const rw = bw * sc;
    const rh = bh * sc;
    const x0 = cx - rw * 0.5;
    const y0 = cy - rh * 0.5;
    const cov = Math.max(8, 40 * sc);
    const bars = parseBars(data.main_bars, 8, 20);
    const rDot = Math.max(2.8, Math.min(4.6, (bars.d * sc) * 0.48));
    const ix0 = x0 + cov + rDot + 1;
    const ix1 = x0 + rw - cov - rDot - 1;
    const iy0 = y0 + cov + rDot + 1;
    const iy1 = y0 + rh - cov - rDot - 1;
    const pts = [
      [ix0, iy0], [ix1, iy0], [ix1, iy1], [ix0, iy1],
    ];
    if (bars.n >= 8) {
      pts.push([cx, iy0], [cx, iy1], [ix0, cy], [ix1, cy]);
    }
    if (bars.n >= 12) {
      const mx1 = ix0 + (ix1 - ix0) / 3;
      const mx2 = ix0 + (2 * (ix1 - ix0)) / 3;
      pts.push([mx1, iy0], [mx2, iy0], [mx1, iy1], [mx2, iy1]);
    }
    return (
      <svg className="pdv-blueprint-svg" viewBox={`0 0 ${W} ${H}`} aria-label="Column cross-section blueprint">
        <rect x={x0} y={y0} width={rw} height={rh} fill="#111c2d" stroke="#22d3ee" strokeWidth="1.8" />
        <rect x={x0 + cov} y={y0 + cov} width={rw - 2 * cov} height={rh - 2 * cov} rx="3" fill="none" stroke="#38bdf8" strokeWidth="1.2" />
        {/* 135-deg seismic hook */}
        <line x1={x0 + rw - cov} y1={y0 + cov} x2={x0 + rw - cov - 8} y2={y0 + cov + 8} stroke="#38bdf8" strokeWidth="1.2" />
        {pts.map(([px, py], i) => (
          <circle key={i} cx={px} cy={py} r={rDot} fill="#4ade80" />
        ))}
        <line x1={x0} y1={y0 + rh + 14} x2={x0 + rw} y2={y0 + rh + 14} stroke="#64748b" strokeWidth="1" />
        <text x={cx} y={y0 + rh + 26} textAnchor="middle" fill="#94a3b8" fontSize="10">{bw.toFixed(0)} × {bh.toFixed(0)} mm</text>
        <text x={cx} y={14} textAnchor="middle" fill="#cbd5e1" fontSize="10" fontWeight="600">
          COLUMN SECTION · {data.section_id} ({data.main_bars})
        </text>
      </svg>
    );
  }

  if (entity.type === "footing") {
    const fw = 156;
    const fh = 38;
    const x0 = cx - fw * 0.5;
    const y0 = cy + 8;
    const colW = 34;
    return (
      <svg className="pdv-blueprint-svg" viewBox={`0 0 ${W} ${H}`} aria-label="Footing section blueprint">
        <rect x={cx - colW * 0.5} y={y0 - 44} width={colW} height={44} fill="#111c2d" stroke="#22d3ee" strokeWidth="1.5" />
        <rect x={x0} y={y0} width={fw} height={fh} fill="#111c2d" stroke="#e879f9" strokeWidth="1.8" />
        {/* Bottom mat bars */}
        <line x1={x0 + 8} y1={y0 + fh - 9} x2={x0 + fw - 8} y2={y0 + fh - 9} stroke="#4ade80" strokeWidth="1.6" />
        {Array.from({ length: 7 }, (_, i) => (
          <circle key={i} cx={x0 + 16 + i * 20.5} cy={y0 + fh - 13} r="2.4" fill="#4ade80" />
        ))}
        <text x={cx} y={y0 + fh + 16} textAnchor="middle" fill="#94a3b8" fontSize="10">
          B×L = {data.b_m.toFixed(2)} × {data.l_m.toFixed(2)} m · t = {data.thickness_mm.toFixed(0)} mm
        </text>
        <text x={cx} y={14} textAnchor="middle" fill="#cbd5e1" fontSize="10" fontWeight="600">
          FOOTING {data.footing_tag} (N{data.node_id}) · {data.bottom_bars_x}
        </text>
      </svg>
    );
  }

  if (entity.type === "slab") {
    const isOneWay = data.slab_action === "one_way";
    const tMm = Number(data.thickness_mm) || 125;
    const lx = Number(data.lx_m) || 3.0;
    const ly = Number(data.ly_m) || 4.0;
    const sw = 196;
    const sh = Math.max(26, Math.min(48, tMm * 0.22));
    const x0 = cx - sw * 0.5;
    const y0 = cy - sh * 0.5 - 4;
    const suppW = 18;
    const suppH = 26;
    return (
      <svg className="pdv-blueprint-svg" viewBox={`0 0 ${W} ${H}`} aria-label="Slab section blueprint">
        {/* Support beams at ends */}
        <rect x={x0} y={y0 + sh} width={suppW} height={suppH} fill="#111c2d" stroke="#64748b" strokeWidth="1.2" />
        <rect x={x0 + sw - suppW} y={y0 + sh} width={suppW} height={suppH} fill="#111c2d" stroke="#64748b" strokeWidth="1.2" />
        {/* Slab concrete body */}
        <rect x={x0} y={y0} width={sw} height={sh} fill="#111c2d" stroke="#38bdf8" strokeWidth="1.8" />
        {/* Top negative bars at continuous (left) & discontinuous (right) supports */}
        <line x1={x0 + 5} y1={y0 + 7} x2={x0 + sw * 0.30} y2={y0 + 7} stroke="#facc15" strokeWidth="1.8" />
        <line x1={x0 + sw * 0.72} y1={y0 + 7} x2={x0 + sw - 5} y2={y0 + 7} stroke="#facc15" strokeWidth="1.8" />
        {/* Bottom main short-span positive bar */}
        <line x1={x0 + 6} y1={y0 + sh - 7} x2={x0 + sw - 6} y2={y0 + sh - 7} stroke="#4ade80" strokeWidth="2.0" />
        {/* Perpendicular long-span / distribution bar dots */}
        {Array.from({ length: isOneWay ? 6 : 8 }, (_, i) => (
          <circle
            key={i}
            cx={x0 + 18 + (i / (isOneWay ? 5 : 7)) * (sw - 36)}
            cy={y0 + sh - 11}
            r="2.1"
            fill="#38bdf8"
          />
        ))}
        {/* Dimension & bar labels */}
        <line x1={x0 - 12} y1={y0} x2={x0 - 12} y2={y0 + sh} stroke="#64748b" strokeWidth="1" />
        <text x={x0 - 16} y={y0 + sh * 0.5 + 3} textAnchor="end" fill="#94a3b8" fontSize="9.5">
          t={tMm.toFixed(0)}
        </text>
        <text x={cx} y={y0 + sh + 16} textAnchor="middle" fill="#94a3b8" fontSize="9.5">
          Lx × Ly = {lx.toFixed(2)} × {ly.toFixed(2)} m ({isOneWay ? "1-WAY" : "2-WAY"})
        </text>
        <text x={cx} y={y0 + sh + 31} textAnchor="middle" fill="#4ade80" fontSize="9.2">
          Short (+): {data.short_span_pos} · Long (+): {data.long_span_pos}
        </text>
        <text x={cx} y={y0 + sh + 44} textAnchor="middle" fill="#facc15" fontSize="9.0">
          Top (-): Cont {data.short_span_cont_neg} / Disc {data.short_span_disc_neg}
        </text>
        <text x={cx} y={14} textAnchor="middle" fill="#cbd5e1" fontSize="10" fontWeight="600">
          SLAB {data.panel_id} ({data.floor_level}) · {isOneWay ? "ONE-WAY" : "TWO-WAY"}
        </text>
      </svg>
    );
  }

  if (entity.type === "retaining_wall") {
    const H_m = Math.max(0.8, Number(data.retained_height_m) || 2.8);
    const B_m = Math.max(0.8, Number(data.base_width_m) || Math.max(1.2, 0.6 * H_m));
    const toe_m = Math.min(B_m * 0.6, Math.max(0.2, Number(data.toe_width_m) || 0.25 * B_m));
    const tStem_m = Math.max(0.15, (Number(data.stem_thickness_mm) || 250) / 1000.0);
    const tBase_m = Math.max(0.20, (Number(data.base_thickness_mm) || 300) / 1000.0);
    const totalH = H_m + tBase_m;
    const sc = Math.min(130 / B_m, 108 / totalH);
    const bwPx = B_m * sc;
    const bhPx = tBase_m * sc;
    const swPx = Math.max(8, tStem_m * sc);
    const shPx = H_m * sc;
    const toePx = toe_m * sc;
    const xBase = cx - bwPx * 0.5 - 12;
    const yBaseBot = cy + totalH * sc * 0.5 + 8;
    const yBaseTop = yBaseBot - bhPx;
    const xStem = xBase + toePx;
    const yStemTop = yBaseTop - shPx;

    return (
      <svg className="pdv-blueprint-svg" viewBox={`0 0 ${W} ${H}`} aria-label="Retaining wall section blueprint">
        {/* Backfill soil wedge (right/heel side) */}
        <polygon
          points={`${xStem + swPx},${yStemTop + 4} ${xBase + bwPx + 24},${yStemTop + 4} ${xBase + bwPx + 24},${yBaseTop} ${xStem + swPx},${yBaseTop}`}
          fill="rgba(245, 158, 11, 0.10)"
          stroke="rgba(245, 158, 11, 0.35)"
          strokeDasharray="3 2"
        />
        {/* Active/at-rest earth pressure triangle */}
        <polygon
          points={`${xBase + bwPx + 18},${yStemTop + 8} ${xBase + bwPx + 18},${yBaseTop} ${xStem + swPx + 6},${yBaseTop}`}
          fill="rgba(239, 68, 68, 0.18)"
          stroke="#f87171"
          strokeWidth="1"
        />
        {/* Base slab & stem concrete */}
        <rect x={xBase} y={yBaseTop} width={bwPx} height={bhPx} fill="#111c2d" stroke="#cbd5e1" strokeWidth="1.6" />
        <rect x={xStem} y={yStemTop} width={swPx} height={shPx} fill="#111c2d" stroke="#cbd5e1" strokeWidth="1.6" />
        {/* Restrained top indicator if basement wall */}
        {data.is_restrained_top && (
          <polygon
            points={`${xStem - 10},${yStemTop + 2} ${xStem},${yStemTop + 7} ${xStem - 10},${yStemTop + 12}`}
            fill="none"
            stroke="#38bdf8"
            strokeWidth="1.2"
          />
        )}
        {/* Stem vertical tension rebar (backfill face) + base mat rebar */}
        <line x1={xStem + swPx - 3} y1={yStemTop + 4} x2={xStem + swPx - 3} y2={yBaseBot - 4} stroke="#4ade80" strokeWidth="1.8" />
        <line x1={xStem + 3} y1={yStemTop + 4} x2={xStem + 3} y2={yBaseBot - 4} stroke="#38bdf8" strokeWidth="1.2" />
        <line x1={xBase + 4} y1={yBaseTop + 4} x2={xBase + bwPx - 4} y2={yBaseTop + 4} stroke="#facc15" strokeWidth="1.5" />
        <line x1={xBase + 4} y1={yBaseBot - 4} x2={xBase + bwPx - 4} y2={yBaseBot - 4} stroke="#4ade80" strokeWidth="1.5" />
        {/* Dimensions & bar callouts */}
        <line x1={xBase} y1={yBaseBot + 10} x2={xBase + bwPx} y2={yBaseBot + 10} stroke="#64748b" strokeWidth="1" />
        <text x={xBase + bwPx * 0.5} y={yBaseBot + 21} textAnchor="middle" fill="#94a3b8" fontSize="9.5">
          B={B_m.toFixed(2)}m (toe {toe_m.toFixed(2)}m) · tb={Math.round(tBase_m * 1000)}mm
        </text>
        <text x={xBase - 6} y={yStemTop + shPx * 0.5} textAnchor="end" fill="#94a3b8" fontSize="9.5">
          H={H_m.toFixed(2)}m
        </text>
        <text x={xBase - 6} y={yStemTop + shPx * 0.5 + 12} textAnchor="end" fill="#94a3b8" fontSize="9">
          ts={Math.round(tStem_m * 1000)}
        </text>
        <text x={cx} y={14} textAnchor="middle" fill="#cbd5e1" fontSize="10" fontWeight="600">
          RETAINING WALL {data.wall_id} · {data.stem_vertical_bars}
        </text>
      </svg>
    );
  }

  return null;
}

function DcrBar({ label, value, limit = 1.0, invert = false }) {
  const ratio = invert ? Math.min(1.5, limit / Math.max(value, 0.01)) : value;
  const pct = Math.min(100, Math.max(3, ratio * 100));
  const pass = invert ? value >= limit : value <= limit;
  const color = !pass ? "#ef4444" : ratio > 0.75 ? "#f59e0b" : "#10b981";
  return (
    <div className="pdv-dcr-row">
      <div className="pdv-dcr-labels">
        <span>{label}</span>
        <strong style={{ color }}>{value.toFixed(2)}</strong>
      </div>
      <div className="pdv-dcr-track">
        <div className="pdv-dcr-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

export default function PostDesignViewer({
  project,
  analysisDesign,
  selectedEntity,
  onSelectEntity,
  onDesignUpdated,
  onNotice,
  onError,
}) {
  const pid = encodeURIComponent(project.project_id);
  const canvasRef = useRef(null);
  const svg2dRef = useRef(null);
  const orbitRef = useRef({ dragging: false, moved: false, pan: false, lastX: 0, lastY: 0 });
  const hitListRef = useRef([]);

  const [collapsed, setCollapsed] = useState(false);
  const [layoutMode, setLayoutMode] = useState("split"); // split | 3d | 2d
  const [overlayMode, setOverlayMode] = useState("dcr"); // dcr | marks | forces | scwb
  const [showDeflected, setShowDeflected] = useState(false);
  const [deflectCase, setDeflectCase] = useState("EQX"); // EQX | EQZ | Wind X | Wind Z
  const [deflectAmp, setDeflectAmp] = useState(25);
  const [view2dMode, setView2dMode] = useState("foundation"); // foundation | floor:<id> | elevX:<label> | elevZ:<label>

  // 3D camera state
  const [yaw, setYaw] = useState(-0.68);
  const [pitch, setPitch] = useState(0.46);
  const [zoom3d, setZoom3d] = useState(1.0);
  const [pan3d, setPan3d] = useState({ x: 0, y: 0 });
  const [pulsePhase, setPulsePhase] = useState(0);

  // Override & pin state
  const [saving, setSaving] = useState(false);
  const [overrideForm, setOverrideForm] = useState({});
  const [detailMode, setDetailMode] = useState("2d"); // "2d" | "3d"
  const [modal3dOpen, setModal3dOpen] = useState(false);

  const sm = project?.structural_model;
  const ad = analysisDesign;

  const nodeById = useMemo(() => {
    const m = {};
    for (const n of sm?.nodes || []) m[n.node_id] = n;
    return m;
  }, [sm?.nodes]);

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

  const ftgByNid = useMemo(() => {
    const m = {};
    for (const f of ad?.footing_designs || []) m[f.node_id] = f;
    return m;
  }, [ad?.footing_designs]);

  const jointByNid = useMemo(() => {
    const m = {};
    for (const j of ad?.smrf_joint_checks || []) m[j.joint_node_id] = j;
    return m;
  }, [ad?.smrf_joint_checks]);

  const levelElevById = useMemo(() => {
    const m = {};
    for (const lv of project?.levels || []) m[lv.id] = lv.elevation_m;
    return m;
  }, [project?.levels]);

  const slabPanelByKey = useMemo(() => {
    const m = {};
    for (const p of sm?.slab_panels || []) {
      m[`${p.floor_level}::${p.panel_id}`] = p;
    }
    return m;
  }, [sm?.slab_panels]);

  const slabDesignByKey = useMemo(() => {
    const m = {};
    for (const s of ad?.slab_designs || []) {
      m[`${s.floor_level}::${s.panel_id}`] = s;
    }
    return m;
  }, [ad?.slab_designs]);

  const rwInputById = useMemo(() => {
    const m = {};
    for (const rw of ad?.retaining_wall_inputs || []) m[rw.wall_id] = rw;
    return m;
  }, [ad?.retaining_wall_inputs]);

  const rwDesignById = useMemo(() => {
    const m = {};
    for (const rw of ad?.retaining_wall_designs || []) m[rw.wall_id] = rw;
    return m;
  }, [ad?.retaining_wall_designs]);

  const clashNodeSet = useMemo(() => {
    const s = new Set();
    for (const cl of ad?.footing_clashes || []) {
      if (cl.node_a != null) s.add(cl.node_a);
      if (cl.node_b != null) s.add(cl.node_b);
    }
    return s;
  }, [ad?.footing_clashes]);

  // Animate pulsing highlight when footing clashes exist
  useEffect(() => {
    if (!clashNodeSet.size || collapsed) return;
    const id = setInterval(() => setPulsePhase((p) => (p + 1) % 20), 140);
    return () => clearInterval(id);
  }, [clashNodeSet.size, collapsed]);

  // Summary KPIs
  const summary = useMemo(() => {
    const beams = ad?.beam_designs || [];
    const cols = ad?.column_designs || [];
    const ftgs = ad?.footing_designs || [];
    const joints = ad?.smrf_joint_checks || [];
    const clashes = ad?.footing_clashes || [];
    const maxBeam = beams.reduce((a, b) => Math.max(a, b.governing_dcr), 0);
    const maxCol = cols.reduce((a, c) => Math.max(a, c.governing_dcr), 0);
    const maxFtg = ftgs.reduce((a, f) => Math.max(a, f.governing_dcr), 0);
    const scwbPass = joints.filter((j) => j.scwb_status === "PASS").length;
    return {
      maxBeam,
      maxCol,
      maxFtg,
      scwbPass,
      scwbTotal: joints.length,
      clashCount: clashes.length,
      clashes,
    };
  }, [ad]);

  // Selected record
  const selectedData = useMemo(() => {
    if (!selectedEntity) return null;
    if (selectedEntity.type === "beam") return beamByMid[selectedEntity.id] || null;
    if (selectedEntity.type === "column") return colByMid[selectedEntity.id] || null;
    if (selectedEntity.type === "footing") return ftgByNid[selectedEntity.id] || null;
    if (selectedEntity.type === "joint") return jointByNid[selectedEntity.id] || null;
    if (selectedEntity.type === "slab") {
      const key = `${selectedEntity.floor_level || ""}::${selectedEntity.id}`;
      const sd =
        slabDesignByKey[key] ||
        (ad?.slab_designs || []).find((s) => s.panel_id === selectedEntity.id) ||
        null;
      if (!sd) return null;
      const panel = slabPanelByKey[`${sd.floor_level}::${sd.panel_id}`] || null;
      const lx = Number(sd.lx_m || panel?.lx_m || 3.0);
      const ly = Number(sd.ly_m || panel?.ly_m || 4.0);
      const fallbackAction =
        Math.max(lx, ly) / Math.max(0.1, Math.min(lx, ly)) >= 2.0 ? "one_way" : "two_way";
      return {
        ...sd,
        slab_action: panel?.slab_action && panel.slab_action !== "void" ? panel.slab_action : fallbackAction,
        net_area_sqm: panel?.net_area_sqm ?? Number((lx * ly).toFixed(2)),
      };
    }
    if (selectedEntity.type === "retaining_wall") {
      const rwd = rwDesignById[selectedEntity.id] || null;
      if (!rwd) return null;
      const rwi = rwInputById[selectedEntity.id] || null;
      return {
        ...rwd,
        stem_thickness_mm: rwi?.stem_thickness_mm ?? 250,
        base_width_m: rwi?.base_width_m ?? Math.max(1.2, Number((0.6 * rwd.retained_height_m).toFixed(2))),
        toe_width_m: rwi?.toe_width_m ?? 0.45,
        base_thickness_mm: rwi?.base_thickness_mm ?? 300,
        allowable_bearing_kpa: rwi?.allowable_bearing_kpa ?? 150,
        is_restrained_top: rwi?.is_restrained_top ?? true,
        friction_angle_deg: rwi?.friction_angle_deg ?? 30,
        surcharge_kpa: rwi?.surcharge_kpa ?? 10,
      };
    }
    return null;
  }, [
    selectedEntity,
    beamByMid,
    colByMid,
    ftgByNid,
    jointByNid,
    slabDesignByKey,
    slabPanelByKey,
    rwDesignById,
    rwInputById,
    ad?.slab_designs,
  ]);

  // Sync quick override form when selection changes
  useEffect(() => {
    if (!selectedEntity || !selectedData) {
      setOverrideForm({});
      return;
    }
    if (selectedEntity.type === "beam") {
      setOverrideForm({
        depth_mm: selectedData.depth_mm,
        bars_top_start: selectedData.bars_top_start,
        bars_bot_mid: selectedData.bars_bot_mid,
        bars_top_end: selectedData.bars_top_end,
        stirrups_support: selectedData.stirrups_support,
      });
    } else if (selectedEntity.type === "column") {
      setOverrideForm({
        width_mm: selectedData.width_mm,
        depth_mm: selectedData.depth_mm,
        main_bars: selectedData.main_bars,
        ties_confined: selectedData.ties_confined,
      });
    } else if (selectedEntity.type === "footing") {
      setOverrideForm({
        b_m: selectedData.b_m,
        l_m: selectedData.l_m,
        thickness_mm: selectedData.thickness_mm,
        bottom_bars_x: selectedData.bottom_bars_x,
        bottom_bars_z: selectedData.bottom_bars_z,
      });
    } else {
      setOverrideForm({});
    }
  }, [selectedEntity, selectedData]);

  // Story drift displacement lookup for deflected shape overlay
  const dispByElev = useMemo(() => {
    const map = {};
    for (const d of ad?.story_drifts || []) {
      const k = d.elevation_m.toFixed(2);
      const scaleW = deflectCase.startsWith("Wind") ? 0.55 : 1.0;
      map[k] = {
        dx: deflectCase.endsWith("X") ? (d.elastic_disp_x_mm / 1000.0) * scaleW : 0,
        dz: deflectCase.endsWith("Z") ? (d.elastic_disp_z_mm / 1000.0) * scaleW : 0,
      };
    }
    return map;
  }, [ad?.story_drifts, deflectCase]);

  const sortedLevels = useMemo(
    () => [...(project?.levels || [])].sort((a, b) => a.elevation_m - b.elevation_m),
    [project?.levels]
  );

  // =========================================================================
  // 3D BIM CANVAS RENDERER
  // =========================================================================
  useEffect(() => {
    if (collapsed || layoutMode === "2d") return;
    const canvas = canvasRef.current;
    if (!canvas || !sm?.nodes?.length) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(300, rect.width);
    const h = Math.max(380, rect.height);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#090e17";
    ctx.fillRect(0, 0, w, h);

    // Subtle engineering grid backdrop
    ctx.strokeStyle = "rgba(51, 65, 85, 0.22)";
    ctx.lineWidth = 1;
    for (let gx = 0; gx < w; gx += 36) {
      ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, h); ctx.stroke();
    }
    for (let gy = 0; gy < h; gy += 36) {
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
    }

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const n of sm.nodes) {
      minX = Math.min(minX, n.x_m); maxX = Math.max(maxX, n.x_m);
      minY = Math.min(minY, n.y_m); maxY = Math.max(maxY, n.y_m);
      minZ = Math.min(minZ, n.z_m); maxZ = Math.max(maxZ, n.z_m);
    }
    const center = { x: (minX + maxX) * 0.5, y: (minY + maxY) * 0.5, z: (minZ + maxZ) * 0.5 };
    const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 4.0);
    const scale = (Math.min(w, h) * 0.58 * zoom3d) / span;

    const proj = (pt) => project3D(pt, center, yaw, pitch, scale, w, h, pan3d.x, pan3d.y);

    const projNodes = {};
    const projDeflNodes = {};
    const nodalCaseKey = deflectCase.endsWith("Z") ? "EZ+" : "EX+";
    const nodalCaseMap = ad?.nodal_displacements_mm?.[nodalCaseKey] || null;
    const scaleW = deflectCase.startsWith("Wind") ? 0.55 : 1.0;
    for (const n of sm.nodes) {
      projNodes[n.node_id] = proj({ x: n.x_m, y: n.y_m, z: n.z_m });
      let dx = 0;
      let dy = 0;
      let dz = 0;
      if (showDeflected) {
        const vec = nodalCaseMap?.[String(n.node_id)];
        if (Array.isArray(vec) && vec.length >= 3) {
          dx = (Number(vec[0] || 0) / 1000.0) * scaleW;
          dy = (Number(vec[1] || 0) / 1000.0) * scaleW;
          dz = (Number(vec[2] || 0) / 1000.0) * scaleW;
        } else {
          const d = dispByElev[n.y_m.toFixed(2)] || { dx: 0, dz: 0 };
          dx = d.dx;
          dz = d.dz;
        }
      }
      projDeflNodes[n.node_id] = proj({
        x: n.x_m + dx * deflectAmp,
        y: n.y_m + dy * deflectAmp,
        z: n.z_m + dz * deflectAmp,
      });
    }

    const hits = [];
    const rwHits = [];
    const slabHits = [];

    // 0. Translucent slab panels (matched to slab_designs by panel_id + floor_level)
    for (const panel of sm?.slab_panels || []) {
      if (panel.slab_action === "void" || !Array.isArray(panel.polygon_m) || panel.polygon_m.length < 3) {
        continue;
      }
      const sd = slabDesignByKey[`${panel.floor_level}::${panel.panel_id}`];
      if (!sd) continue;
      const yElev = levelElevById[panel.floor_level] ?? 0;
      const polyPts = panel.polygon_m.map(([px, pz]) => proj({ x: px, y: yElev, z: pz }));
      const isSel =
        selectedEntity?.type === "slab" &&
        selectedEntity.id === panel.panel_id &&
        (!selectedEntity.floor_level || selectedEntity.floor_level === panel.floor_level);
      ctx.beginPath();
      ctx.moveTo(polyPts[0].sx, polyPts[0].sy);
      for (let i = 1; i < polyPts.length; i++) ctx.lineTo(polyPts[i].sx, polyPts[i].sy);
      ctx.closePath();
      ctx.fillStyle = isSel
        ? "rgba(56, 189, 248, 0.34)"
        : sd.status === "FAIL"
        ? "rgba(239, 68, 68, 0.16)"
        : "rgba(56, 189, 248, 0.08)";
      ctx.fill();
      ctx.strokeStyle = isSel ? "#38bdf8" : "rgba(148, 163, 184, 0.28)";
      ctx.lineWidth = isSel ? 2.2 : 0.8;
      ctx.stroke();
      if (isSel) {
        const cxS = polyPts.reduce((acc, p) => acc + p.sx, 0) / polyPts.length;
        const cyS = polyPts.reduce((acc, p) => acc + p.sy, 0) / polyPts.length;
        ctx.fillStyle = "#e0f2fe";
        ctx.font = "bold 10px ui-monospace, SFMono-Regular, monospace";
        ctx.fillText(`${panel.panel_id} (${panel.floor_level})`, cxS - 22, cyS);
      }
      slabHits.push({
        type: "slab",
        id: panel.panel_id,
        floor_level: panel.floor_level,
        kind: "poly",
        pts: polyPts,
      });
    }

    // 1. Retaining walls (render all spatially placed walls using exact X, Y-up, Z coordinates;
    // preserve legacy min-Z perimeter fallback for non-spatial retaining walls)
    const rwList = ad?.retaining_wall_designs || [];
    let drewFallbackRw = false;
    for (const rw of rwList) {
      const hRw = rw.retained_height_m || 2.8;
      let c1, c2, c3, c4;
      if (Array.isArray(rw.start_xyz_m) && Array.isArray(rw.end_xyz_m)) {
        const [sx, sy, sz] = rw.start_xyz_m;
        const [ex, ey, ez] = rw.end_xyz_m;
        c1 = proj({ x: sx, y: sy, z: sz });
        c2 = proj({ x: ex, y: ey, z: ez });
        c3 = proj({ x: ex, y: ey + hRw, z: ez });
        c4 = proj({ x: sx, y: sy + hRw, z: sz });
      } else if (!drewFallbackRw) {
        drewFallbackRw = true;
        c1 = proj({ x: minX, y: minY, z: minZ - 0.15 });
        c2 = proj({ x: maxX, y: minY, z: minZ - 0.15 });
        c3 = proj({ x: maxX, y: minY + hRw, z: minZ - 0.15 });
        c4 = proj({ x: minX, y: minY + hRw, z: minZ - 0.15 });
      } else {
        continue;
      }
      const isSelRw = selectedEntity?.type === "retaining_wall" && selectedEntity.id === rw.wall_id;
      ctx.beginPath();
      ctx.moveTo(c1.sx, c1.sy); ctx.lineTo(c2.sx, c2.sy);
      ctx.lineTo(c3.sx, c3.sy); ctx.lineTo(c4.sx, c4.sy); ctx.closePath();
      ctx.fillStyle = isSelRw
        ? "rgba(56, 189, 248, 0.36)"
        : rw.status === "FAIL"
        ? "rgba(239, 68, 68, 0.24)"
        : "rgba(148, 163, 184, 0.24)";
      ctx.fill();
      ctx.strokeStyle = isSelRw ? "#38bdf8" : rw.status === "FAIL" ? "#f87171" : "#cbd5e1";
      ctx.lineWidth = isSelRw ? 2.6 : 1.5;
      ctx.stroke();
      ctx.fillStyle = isSelRw ? "#e0f2fe" : "#e2e8f0";
      ctx.font = "bold 10px ui-monospace, SFMono-Regular, monospace";
      ctx.fillText(rw.wall_id, (c1.sx + c2.sx) * 0.5, (c3.sy + c4.sy) * 0.5 - 4);
      rwHits.push({
        type: "retaining_wall",
        id: rw.wall_id,
        kind: "poly",
        pts: [c1, c2, c3, c4],
      });
    }

    // 2. Scaled 3D footing pads at actual y_m
    const pulseAlpha = 0.35 + 0.35 * Math.abs(Math.sin((pulsePhase / 20) * Math.PI * 2));
    for (const f of ad?.footing_designs || []) {
      const n = nodeById[f.node_id];
      if (!n) continue;
      const hb = f.b_m * 0.5;
      const hl = f.l_m * 0.5;
      const tm = f.thickness_mm / 1000.0;
      const topPts = [
        proj({ x: n.x_m - hb, y: n.y_m, z: n.z_m - hl }),
        proj({ x: n.x_m + hb, y: n.y_m, z: n.z_m - hl }),
        proj({ x: n.x_m + hb, y: n.y_m, z: n.z_m + hl }),
        proj({ x: n.x_m - hb, y: n.y_m, z: n.z_m + hl }),
      ];
      const botPts = [
        proj({ x: n.x_m - hb, y: n.y_m - tm, z: n.z_m - hl }),
        proj({ x: n.x_m + hb, y: n.y_m - tm, z: n.z_m - hl }),
        proj({ x: n.x_m + hb, y: n.y_m - tm, z: n.z_m + hl }),
        proj({ x: n.x_m - hb, y: n.y_m - tm, z: n.z_m + hl }),
      ];
      const isSel = selectedEntity?.type === "footing" && selectedEntity.id === f.node_id;
      const isClash = clashNodeSet.has(f.node_id);
      const col = isSel ? "#38bdf8" : isClash ? "#ef4444" : statusColor(f.governing_dcr, f.status);

      ctx.beginPath();
      ctx.moveTo(topPts[0].sx, topPts[0].sy);
      for (let i = 1; i < 4; i++) ctx.lineTo(topPts[i].sx, topPts[i].sy);
      ctx.closePath();
      ctx.fillStyle = isClash
        ? `rgba(239, 68, 68, ${pulseAlpha.toFixed(2)})`
        : isSel
        ? "rgba(56, 189, 248, 0.34)"
        : "rgba(148, 163, 184, 0.16)";
      ctx.fill();
      ctx.strokeStyle = col;
      ctx.lineWidth = isSel || isClash ? 2.2 : 1.2;
      ctx.stroke();

      // Bottom outline + vertical edges
      ctx.beginPath();
      for (let i = 0; i < 4; i++) {
        ctx.moveTo(topPts[i].sx, topPts[i].sy);
        ctx.lineTo(botPts[i].sx, botPts[i].sy);
      }
      ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
      ctx.lineWidth = 0.9;
      ctx.stroke();

      const centerP = projNodes[f.node_id];
      hits.push({ type: "footing", id: f.node_id, kind: "point", x: centerP.sx, y: centerP.sy, r: 14 });
    }

    // 3. Proportioned beams, columns & pedestals sorted back-to-front
    const segs = [];
    for (const m of sm.members || []) {
      const pa = projNodes[m.start_node];
      const pb = projNodes[m.end_node];
      if (!pa || !pb) continue;
      const isCol = m.member_type === "column";
      const res = isCol ? colByMid[m.member_id] : beamByMid[m.member_id];
      const isSel = selectedEntity?.type === (isCol ? "column" : "beam") && selectedEntity.id === m.member_id;
      const color = isSel
        ? "#38bdf8"
        : m.is_pedestal
        ? "#818cf8"
        : res
        ? statusColor(res.governing_dcr, res.status)
        : "#475569";
      const secDim = res ? Math.max(res.width_mm, res.depth_mm) : 350;
      const lw = isSel ? 5.2 : Math.max(2.0, Math.min(5.2, secDim / 120));
      segs.push({ m, res, pa, pb, isCol, isSel, color, lw, depth: (pa.depth + pb.depth) * 0.5 });
    }
    segs.sort((a, b) => a.depth - b.depth);

    for (const s of segs) {
      ctx.beginPath();
      ctx.moveTo(s.pa.sx, s.pa.sy);
      ctx.lineTo(s.pb.sx, s.pb.sy);
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.lw;
      ctx.stroke();

      // Deflected ghost overlay
      if (showDeflected) {
        const da = projDeflNodes[s.m.start_node];
        const db = projDeflNodes[s.m.end_node];
        if (da && db) {
          ctx.save();
          ctx.setLineDash([4, 3]);
          ctx.beginPath();
          ctx.moveTo(da.sx, da.sy);
          ctx.lineTo(db.sx, db.sy);
          ctx.strokeStyle = "rgba(56, 189, 248, 0.78)";
          ctx.lineWidth = 1.6;
          ctx.stroke();
          ctx.restore();
        }
      }

      // Overlay text at member midpoint
      if (s.res && (overlayMode === "marks" || overlayMode === "forces")) {
        const mx = (s.pa.sx + s.pb.sx) * 0.5;
        const my = (s.pa.sy + s.pb.sy) * 0.5;
        const txt =
          overlayMode === "marks"
            ? `${s.res.section_id} ${s.res.width_mm.toFixed(0)}x${s.res.depth_mm.toFixed(0)}`
            : s.isCol
            ? `Pu ${s.res.pu_max_kn.toFixed(0)}`
            : `Mu ${Math.max(s.res.mu_neg_start_knm, s.res.mu_pos_mid_knm).toFixed(0)}`;
        ctx.fillStyle = "#e2e8f0";
        ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
        ctx.fillText(txt, mx + 3, my - 3);
      }

      hits.push({
        type: s.isCol ? "column" : "beam",
        id: s.m.member_id,
        kind: "seg",
        x1: s.pa.sx,
        y1: s.pa.sy,
        x2: s.pb.sx,
        y2: s.pb.sy,
      });
    }

    // 4. SMRF Joint SCWB badges in 3D when overlayMode === "scwb"
    if (overlayMode === "scwb") {
      for (const j of ad?.smrf_joint_checks || []) {
        const p = projNodes[j.joint_node_id];
        if (!p) continue;
        const isSel = selectedEntity?.type === "joint" && selectedEntity.id === j.joint_node_id;
        const jc = isSel ? "#38bdf8" : j.scwb_status === "PASS" ? "#10b981" : "#ef4444";
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, 6.5, 0, Math.PI * 2);
        ctx.fillStyle = jc;
        ctx.fill();
        ctx.fillStyle = "#090e17";
        ctx.font = "bold 9px ui-monospace, monospace";
        ctx.fillText(j.scwb_ratio.toFixed(1), p.sx + 8, p.sy + 3);
        hits.push({ type: "joint", id: j.joint_node_id, kind: "point", x: p.sx, y: p.sy, r: 10 });
      }
    }

    hitListRef.current = { primary: hits, rw: rwHits, slabs: slabHits };
  }, [
    collapsed, layoutMode, sm, ad, selectedEntity, overlayMode, showDeflected,
    deflectAmp, dispByElev, yaw, pitch, zoom3d, pan3d, pulsePhase,
    beamByMid, colByMid, nodeById, clashNodeSet, slabDesignByKey, levelElevById,
  ]);

  function on3dDown(e) {
    orbitRef.current = {
      dragging: true,
      moved: false,
      pan: e.shiftKey || e.button === 2,
      lastX: e.clientX,
      lastY: e.clientY,
    };
  }
  function on3dMove(e) {
    if (!orbitRef.current.dragging) return;
    const dx = e.clientX - orbitRef.current.lastX;
    const dy = e.clientY - orbitRef.current.lastY;
    if (Math.hypot(dx, dy) > 2) orbitRef.current.moved = true;
    orbitRef.current.lastX = e.clientX;
    orbitRef.current.lastY = e.clientY;
    if (orbitRef.current.pan) {
      setPan3d((p) => ({ x: p.x + dx, y: p.y + dy }));
    } else {
      setYaw((y) => y + dx * 0.01);
      setPitch((p) => Math.max(-1.45, Math.min(1.45, p + dy * 0.01)));
    }
  }
  function on3dUp(e) {
    const wasDrag = orbitRef.current.moved;
    orbitRef.current.dragging = false;
    if (wasDrag) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const hitBuckets = hitListRef.current || { primary: [], rw: [], slabs: [] };
    let best = null;
    let bestD = 12;
    for (const h of hitBuckets.primary || []) {
      const d =
        h.kind === "point"
          ? Math.hypot(px - h.x, py - h.y)
          : ptSegDist(px, py, h.x1, h.y1, h.x2, h.y2);
      if (d < Math.min(bestD, h.r || 12)) {
        bestD = d;
        best = { type: h.type, id: h.id };
      }
    }
    if (!best) {
      for (const h of hitBuckets.rw || []) {
        if (pointInPolygon(px, py, h.pts)) {
          best = { type: "retaining_wall", id: h.id };
          break;
        }
      }
    }
    if (!best) {
      for (const h of hitBuckets.slabs || []) {
        if (pointInPolygon(px, py, h.pts)) {
          best = { type: "slab", id: h.id, floor_level: h.floor_level };
          break;
        }
      }
    }
    onSelectEntity(best);
  }
  function on3dWheel(e) {
    e.preventDefault();
    setZoom3d((z) => Math.max(0.4, Math.min(3.5, z * (e.deltaY < 0 ? 1.1 : 0.91))));
  }

  function applyCameraPreset(preset) {
    setPan3d({ x: 0, y: 0 });
    setZoom3d(1.0);
    if (preset === "ISO") { setYaw(-0.68); setPitch(0.46); }
    else if (preset === "TOP") { setYaw(0); setPitch(1.42); }
    else if (preset === "FRONT") { setYaw(0); setPitch(0.02); }
    else if (preset === "RIGHT") { setYaw(-Math.PI * 0.5); setPitch(0.02); }
    else if (preset === "FIT") { setYaw(-0.68); setPitch(0.46); }
  }

  async function handlePinCustomSnapshot(title, caption, imageDataUrl) {
    setSaving(true);
    try {
      const res = await fetch(`/api/projects/${pid}/design/pin-snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, caption, image_data_url: imageDataUrl }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Failed to pin snapshot.");
      onDesignUpdated(body);
      onNotice(`Pinned '${title}' to Module 08 OBO Report.`);
    } catch (err) {
      onError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handlePinSnapshot() {
    setSaving(true);
    try {
      let dataUrl = "";
      if (canvasRef.current && layoutMode !== "2d") {
        dataUrl = canvasRef.current.toDataURL("image/png");
      } else if (svg2dRef.current) {
        const xml = new XMLSerializer().serializeToString(svg2dRef.current);
        dataUrl = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(xml)))}`;
      }
      if (!dataUrl) throw new Error("No active viewport image available to capture.");
      const res = await fetch(`/api/projects/${pid}/design/pin-snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: `Design Studio (${layoutMode.toUpperCase()} · ${overlayMode.toUpperCase()})`,
          caption: `Max Beam DCR ${summary.maxBeam.toFixed(2)}, Max Col DCR ${summary.maxCol.toFixed(2)}, Max Ftg DCR ${summary.maxFtg.toFixed(2)}`,
          image_data_url: dataUrl,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Failed to pin snapshot.");
      onDesignUpdated(body);
      onNotice("Pinned current design studio view to Module 08 OBO Report.");
    } catch (err) {
      onError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleApplyOverride() {
    if (!selectedEntity || selectedEntity.type === "joint") return;
    setSaving(true);
    try {
      const targetId =
        selectedEntity.type === "footing"
          ? `N${selectedEntity.id}`
          : `M${selectedEntity.id}`;
      const res = await fetch(`/api/projects/${pid}/design/override-member`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_type: selectedEntity.type,
          target_id: targetId,
          updates: overrideForm,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Override failed.");
      onDesignUpdated(body);
      onNotice(`Updated ${selectedEntity.type} ${targetId} and recomputed NSCP 2015 DCRs.`);
    } catch (err) {
      onError(err.message);
    } finally {
      setSaving(false);
    }
  }

  // =========================================================================
  // 2D PLAN / ELEVATION VIEWPORT DATA
  // =========================================================================
  const view2dOptions = useMemo(() => {
    const opts = [{ value: "foundation", label: "Foundation Plan" }];
    for (const lv of sortedLevels) {
      opts.push({ value: `floor:${lv.id}`, label: `${lv.id} Framing Plan (${lv.elevation_m.toFixed(1)}m)` });
    }
    for (const gx of project?.grids?.x_grids || []) {
      opts.push({ value: `elevX:${gx.label}`, label: `Grid ${gx.label} Elevation (X=${gx.coord_m.toFixed(1)}m)` });
    }
    for (const gy of project?.grids?.y_grids || []) {
      opts.push({ value: `elevZ:${gy.label}`, label: `Grid ${gy.label} Elevation (Z=${gy.coord_m.toFixed(1)}m)` });
    }
    return opts;
  }, [sortedLevels, project?.grids]);

  const svg2dContent = useMemo(() => {
    if (!sm?.nodes?.length) return null;
    const W = 520;
    const H = 430;
    const pad = 48;

    const isElev = view2dMode.startsWith("elev");
    if (isElev) {
      const isX = view2dMode.startsWith("elevX:");
      const lbl = view2dMode.split(":")[1];
      const gList = isX ? project?.grids?.x_grids || [] : project?.grids?.y_grids || [];
      const gObj = gList.find((g) => g.label === lbl);
      const coord = gObj ? gObj.coord_m : 0;
      const filtNodes = sm.nodes.filter((n) => Math.abs((isX ? n.x_m : n.z_m) - coord) <= 0.35);
      const nidSet = new Set(filtNodes.map((n) => n.node_id));
      const xs = filtNodes.map((n) => (isX ? n.z_m : n.x_m));
      const ys = filtNodes.map((n) => n.y_m);
      const minU = Math.min(...xs, 0);
      const maxU = Math.max(...xs, 4);
      const minV = Math.min(...ys, 0);
      const maxV = Math.max(...ys, 3);
      const sc = Math.min((W - 2 * pad) / Math.max(maxU - minU, 1), (H - 2 * pad) / Math.max(maxV - minV, 1));
      const sx = (u) => pad + (u - minU) * sc + ((W - 2 * pad) - (maxU - minU) * sc) * 0.5;
      const sy = (v) => H - pad - (v - minV) * sc;

      return (
        <svg ref={svg2dRef} className="pdv-2d-svg" viewBox={`0 0 ${W} ${H}`}>
          {sortedLevels.map((lv) => (
            <g key={lv.id}>
              <line x1={pad - 12} y1={sy(lv.elevation_m)} x2={W - pad + 12} y2={sy(lv.elevation_m)} stroke="#1e293b" strokeDasharray="4 4" />
              <text x={8} y={sy(lv.elevation_m) + 3} fill="#94a3b8" fontSize="9.5">{lv.id} ({lv.elevation_m.toFixed(1)}m)</text>
            </g>
          ))}
          {(sm.members || []).filter((m) => nidSet.has(m.start_node) && nidSet.has(m.end_node)).map((m) => {
            const a = nodeById[m.start_node];
            const b = nodeById[m.end_node];
            const isCol = m.member_type === "column";
            const res = isCol ? colByMid[m.member_id] : beamByMid[m.member_id];
            const isSel = selectedEntity?.type === (isCol ? "column" : "beam") && selectedEntity.id === m.member_id;
            const col = isSel ? "#38bdf8" : res ? statusColor(res.governing_dcr, res.status) : "#64748b";
            return (
              <line
                key={m.member_id}
                x1={sx(isX ? a.z_m : a.x_m)} y1={sy(a.y_m)}
                x2={sx(isX ? b.z_m : b.x_m)} y2={sy(b.y_m)}
                stroke={col} strokeWidth={isSel ? 5 : isCol ? 4 : 3}
                style={{ cursor: "pointer" }}
                onClick={() => onSelectEntity({ type: isCol ? "column" : "beam", id: m.member_id })}
              />
            );
          })}
          {filtNodes.filter((n) => ftgByNid[n.node_id]).map((n) => {
            const f = ftgByNid[n.node_id];
            const isSel = selectedEntity?.type === "footing" && selectedEntity.id === f.node_id;
            const px = sx(isX ? n.z_m : n.x_m);
            const py = sy(n.y_m);
            const fw = Math.max(18, f.b_m * sc);
            const fh = Math.max(6, (f.thickness_mm / 1000) * sc);
            return (
              <rect
                key={f.node_id}
                x={px - fw * 0.5} y={py} width={fw} height={fh}
                fill={isSel ? "rgba(56,189,248,0.4)" : "rgba(232,121,249,0.28)"}
                stroke={isSel ? "#38bdf8" : statusColor(f.governing_dcr, f.status)}
                strokeWidth="1.6" style={{ cursor: "pointer" }}
                onClick={() => onSelectEntity({ type: "footing", id: f.node_id })}
              />
            );
          })}
        </svg>
      );
    }

    // Plan View (Foundation or Floor Framing)
    const xs = sm.nodes.map((n) => n.x_m);
    const zs = sm.nodes.map((n) => n.z_m);
    const minX = Math.min(...xs, 0);
    const maxX = Math.max(...xs, 4);
    const minZ = Math.min(...zs, 0);
    const maxZ = Math.max(...zs, 4);
    const sc = Math.min((W - 2 * pad) / Math.max(maxX - minX, 1), (H - 2 * pad) / Math.max(maxZ - minZ, 1));
    const sx = (x) => pad + (x - minX) * sc + ((W - 2 * pad) - (maxX - minX) * sc) * 0.5;
    const sy = (z) => pad + (z - minZ) * sc + ((H - 2 * pad) - (maxZ - minZ) * sc) * 0.5;

    const isFdn = view2dMode === "foundation";
    const floorId = isFdn ? sortedLevels[0]?.id : view2dMode.split(":")[1];

    const planBeams = (sm.members || []).filter((m) => {
      if (m.member_type === "column") return false;
      if (isFdn) return m.member_type === "tie_beam" || m.floor_level === floorId;
      return m.floor_level === floorId;
    });

    const planJoints = (ad?.smrf_joint_checks || []).filter((j) => j.floor_level === floorId);

    return (
      <svg ref={svg2dRef} className="pdv-2d-svg" viewBox={`0 0 ${W} ${H}`}>
        {/* Gridlines */}
        {(project?.grids?.x_grids || []).map((gx) => (
          <g key={`gx-${gx.label}`}>
            <line x1={sx(gx.coord_m)} y1={pad - 18} x2={sx(gx.coord_m)} y2={H - pad + 14} stroke="#1e293b" strokeDasharray="5 4" />
            <circle cx={sx(gx.coord_m)} cy={pad - 24} r="9" fill="#0f172a" stroke="#38bdf8" strokeWidth="1.1" />
            <text x={sx(gx.coord_m)} y={pad - 21} textAnchor="middle" fill="#cbd5e1" fontSize="9.5">{gx.label}</text>
          </g>
        ))}
        {(project?.grids?.y_grids || []).map((gy) => (
          <g key={`gy-${gy.label}`}>
            <line x1={pad - 18} y1={sy(gy.coord_m)} x2={W - pad + 14} y2={sy(gy.coord_m)} stroke="#1e293b" strokeDasharray="5 4" />
            <circle cx={pad - 24} cy={sy(gy.coord_m)} r="9" fill="#0f172a" stroke="#38bdf8" strokeWidth="1.1" />
            <text x={pad - 24} y={sy(gy.coord_m) + 3} textAnchor="middle" fill="#cbd5e1" fontSize="9.5">{gy.label}</text>
          </g>
        ))}

        {/* Footing pads on Foundation Plan */}
        {isFdn && (ad?.footing_designs || []).map((f) => {
          const n = nodeById[f.node_id];
          if (!n) return null;
          const isSel = selectedEntity?.type === "footing" && selectedEntity.id === f.node_id;
          const isClash = clashNodeSet.has(f.node_id);
          const rw = Math.max(16, f.b_m * sc);
          const rl = Math.max(16, f.l_m * sc);
          const col = isSel ? "#38bdf8" : isClash ? "#ef4444" : statusColor(f.governing_dcr, f.status);
          return (
            <g
              key={`ftg-${f.node_id}`}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectEntity({ type: "footing", id: f.node_id })}
            >
              <rect
                x={sx(n.x_m) - rw * 0.5} y={sy(n.z_m) - rl * 0.5} width={rw} height={rl}
                fill={isClash ? "rgba(239,68,68,0.28)" : isSel ? "rgba(56,189,248,0.28)" : "rgba(232,121,249,0.12)"}
                stroke={col} strokeWidth={isSel || isClash ? 2.2 : 1.3}
                strokeDasharray={isClash ? "4 2" : undefined}
              />
              <text x={sx(n.x_m)} y={sy(n.z_m) + rl * 0.5 + 11} textAnchor="middle" fill={col} fontSize="9.5" fontWeight="600">
                {f.footing_tag} ({f.b_m.toFixed(1)}×{f.l_m.toFixed(1)})
              </text>
            </g>
          );
        })}

        {/* Beams */}
        {planBeams.map((m) => {
          const a = nodeById[m.start_node];
          const b = nodeById[m.end_node];
          if (!a || !b) return null;
          const res = beamByMid[m.member_id];
          const isSel = selectedEntity?.type === "beam" && selectedEntity.id === m.member_id;
          const col = isSel ? "#38bdf8" : res ? statusColor(res.governing_dcr, res.status) : "#64748b";
          const mx = (sx(a.x_m) + sx(b.x_m)) * 0.5;
          const my = (sy(a.z_m) + sy(b.z_m)) * 0.5;
          return (
            <g
              key={`bm-${m.member_id}`}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectEntity({ type: "beam", id: m.member_id })}
            >
              <line x1={sx(a.x_m)} y1={sy(a.z_m)} x2={sx(b.x_m)} y2={sy(b.z_m)} stroke={col} strokeWidth={isSel ? 5 : 3.2} />
              {res && (
                <text x={mx} y={my - 5} textAnchor="middle" fill="#cbd5e1" fontSize="9">
                  {overlayMode === "forces"
                    ? `Mu ${Math.max(res.mu_neg_start_knm, res.mu_pos_mid_knm).toFixed(0)}`
                    : `${res.section_id} (${res.governing_dcr.toFixed(2)})`}
                </text>
              )}
            </g>
          );
        })}

        {/* Columns + SMRF Joint SCWB badges */}
        {(sm.members || []).filter((m) => m.member_type === "column" && (isFdn ? true : m.floor_level === floorId)).map((m) => {
          const a = nodeById[m.start_node];
          if (!a) return null;
          const res = colByMid[m.member_id];
          const isSel = selectedEntity?.type === "column" && selectedEntity.id === m.member_id;
          const col = isSel ? "#38bdf8" : res ? statusColor(res.governing_dcr, res.status) : "#22d3ee";
          return (
            <rect
              key={`col-${m.member_id}`}
              x={sx(a.x_m) - 5} y={sy(a.z_m) - 5} width={10} height={10}
              fill={col} stroke="#090e17" strokeWidth="1"
              style={{ cursor: "pointer" }}
              onClick={() => onSelectEntity({ type: "column", id: m.member_id })}
            />
          );
        })}

        {!isFdn && planJoints.map((j) => {
          const n = nodeById[j.joint_node_id];
          if (!n) return null;
          const jc = j.scwb_status === "PASS" ? "#10b981" : "#ef4444";
          return (
            <g
              key={`j-${j.joint_node_id}`}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectEntity({ type: "joint", id: j.joint_node_id })}
            >
              <rect x={sx(n.x_m) + 7} y={sy(n.z_m) - 14} width={34} height={13} rx="3" fill="#0f172a" stroke={jc} strokeWidth="1" />
              <text x={sx(n.x_m) + 24} y={sy(n.z_m) - 4.5} textAnchor="middle" fill={jc} fontSize="8.5" fontWeight="700">
                {j.scwb_ratio.toFixed(2)}
              </text>
            </g>
          );
        })}
      </svg>
    );
  }, [sm, ad, view2dMode, project?.grids, sortedLevels, nodeById, beamByMid, colByMid, ftgByNid, selectedEntity, clashNodeSet, overlayMode]);

  if (!ad?.generated_at) return null;

  return (
    <section className="pdv-studio" data-testid="post-design-viewer">
      <div className="pdv-studio-head">
        <div className="pdv-studio-title">
          <strong>POST-DESIGN CAD-BIM STUDIO · 3D &amp; 2D INTERACTIVE VERIFICATION</strong>
          <small>
            Click any beam, column, footing, or SMRF joint to inspect cross-section rebar &amp; apply instant overrides.
          </small>
        </div>
        <div className="pdv-studio-head-right">
          {summary.clashCount > 0 && (
            <span className="design-status-pill fail">{summary.clashCount} Footing Clash(es)</span>
          )}
          <button
            type="button"
            className="secondary mini"
            onClick={() => setCollapsed((c) => !c)}
          >
            {collapsed ? "Expand Studio" : "Collapse Studio"}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="pdv-workspace">
          {/* MAIN STAGE */}
          <div className="pdv-stage">
            {/* Top 36px HUD Control Strip */}
            <div className="pdv-hud">
              <div className="pdv-seg-group" role="group" aria-label="Studio Layout">
                {[
                  ["split", "Split (3D + 2D)"],
                  ["3d", "3D BIM"],
                  ["2d", "2D Plan/Elev"],
                ].map(([k, lbl]) => (
                  <button
                    key={k}
                    type="button"
                    className={layoutMode === k ? "pdv-seg active" : "pdv-seg"}
                    onClick={() => setLayoutMode(k)}
                  >
                    {lbl}
                  </button>
                ))}
              </div>

              <div className="pdv-seg-group" role="group" aria-label="Overlay Mode">
                {[
                  ["dcr", "DCR Heatmap"],
                  ["marks", "Marks & Sizes"],
                  ["forces", "Force Envelopes"],
                  ["scwb", "Joint SCWB"],
                ].map(([k, lbl]) => (
                  <button
                    key={k}
                    type="button"
                    className={overlayMode === k ? "pdv-seg active" : "pdv-seg"}
                    onClick={() => setOverlayMode(k)}
                  >
                    {lbl}
                  </button>
                ))}
              </div>

              <label className="pdv-hud-check">
                <input
                  type="checkbox"
                  checked={showDeflected}
                  onChange={(e) => setShowDeflected(e.target.checked)}
                />
                Deflected
              </label>
              {showDeflected && (
                <>
                  <select
                    className="pdv-hud-select"
                    value={deflectCase}
                    onChange={(e) => setDeflectCase(e.target.value)}
                  >
                    <option value="EQX">EQX</option>
                    <option value="EQZ">EQZ</option>
                    <option value="Wind X">Wind X</option>
                    <option value="Wind Z">Wind Z</option>
                  </select>
                  <input
                    type="range"
                    min="1"
                    max="100"
                    value={deflectAmp}
                    onChange={(e) => setDeflectAmp(Number(e.target.value))}
                    title={`Amplification: ${deflectAmp}x`}
                    style={{ width: "64px" }}
                  />
                  <span className="pdv-hud-amp">{deflectAmp}x</span>
                </>
              )}

              <button
                type="button"
                className="pdv-pin-btn"
                disabled={saving}
                onClick={handlePinSnapshot}
              >
                Pin to OBO Report
              </button>
            </div>

            {/* VIEWPORTS */}
            <div className={`pdv-viewports mode-${layoutMode}`}>
              {layoutMode !== "2d" && (
                <div className="pdv-pane pdv-3d-pane">
                  <div className="pdv-cam-pills">
                    {["ISO", "TOP", "FRONT", "RIGHT", "FIT"].map((preset) => (
                      <button
                        key={preset}
                        type="button"
                        className="pdv-cam-pill"
                        onClick={() => applyCameraPreset(preset)}
                      >
                        {preset}
                      </button>
                    ))}
                  </div>
                  <canvas
                    ref={canvasRef}
                    className="pdv-3d-canvas"
                    onPointerDown={on3dDown}
                    onPointerMove={on3dMove}
                    onPointerUp={on3dUp}
                    onPointerLeave={() => { orbitRef.current.dragging = false; }}
                    onWheel={on3dWheel}
                    onContextMenu={(e) => e.preventDefault()}
                  />
                  <div className="pdv-legend">
                    <span><i style={{ background: "#10b981" }} /> &le;0.75</span>
                    <span><i style={{ background: "#f59e0b" }} /> 0.75–1.00</span>
                    <span><i style={{ background: "#ef4444" }} /> &gt;1.00 / Clash</span>
                    <span><i style={{ background: "#38bdf8" }} /> Selected</span>
                  </div>
                </div>
              )}

              {layoutMode !== "3d" && (
                <div className="pdv-pane pdv-2d-pane">
                  <div className="pdv-2d-bar">
                    <select
                      className="pdv-hud-select"
                      aria-label="2D Plan or Elevation View"
                      value={view2dMode}
                      onChange={(e) => setView2dMode(e.target.value)}
                    >
                      {view2dOptions.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                  {svg2dContent}
                </div>
              )}
            </div>
          </div>

          {/* DOCKED RIGHT-HAND STRUCTURAL INSPECTOR (320px) */}
          <aside className="pdv-inspector" data-testid="pdv-inspector">
            {!selectedEntity || !selectedData ? (
              <div className="pdv-insp-body">
                <div className="pdv-insp-head">
                  <strong>Structural Summary</strong>
                  <span className="pdv-insp-sub">Click an element in 3D/2D</span>
                </div>
                <div className="pdv-kpi-grid">
                  <div className="pdv-kpi">
                    <small>Max Beam DCR</small>
                    <strong style={{ color: statusColor(summary.maxBeam, summary.maxBeam > 1 ? "FAIL" : "PASS") }}>
                      {summary.maxBeam.toFixed(2)}
                    </strong>
                  </div>
                  <div className="pdv-kpi">
                    <small>Max Col DCR</small>
                    <strong style={{ color: statusColor(summary.maxCol, summary.maxCol > 1 ? "FAIL" : "PASS") }}>
                      {summary.maxCol.toFixed(2)}
                    </strong>
                  </div>
                  <div className="pdv-kpi">
                    <small>Max Ftg DCR</small>
                    <strong style={{ color: statusColor(summary.maxFtg, summary.maxFtg > 1 ? "FAIL" : "PASS") }}>
                      {summary.maxFtg.toFixed(2)}
                    </strong>
                  </div>
                  <div className="pdv-kpi">
                    <small>SCWB Pass Rate</small>
                    <strong>
                      {summary.scwbTotal > 0 ? `${summary.scwbPass}/${summary.scwbTotal}` : "—"}
                    </strong>
                  </div>
                </div>
                <div className="pdv-clash-box">
                  <div className="pdv-dcr-labels">
                    <span>Footing Plan Clashes</span>
                    <strong style={{ color: summary.clashCount > 0 ? "#ef4444" : "#10b981" }}>
                      {summary.clashCount}
                    </strong>
                  </div>
                  {summary.clashes.length > 0 ? (
                    <ul className="pdv-clash-list">
                      {summary.clashes.slice(0, 5).map((cl, idx) => (
                        <li key={idx}>
                          <button
                            type="button"
                            className="pdv-clash-link"
                            onClick={() => onSelectEntity({ type: "footing", id: cl.node_a })}
                          >
                            {cl.footing_a} &times; {cl.footing_b} ({cl.overlap_area_m2} m&sup2;)
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <small className="pdv-insp-sub">Zero isolated footing plan overlaps detected.</small>
                  )}
                </div>
              </div>
            ) : (
              <div className="pdv-insp-body">
                <div className="pdv-insp-head">
                  <div>
                    <strong>
                      {selectedEntity.type === "beam" && `Beam M${selectedData.member_id} · ${selectedData.section_id}`}
                      {selectedEntity.type === "column" && `Column M${selectedData.member_id} · ${selectedData.section_id}`}
                      {selectedEntity.type === "footing" && `Footing ${selectedData.footing_tag} (N${selectedData.node_id})`}
                      {selectedEntity.type === "joint" && `SMRF Joint N${selectedData.joint_node_id} (${selectedData.grid_ref || selectedData.floor_level})`}
                      {selectedEntity.type === "slab" &&
                        `Slab ${selectedData.panel_id} (${selectedData.floor_level}) · ${
                          selectedData.slab_action === "one_way" ? "1-Way" : "2-Way"
                        }`}
                      {selectedEntity.type === "retaining_wall" &&
                        `Retaining Wall ${selectedData.wall_id}${
                          selectedData.floor_level ? ` (${selectedData.floor_level})` : ""
                        }`}
                    </strong>
                    <span
                      className={`design-status-pill ${
                        (selectedEntity.type === "joint"
                          ? selectedData.scwb_status === "PASS" && selectedData.joint_shear_status === "PASS"
                          : selectedData.status === "PASS")
                          ? "pass"
                          : "fail"
                      }`}
                    >
                      {selectedEntity.type === "joint"
                        ? selectedData.scwb_status === "PASS" && selectedData.joint_shear_status === "PASS"
                          ? "PASS"
                          : "FAIL"
                        : selectedData.status}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="secondary mini"
                    onClick={() => onSelectEntity(null)}
                  >
                    &times;
                  </button>
                </div>

                {selectedEntity.type !== "joint" && (
                  <div className="pdv-seg-group" role="group" aria-label="Detail View Mode" style={{ alignSelf: "flex-start" }}>
                    <button
                      type="button"
                      className={detailMode === "2d" ? "pdv-seg active" : "pdv-seg"}
                      onClick={() => setDetailMode("2d")}
                    >
                      2D Section
                    </button>
                    <button
                      type="button"
                      className={detailMode === "3d" ? "pdv-seg active" : "pdv-seg"}
                      onClick={() => setDetailMode("3d")}
                    >
                      3D Rebar Cage
                    </button>
                  </div>
                )}

                {selectedEntity.type !== "joint" && detailMode === "3d" ? (
                  <Member3DDetailViewer
                    entity={selectedEntity}
                    data={selectedData}
                    height={225}
                    saving={saving}
                    onExpandModal={() => setModal3dOpen(true)}
                    onPinSnapshot={handlePinCustomSnapshot}
                  />
                ) : (
                  <CrossSectionBlueprint entity={selectedEntity} data={selectedData} />
                )}

                {/* DCR Progress Bars */}
                <div className="pdv-dcr-block">
                  {selectedEntity.type === "beam" && (
                    <>
                      <DcrBar label="Flexure DCR" value={selectedData.flexure_dcr} />
                      <DcrBar label="Shear DCR" value={selectedData.shear_dcr} />
                    </>
                  )}
                  {selectedEntity.type === "column" && (
                    <>
                      <DcrBar label="Biaxial P-M DCR" value={selectedData.pm_dcr} />
                      <DcrBar label="Confined Shear DCR" value={selectedData.shear_dcr} />
                    </>
                  )}
                  {selectedEntity.type === "footing" && (
                    <>
                      <DcrBar label="Bearing DCR" value={selectedData.bearing_dcr} />
                      <DcrBar label="One-Way Shear DCR" value={selectedData.one_way_shear_dcr} />
                      <DcrBar label="Punching Shear DCR" value={selectedData.punching_shear_dcr} />
                    </>
                  )}
                  {selectedEntity.type === "joint" && (
                    <>
                      <DcrBar label="SCWB Ratio (>= 1.20)" value={selectedData.scwb_ratio} limit={1.2} invert />
                      <DcrBar label="Joint Shear DCR" value={selectedData.joint_shear_dcr} />
                    </>
                  )}
                  {selectedEntity.type === "slab" && (
                    <div className="pdv-kpi-grid" style={{ marginTop: 4 }}>
                      <div className="pdv-kpi">
                        <small>Thickness</small>
                        <strong>{Number(selectedData.thickness_mm || 0).toFixed(0)} mm</strong>
                      </div>
                      <div className="pdv-kpi">
                        <small>Aspect (Ly/Lx)</small>
                        <strong>
                          {(
                            Math.max(selectedData.lx_m, selectedData.ly_m) /
                            Math.max(0.1, Math.min(selectedData.lx_m, selectedData.ly_m))
                          ).toFixed(2)}
                        </strong>
                      </div>
                      <div className="pdv-kpi">
                        <small>Short (+)</small>
                        <strong>{selectedData.short_span_pos}</strong>
                      </div>
                      <div className="pdv-kpi">
                        <small>Long (+)</small>
                        <strong>{selectedData.long_span_pos}</strong>
                      </div>
                    </div>
                  )}
                  {selectedEntity.type === "retaining_wall" && (
                    <>
                      <DcrBar
                        label="FS Overturning (>= 1.50)"
                        value={Number(selectedData.fs_overturning || 0)}
                        limit={1.5}
                        invert
                      />
                      <DcrBar
                        label="FS Sliding (>= 1.50)"
                        value={Number(selectedData.fs_sliding || 0)}
                        limit={1.5}
                        invert
                      />
                      <DcrBar
                        label={`Toe Bearing (${Number(selectedData.max_toe_bearing_kpa || 0).toFixed(0)} / ${Number(selectedData.allowable_bearing_kpa || 150).toFixed(0)} kPa)`}
                        value={
                          Number(selectedData.max_toe_bearing_kpa || 0) /
                          Math.max(1, Number(selectedData.allowable_bearing_kpa || 150))
                        }
                      />
                      <div className="pdv-kpi-grid" style={{ marginTop: 6 }}>
                        <div className="pdv-kpi">
                          <small>Ka / K0 / Kae</small>
                          <strong>
                            {Number(selectedData.ka || 0).toFixed(2)} / {Number(selectedData.k0 || 0).toFixed(2)} /{" "}
                            {Number(selectedData.k_ae_seismic || 0).toFixed(2)}
                          </strong>
                        </div>
                        <div className="pdv-kpi">
                          <small>Stem Mu / Vu</small>
                          <strong>
                            {Number(selectedData.stem_mu_knm_per_m || 0).toFixed(1)} /{" "}
                            {Number(selectedData.stem_vu_kn_per_m || 0).toFixed(1)}
                          </strong>
                        </div>
                      </div>
                    </>
                  )}
                </div>

                {/* Quick Detailing Override */}
                {!["joint", "slab", "retaining_wall"].includes(selectedEntity.type) && (
                  <div className="pdv-override-box">
                    <div className="pdv-override-title">Quick Detailing Override</div>
                    {selectedEntity.type === "beam" && (
                      <div className="pdv-override-grid">
                        <label>
                          <span>Depth (mm)</span>
                          <input
                            type="number"
                            step="50"
                            value={overrideForm.depth_mm ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, depth_mm: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Top Start</span>
                          <input
                            value={overrideForm.bars_top_start ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, bars_top_start: e.target.value })}
                          />
                        </label>
                        <label>
                          <span>Bot Mid</span>
                          <input
                            value={overrideForm.bars_bot_mid ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, bars_bot_mid: e.target.value })}
                          />
                        </label>
                        <label>
                          <span>Stirrups</span>
                          <input
                            value={overrideForm.stirrups_support ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, stirrups_support: e.target.value })}
                          />
                        </label>
                      </div>
                    )}
                    {selectedEntity.type === "column" && (
                      <div className="pdv-override-grid">
                        <label>
                          <span>Width (mm)</span>
                          <input
                            type="number"
                            step="50"
                            value={overrideForm.width_mm ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, width_mm: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Depth (mm)</span>
                          <input
                            type="number"
                            step="50"
                            value={overrideForm.depth_mm ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, depth_mm: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Main Bars</span>
                          <input
                            value={overrideForm.main_bars ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, main_bars: e.target.value })}
                          />
                        </label>
                        <label>
                          <span>Confined Ties</span>
                          <input
                            value={overrideForm.ties_confined ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, ties_confined: e.target.value })}
                          />
                        </label>
                      </div>
                    )}
                    {selectedEntity.type === "footing" && (
                      <div className="pdv-override-grid">
                        <label>
                          <span>Width B (m)</span>
                          <input
                            type="number"
                            step="0.1"
                            value={overrideForm.b_m ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, b_m: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Length L (m)</span>
                          <input
                            type="number"
                            step="0.1"
                            value={overrideForm.l_m ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, l_m: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Thk t (mm)</span>
                          <input
                            type="number"
                            step="50"
                            value={overrideForm.thickness_mm ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, thickness_mm: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span>Bars X</span>
                          <input
                            value={overrideForm.bottom_bars_x ?? ""}
                            onChange={(e) => setOverrideForm({ ...overrideForm, bottom_bars_x: e.target.value })}
                          />
                        </label>
                      </div>
                    )}
                    <button
                      type="button"
                      disabled={saving}
                      onClick={handleApplyOverride}
                      style={{ width: "100%", marginTop: "8px" }}
                    >
                      {saving ? "Re-Checking..." : "Apply & Re-Check"}
                    </button>
                  </div>
                )}
              </div>
            )}
          </aside>
        </div>
      )}

      {modal3dOpen && selectedEntity && selectedData && selectedEntity.type !== "joint" && (
        <div className="m3d-modal-backdrop" onClick={() => setModal3dOpen(false)}>
          <div className="m3d-modal" onClick={(e) => e.stopPropagation()}>
            <div className="pdv-studio-head">
              <div className="pdv-studio-title">
                <strong>3D MEMBER REBAR CAGE INSPECTOR</strong>
                <small>Drag to orbit · Scroll to zoom · Adjust concrete opacity or pin snapshot directly to Module 08 OBO Report</small>
              </div>
              <button type="button" className="secondary mini" onClick={() => setModal3dOpen(false)}>
                Close
              </button>
            </div>
            <div style={{ padding: "12px" }}>
              <Member3DDetailViewer
                entity={selectedEntity}
                data={selectedData}
                height={480}
                isModal
                saving={saving}
                onPinSnapshot={handlePinCustomSnapshot}
              />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
