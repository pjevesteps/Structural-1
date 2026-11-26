import React, { useEffect, useMemo, useRef, useState } from "react";

/**
 * Defensive rebar callout parser supporting formats like:
 *   "3-Ø16", "4-16mm", "8-20mm", "Ø10 @ 100/150/200", "10mm @ 100mm o.c.", "9-16mm @ 175mm o.c."
 * Never returns NaN or non-positive numbers.
 */
export function parseRebarCallout(raw, fallback = {}) {
  const fbCount = Number.isFinite(fallback.count) && fallback.count > 0 ? fallback.count : 4;
  const fbDia = Number.isFinite(fallback.diaMm) && fallback.diaMm > 0 ? fallback.diaMm : 16;
  const fbSpacing = Number.isFinite(fallback.spacingMm) && fallback.spacingMm > 0 ? fallback.spacingMm : 150;
  const text = String(raw || "").trim();

  let count = fbCount;
  let diaMm = fbDia;
  let spacingMm = fbSpacing;
  let midSpacingMm = Math.min(250, fbSpacing * 1.5);

  const countDiaMatch = text.match(/(\d+)\s*[-xX]\s*(?:[ØøDd])?\s*(\d+(?:\.\d+)?)/);
  if (countDiaMatch) {
    const c = parseInt(countDiaMatch[1], 10);
    const d = parseFloat(countDiaMatch[2]);
    if (Number.isFinite(c) && c >= 1) count = Math.min(c, 24);
    if (Number.isFinite(d) && d >= 6) diaMm = Math.min(d, 40);
  } else {
    const diaOnly = text.match(/(?:[ØøDd]\s*|)(\d+(?:\.\d+)?)\s*(?:mm)?\s*@/i);
    if (diaOnly) {
      const d = parseFloat(diaOnly[1]);
      if (Number.isFinite(d) && d >= 6) diaMm = Math.min(d, 32);
    }
  }

  const spMulti = text.match(/@\s*(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)/);
  if (spMulti) {
    const s1 = parseFloat(spMulti[1]);
    const s2 = parseFloat(spMulti[2]);
    if (Number.isFinite(s1) && s1 >= 25) spacingMm = s1;
    if (Number.isFinite(s2) && s2 >= 25) midSpacingMm = s2;
  } else {
    const spSingle = text.match(/@\s*(\d+(?:\.\d+)?)/);
    if (spSingle) {
      const s1 = parseFloat(spSingle[1]);
      if (Number.isFinite(s1) && s1 >= 25) {
        spacingMm = s1;
        midSpacingMm = Math.min(250, s1 * 1.5);
      }
    }
  }

  return { count, diaMm, spacingMm, midSpacingMm };
}

function projectPt(p, yaw, pitch, scale, cx, cy) {
  const cyaw = Math.cos(yaw);
  const syaw = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);

  const x1 = p.x * cyaw - p.z * syaw;
  const z1 = p.x * syaw + p.z * cyaw;
  const y2 = p.y * cp - z1 * sp;
  const z2 = p.y * sp + z1 * cp;

  const camDist = 14;
  const persp = camDist / Math.max(camDist - z2, 2.5);
  return {
    sx: cx + x1 * scale * persp,
    sy: cy - y2 * scale * persp,
    depth: z2,
  };
}

function drawBox3D(ctx, minX, maxX, minY, maxY, minZ, maxZ, proj, opacity, strokeColor) {
  const v = [
    proj({ x: minX, y: minY, z: minZ }),
    proj({ x: maxX, y: minY, z: minZ }),
    proj({ x: maxX, y: maxY, z: minZ }),
    proj({ x: minX, y: maxY, z: minZ }),
    proj({ x: minX, y: minY, z: maxZ }),
    proj({ x: maxX, y: minY, z: maxZ }),
    proj({ x: maxX, y: maxY, z: maxZ }),
    proj({ x: minX, y: maxY, z: maxZ }),
  ];

  const faces = [
    [0, 1, 2, 3],
    [4, 5, 6, 7],
    [0, 1, 5, 4],
    [2, 3, 7, 6],
    [1, 2, 6, 5],
    [0, 3, 7, 4],
  ];

  if (opacity > 0.01) {
    ctx.fillStyle = `rgba(148, 163, 184, ${(opacity * 0.28).toFixed(3)})`;
    for (const f of faces) {
      ctx.beginPath();
      ctx.moveTo(v[f[0]].sx, v[f[0]].sy);
      for (let i = 1; i < 4; i++) ctx.lineTo(v[f[i]].sx, v[f[i]].sy);
      ctx.closePath();
      ctx.fill();
    }
  }

  const edges = [
    [0, 1], [1, 2], [2, 3], [3, 0],
    [4, 5], [5, 6], [6, 7], [7, 4],
    [0, 4], [1, 5], [2, 6], [3, 7],
  ];
  ctx.strokeStyle = strokeColor || `rgba(148, 163, 184, ${Math.max(0.22, opacity * 0.75).toFixed(2)})`;
  ctx.lineWidth = 1.05;
  ctx.beginPath();
  for (const [a, b] of edges) {
    ctx.moveTo(v[a].sx, v[a].sy);
    ctx.lineTo(v[b].sx, v[b].sy);
  }
  ctx.stroke();
}

function drawSeg3D(ctx, p1, p2, proj, color, lw) {
  const a = proj(p1);
  const b = proj(p2);
  ctx.strokeStyle = color;
  ctx.lineWidth = lw;
  ctx.beginPath();
  ctx.moveTo(a.sx, a.sy);
  ctx.lineTo(b.sx, b.sy);
  ctx.stroke();
}

function drawLoop3D(ctx, pts, proj, color, lw) {
  if (pts.length < 2) return;
  const pp = pts.map(proj);
  ctx.strokeStyle = color;
  ctx.lineWidth = lw;
  ctx.beginPath();
  ctx.moveTo(pp[0].sx, pp[0].sy);
  for (let i = 1; i < pp.length; i++) ctx.lineTo(pp[i].sx, pp[i].sy);
  ctx.closePath();
  ctx.stroke();
}

export default function Member3DDetailViewer({
  entity,
  data,
  height = 225,
  onPinSnapshot,
  onExpandModal,
  isModal = false,
  saving = false,
}) {
  const canvasRef = useRef(null);
  const dragRef = useRef({ active: false, x: 0, y: 0 });
  const [yaw, setYaw] = useState(-0.62);
  const [pitch, setPitch] = useState(0.38);
  const [zoom, setZoom] = useState(1.0);
  const [concreteOpacity, setConcreteOpacity] = useState(0.45);

  const summaryCaption = useMemo(() => {
    if (!entity || !data) return "";
    if (entity.type === "beam") {
      return `Beam M${data.member_id} (${data.section_id}) · ${data.width_mm}x${data.depth_mm}mm · L=${(data.length_m || 4).toFixed(2)}m · Top ${data.bars_top_start} / Bot ${data.bars_bot_mid} / ${data.stirrups_support}`;
    }
    if (entity.type === "column") {
      return `Column M${data.member_id} (${data.section_id}) · ${data.width_mm}x${data.depth_mm}mm · H=${(data.length_m || 3.2).toFixed(2)}m · ${data.main_bars} / ${data.ties_confined}`;
    }
    if (entity.type === "footing") {
      return `Footing ${data.footing_tag} (N${data.node_id}) · ${data.b_m.toFixed(2)}x${data.l_m.toFixed(2)}m x ${data.thickness_mm.toFixed(0)}mm · ${data.bottom_bars_x}`;
    }
    if (entity.type === "combined_footing") {
      return `Combined Footing ${data.combined_id} · ${data.length_l_m.toFixed(2)}x${data.width_b_m.toFixed(2)}m x ${data.thickness_mm.toFixed(0)}mm · Top ${data.top_long_bars} / Bot ${data.bot_long_bars}`;
    }
    return "";
  }, [entity, data]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !entity || !data) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(240, rect.width);
    const h = Math.max(height, rect.height || height);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#090e17";
    ctx.fillRect(0, 0, w, h);

    // Subtle CAD grid backdrop
    ctx.strokeStyle = "rgba(51, 65, 85, 0.20)";
    ctx.lineWidth = 1;
    for (let gx = 0; gx < w; gx += 24) {
      ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, h); ctx.stroke();
    }
    for (let gy = 0; gy < h; gy += 24) {
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
    }

    const cx = w * 0.5;
    const cy = h * 0.53;

    if (entity.type === "beam") {
      const L = Math.max(1.5, Math.min(9.0, Number(data.length_m) || 4.0));
      const bw = Math.max(0.18, (Number(data.width_mm) || 300) / 1000.0);
      const bh = Math.max(0.25, (Number(data.depth_mm) || 500) / 1000.0);
      const cov = 0.045;
      const scale = (Math.min(w, h) * 0.72 * zoom) / Math.max(L * 0.85, bh * 3.0);
      const proj = (pt) => projectPt(pt, yaw, pitch, scale, cx, cy);

      const topSup = parseRebarCallout(data.bars_top_start, { count: 3, diaMm: 16 });
      const botMid = parseRebarCallout(data.bars_bot_mid, { count: 3, diaMm: 16 });
      const stirSup = parseRebarCallout(data.stirrups_support, { diaMm: 10, spacingMm: 100 });
      const stirMid = parseRebarCallout(data.stirrups_midspan, { diaMm: 10, spacingMm: 200 });

      const hx = L * 0.5;
      const hy = bh * 0.5;
      const hz = bw * 0.5;
      const iy = Math.max(0.03, hy - cov);
      const iz = Math.max(0.03, hz - cov);

      // Closed seismic stirrups (tighter in 2h support zones, wider in midspan)
      const zone2h = Math.min(L * 0.28, 2.0 * bh);
      const sSupM = Math.max(0.08, stirSup.spacingMm / 1000.0);
      const sMidM = Math.max(0.12, stirMid.spacingMm / 1000.0);
      const stirXs = [];
      for (let x = -hx + 0.06; x <= -hx + zone2h; x += sSupM) stirXs.push(x);
      for (let x = -hx + zone2h + sMidM * 0.5; x < hx - zone2h; x += sMidM) stirXs.push(x);
      for (let x = hx - zone2h; x <= hx - 0.06; x += sSupM) stirXs.push(x);
      for (const sx of stirXs) {
        drawLoop3D(
          ctx,
          [
            { x: sx, y: -iy, z: -iz },
            { x: sx, y: iy, z: -iz },
            { x: sx, y: iy, z: iz },
            { x: sx, y: -iy, z: iz },
          ],
          proj,
          "#38bdf8",
          1.1
        );
      }

      // Continuous top bars (2 corner bars) + extra support top cut-off bars (L/3 each end)
      const nContTop = Math.min(2, topSup.count);
      const drawLongBars = (nBars, yPos, xStart, xEnd, color, lw) => {
        const n = Math.max(1, Math.min(nBars, 6));
        for (let i = 0; i < n; i++) {
          const z = n === 1 ? 0 : -iz + (i / (n - 1)) * (2 * iz);
          drawSeg3D(ctx, { x: xStart, y: yPos, z }, { x: xEnd, y: yPos, z }, proj, color, lw);
        }
      };
      drawLongBars(nContTop, iy, -hx + 0.04, hx - 0.04, "#4ade80", 2.0);
      if (topSup.count > 2) {
        const extra = topSup.count - 2;
        const cut = L / 3.0;
        for (let i = 0; i < Math.min(extra, 4); i++) {
          const frac = (i + 1) / (Math.min(extra, 4) + 1);
          const z = -iz + frac * (2 * iz);
          drawSeg3D(ctx, { x: -hx + 0.04, y: iy, z }, { x: -hx + cut, y: iy, z }, proj, "#facc15", 2.0);
          drawSeg3D(ctx, { x: hx - cut, y: iy, z }, { x: hx - 0.04, y: iy, z }, proj, "#facc15", 2.0);
        }
      }
      // Continuous bottom bars
      drawLongBars(botMid.count, -iy, -hx + 0.04, hx - 0.04, "#4ade80", 2.1);

      // Translucent concrete prism drawn over cage
      drawBox3D(ctx, -hx, hx, -hy, hy, -hz, hz, proj, concreteOpacity);
    } else if (entity.type === "column") {
      const H = Math.max(1.5, Math.min(6.0, Number(data.length_m) || 3.2));
      const bw = Math.max(0.22, (Number(data.width_mm) || 400) / 1000.0);
      const bh = Math.max(0.22, (Number(data.depth_mm) || 400) / 1000.0);
      const cov = 0.045;
      const scale = (Math.min(w, h) * 0.68 * zoom) / Math.max(H * 1.12, 2.5);
      const proj = (pt) => projectPt(pt, yaw, pitch, scale, cx, cy);

      const main = parseRebarCallout(data.main_bars, { count: 8, diaMm: 20 });
      const tieConf = parseRebarCallout(data.ties_confined, { diaMm: 10, spacingMm: 100 });
      const tieMid = parseRebarCallout(data.ties_mid, { diaMm: 10, spacingMm: 150 });

      const hx = bw * 0.5;
      const hy = H * 0.5;
      const hz = bh * 0.5;
      const ix = Math.max(0.03, hx - cov);
      const iz = Math.max(0.03, hz - cov);

      // End confinement ties (Lo) + mid-height ties
      const lo = Math.min(H * 0.25, Math.max(0.5, Math.max(bw, bh)));
      const sConf = Math.max(0.075, tieConf.spacingMm / 1000.0);
      const sMid = Math.max(0.12, tieMid.spacingMm / 1000.0);
      const tieYs = [];
      for (let y = -hy + 0.06; y <= -hy + lo; y += sConf) tieYs.push(y);
      for (let y = -hy + lo + sMid * 0.6; y < hy - lo; y += sMid) tieYs.push(y);
      for (let y = hy - lo; y <= hy - 0.06; y += sConf) tieYs.push(y);
      for (const ty of tieYs) {
        drawLoop3D(
          ctx,
          [
            { x: -ix, y: ty, z: -iz },
            { x: ix, y: ty, z: -iz },
            { x: ix, y: ty, z: iz },
            { x: -ix, y: ty, z: iz },
          ],
          proj,
          "#38bdf8",
          1.1
        );
      }

      // Perimeter longitudinal bars + top dowel projection (0.45 m)
      const pts = [
        [-ix, -iz], [ix, -iz], [ix, iz], [-ix, iz],
      ];
      if (main.count >= 8) pts.push([0, -iz], [0, iz], [-ix, 0], [ix, 0]);
      if (main.count >= 12) {
        pts.push([-ix * 0.35, -iz], [ix * 0.35, -iz], [-ix * 0.35, iz], [ix * 0.35, iz]);
      }
      for (const [bx, bz] of pts) {
        drawSeg3D(ctx, { x: bx, y: -hy, z: bz }, { x: bx, y: hy + 0.42, z: bz }, proj, "#4ade80", 2.0);
      }

      drawBox3D(ctx, -hx, hx, -hy, hy, -hz, hz, proj, concreteOpacity);
    } else if (entity.type === "footing" || entity.type === "combined_footing") {
      const isComb = entity.type === "combined_footing";
      const B = Math.max(1.0, Number(isComb ? data.length_l_m : data.b_m) || 1.8);
      const L = Math.max(1.0, Number(isComb ? data.width_b_m : data.l_m) || 1.8);
      const T = Math.max(0.25, (Number(data.thickness_mm) || 400) / 1000.0);
      const cov = 0.075;
      const scale = (Math.min(w, h) * 0.68 * zoom) / Math.max(B, L, 1.8);
      const proj = (pt) => projectPt(pt, yaw, pitch, scale, cx, cy + 12);

      const hx = B * 0.5;
      const hz = L * 0.5;
      const yBot = -T * 0.5;
      const yTop = T * 0.5;
      const yBarBot = yBot + cov;
      const hookH = Math.max(0.12, T - 2 * cov);

      const bxInfo = parseRebarCallout(isComb ? data.bot_long_bars : data.bottom_bars_x, { count: 7, spacingMm: 175 });
      const nX = Math.max(4, Math.min(10, Math.round(L / Math.max(0.12, bxInfo.spacingMm / 1000.0))));
      const nZ = Math.max(4, Math.min(10, Math.round(B / Math.max(0.12, bxInfo.spacingMm / 1000.0))));

      // Bottom orthogonal rebar mat with 90-degree upward hooks
      for (let i = 0; i < nX; i++) {
        const z = -(hz - cov) + (i / Math.max(nX - 1, 1)) * (2 * (hz - cov));
        drawSeg3D(ctx, { x: -hx + cov, y: yBarBot, z }, { x: hx - cov, y: yBarBot, z }, proj, "#4ade80", 1.7);
        drawSeg3D(ctx, { x: -hx + cov, y: yBarBot, z }, { x: -hx + cov, y: yBarBot + hookH, z }, proj, "#4ade80", 1.4);
        drawSeg3D(ctx, { x: hx - cov, y: yBarBot, z }, { x: hx - cov, y: yBarBot + hookH, z }, proj, "#4ade80", 1.4);
      }
      for (let i = 0; i < nZ; i++) {
        const x = -(hx - cov) + (i / Math.max(nZ - 1, 1)) * (2 * (hx - cov));
        drawSeg3D(ctx, { x, y: yBarBot + 0.02, z: -hz + cov }, { x, y: yBarBot + 0.02, z: hz - cov }, proj, "#38bdf8", 1.5);
      }

      // Top mat for combined footings
      if (isComb) {
        const yBarTop = yTop - cov;
        for (let i = 0; i < Math.min(nX, 6); i++) {
          const z = -(hz - cov) + (i / Math.max(Math.min(nX, 6) - 1, 1)) * (2 * (hz - cov));
          drawSeg3D(ctx, { x: -hx + cov, y: yBarTop, z }, { x: hx - cov, y: yBarTop, z }, proj, "#facc15", 1.5);
        }
      }

      // Column / pedestal stub(s) with dowels
      const stubXs = isComb ? [-hx * 0.45, hx * 0.45] : [0];
      for (const sx of stubXs) {
        const cw = 0.20;
        drawBox3D(ctx, sx - cw, sx + cw, yTop, yTop + 0.65, -cw, cw, proj, concreteOpacity, "rgba(34,211,238,0.65)");
        for (const [dx, dz] of [[-0.14, -0.14], [0.14, -0.14], [0.14, 0.14], [-0.14, 0.14]]) {
          drawSeg3D(ctx, { x: sx + dx, y: yBarBot + 0.03, z: dz }, { x: sx + dx, y: yTop + 0.85, z: dz }, proj, "#4ade80", 1.8);
        }
      }

      // Footing concrete pad
      drawBox3D(ctx, -hx, hx, yBot, yTop, -hz, hz, proj, concreteOpacity, "rgba(232,121,249,0.65)");
    }

    // HUD caption bar at bottom of canvas (included in PNG snapshot!)
    ctx.fillStyle = "rgba(9, 14, 23, 0.78)";
    ctx.fillRect(0, h - 22, w, 22);
    ctx.fillStyle = "#cbd5e1";
    ctx.font = "10px ui-monospace, SFMono-Regular, monospace";
    ctx.fillText(summaryCaption.slice(0, Math.floor(w / 6.2)), 8, h - 8);
  }, [entity, data, yaw, pitch, zoom, concreteOpacity, height, summaryCaption]);

  function applyPreset(p) {
    setZoom(1.0);
    if (p === "ISO") { setYaw(-0.62); setPitch(0.38); }
    else if (p === "Section") { setYaw(Math.PI * 0.5); setPitch(0.02); }
    else if (p === "Side") { setYaw(0.0); setPitch(0.02); }
  }

  function handlePin() {
    if (!canvasRef.current || !onPinSnapshot) return;
    const dataUrl = canvasRef.current.toDataURL("image/png");
    const title =
      entity.type === "beam"
        ? `3D Rebar Cage — Beam M${data.member_id} (${data.section_id})`
        : entity.type === "column"
        ? `3D Rebar Cage — Column M${data.member_id} (${data.section_id})`
        : entity.type === "combined_footing"
        ? `3D Rebar Cage — Combined Footing ${data.combined_id}`
        : `3D Rebar Cage — Footing ${data.footing_tag} (N${data.node_id})`;
    onPinSnapshot(title, summaryCaption, dataUrl);
  }

  return (
    <div className="m3d-wrap">
      <div className="m3d-toolbar">
        <div className="pdv-seg-group">
          {["ISO", "Section", "Side"].map((p) => (
            <button key={p} type="button" className="pdv-seg" onClick={() => applyPreset(p)}>
              {p}
            </button>
          ))}
        </div>
        <label className="m3d-opacity" title="Concrete opacity (rebar-only to solid)">
          <span>Conc</span>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={concreteOpacity}
            onChange={(e) => setConcreteOpacity(Number(e.target.value))}
          />
        </label>
        {!isModal && onExpandModal && (
          <button type="button" className="secondary mini" onClick={onExpandModal} title="Expand 3D Cage">
            ⤢
          </button>
        )}
      </div>

      <canvas
        ref={canvasRef}
        className="m3d-canvas"
        style={{ height: `${height}px` }}
        aria-label="3D member rebar cage viewer"
        onPointerDown={(e) => {
          dragRef.current = { active: true, x: e.clientX, y: e.clientY };
        }}
        onPointerMove={(e) => {
          if (!dragRef.current.active) return;
          const dx = e.clientX - dragRef.current.x;
          const dy = e.clientY - dragRef.current.y;
          dragRef.current = { active: true, x: e.clientX, y: e.clientY };
          setYaw((v) => v + dx * 0.012);
          setPitch((v) => Math.max(-1.45, Math.min(1.45, v + dy * 0.012)));
        }}
        onPointerUp={() => { dragRef.current.active = false; }}
        onPointerLeave={() => { dragRef.current.active = false; }}
        onWheel={(e) => {
          e.preventDefault();
          setZoom((z) => Math.max(0.5, Math.min(3.0, z * (e.deltaY < 0 ? 1.1 : 0.91))));
        }}
      />

      {onPinSnapshot && (
        <button
          type="button"
          className="pdv-pin-btn"
          disabled={saving}
          onClick={handlePin}
          style={{ width: "100%", marginTop: "6px" }}
        >
          {saving ? "Pinning..." : "Pin 3D Detail to OBO Report"}
        </button>
      )}
    </div>
  );
}
