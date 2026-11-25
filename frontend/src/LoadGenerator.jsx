import React, { useEffect, useMemo, useState } from "react";

const OCCUPANCY_PRESET_MAP = {
  residential: {
    label: "Residential (1.90 kPa)",
    sdl_finish_kpa: 1.2,
    sdl_ceiling_mep_kpa: 0.5,
    sdl_partitions_kpa: 1.0,
    live_load_kpa: 1.9,
    is_roof: false,
    exterior_wall_line_kn_m: 7.5,
    interior_wall_line_kn_m: 4.5,
    apply_exterior_wall_perimeter: true,
    apply_interior_wall_beams: false,
  },
  parking: {
    label: "Parking / Garage (2.40 kPa)",
    sdl_finish_kpa: 1.0,
    sdl_ceiling_mep_kpa: 0.5,
    sdl_partitions_kpa: 0.0,
    live_load_kpa: 2.4,
    is_roof: false,
    exterior_wall_line_kn_m: 7.5,
    interior_wall_line_kn_m: 4.5,
    apply_exterior_wall_perimeter: true,
    apply_interior_wall_beams: false,
  },
  corridor_stairs: {
    label: "Corridor / Stairs (4.80 kPa)",
    sdl_finish_kpa: 1.2,
    sdl_ceiling_mep_kpa: 0.5,
    sdl_partitions_kpa: 1.0,
    live_load_kpa: 4.8,
    is_roof: false,
    exterior_wall_line_kn_m: 7.5,
    interior_wall_line_kn_m: 4.5,
    apply_exterior_wall_perimeter: true,
    apply_interior_wall_beams: false,
  },
  roof_deck: {
    label: "Roof Deck (1.90 kPa)",
    sdl_finish_kpa: 1.2,
    sdl_ceiling_mep_kpa: 0.5,
    sdl_partitions_kpa: 0.0,
    live_load_kpa: 1.9,
    is_roof: false,
    exterior_wall_line_kn_m: 3.0,
    interior_wall_line_kn_m: 0.0,
    apply_exterior_wall_perimeter: true,
    apply_interior_wall_beams: false,
  },
  roof_non_accessible: {
    label: "Roof Non-Accessible (1.00 kPa LR)",
    sdl_finish_kpa: 0.5,
    sdl_ceiling_mep_kpa: 0.5,
    sdl_partitions_kpa: 0.0,
    live_load_kpa: 1.0,
    is_roof: true,
    exterior_wall_line_kn_m: 0.0,
    interior_wall_line_kn_m: 0.0,
    apply_exterior_wall_perimeter: false,
    apply_interior_wall_beams: false,
  },
  commercial: {
    label: "Commercial / Office (2.40 kPa)",
    sdl_finish_kpa: 1.2,
    sdl_ceiling_mep_kpa: 0.7,
    sdl_partitions_kpa: 1.0,
    live_load_kpa: 2.4,
    is_roof: false,
    exterior_wall_line_kn_m: 7.5,
    interior_wall_line_kn_m: 4.5,
    apply_exterior_wall_perimeter: true,
    apply_interior_wall_beams: false,
  },
  custom: {
    label: "Custom",
  },
};

const REGIONAL_PRESETS = [
  {
    id: "baguio",
    label: "Baguio Residential — Zone 4, 250 kph, SD",
    seismic: {
      zone: 4,
      soil_profile: "SD",
      occupancy_category: "IV_STANDARD",
      importance_factor_i: 1.0,
      seismic_source_type: "A",
      fault_distance_km: 5.0,
      r_factor_x: 8.5,
      r_factor_z: 8.5,
      ct_coeff: 0.0731,
      redundancy_rho: 1.0,
      include_vertical_ev: true,
      live_load_seismic_fraction: 0.0,
    },
    wind: {
      basic_wind_speed_kph: 250.0,
      exposure_category: "B",
      topographic_kzt: 1.0,
      directionality_kd: 0.85,
      gust_factor_g: 0.85,
      cp_windward: 0.8,
      cp_leeward_x: -0.5,
      cp_leeward_z: -0.5,
    },
  },
  {
    id: "manila",
    label: "Metro Manila — Zone 4, 250 kph, SD",
    seismic: {
      zone: 4,
      soil_profile: "SD",
      occupancy_category: "IV_STANDARD",
      importance_factor_i: 1.0,
      seismic_source_type: "A",
      fault_distance_km: 5.0,
      r_factor_x: 8.5,
      r_factor_z: 8.5,
      ct_coeff: 0.0731,
      redundancy_rho: 1.0,
      include_vertical_ev: true,
      live_load_seismic_fraction: 0.0,
    },
    wind: {
      basic_wind_speed_kph: 250.0,
      exposure_category: "B",
      topographic_kzt: 1.0,
      directionality_kd: 0.85,
      gust_factor_g: 0.85,
      cp_windward: 0.8,
      cp_leeward_x: -0.5,
      cp_leeward_z: -0.5,
    },
  },
  {
    id: "cebu",
    label: "Cebu / Visayas — Zone 4, 260 kph, SC",
    seismic: {
      zone: 4,
      soil_profile: "SC",
      occupancy_category: "IV_STANDARD",
      importance_factor_i: 1.0,
      seismic_source_type: "A",
      fault_distance_km: 10.0,
      r_factor_x: 8.5,
      r_factor_z: 8.5,
      ct_coeff: 0.0731,
      redundancy_rho: 1.0,
      include_vertical_ev: true,
      live_load_seismic_fraction: 0.0,
    },
    wind: {
      basic_wind_speed_kph: 260.0,
      exposure_category: "C",
      topographic_kzt: 1.0,
      directionality_kd: 0.85,
      gust_factor_g: 0.85,
      cp_windward: 0.8,
      cp_leeward_x: -0.5,
      cp_leeward_z: -0.5,
    },
  },
];

function computeBeamLoadForceKn(load, memberLength) {
  if (load.load_shape === "UNI") {
    const span = load.d2_m > load.d1_m ? load.d2_m - load.d1_m : memberLength;
    return Math.abs(load.w1_kn_m) * Math.max(span, 0);
  }
  const span = Math.max(load.d2_m - load.d1_m, 0);
  return 0.5 * (Math.abs(load.w1_kn_m) + Math.abs(load.w2_kn_m)) * span;
}

export default function LoadGenerator({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [stdPreview, setStdPreview] = useState(null);

  // Active workspace tab: gravity | seismic | wind | combos
  const [activeTab, setActiveTab] = useState("gravity");
  const [comboFilter, setComboFilter] = useState("ALL");
  const [comboSearch, setComboSearch] = useState("");

  // Editable draft states synced from project.load_generation
  const [floorLoads, setFloorLoads] = useState([]);
  const [seismic, setSeismic] = useState({
    zone: 4,
    soil_profile: "SD",
    occupancy_category: "IV_STANDARD",
    importance_factor_i: 1.0,
    seismic_source_type: "A",
    fault_distance_km: 5.0,
    r_factor_x: 8.5,
    r_factor_z: 8.5,
    ct_coeff: 0.0731,
    redundancy_rho: 1.0,
    include_vertical_ev: true,
    live_load_seismic_fraction: 0.0,
    staad_seismic_method: "ubc_auto",
  });
  const [wind, setWind] = useState({
    basic_wind_speed_kph: 250.0,
    exposure_category: "B",
    topographic_kzt: 1.0,
    directionality_kd: 0.85,
    gust_factor_g: 0.85,
    cp_windward: 0.8,
    cp_leeward_x: -0.5,
    cp_leeward_z: -0.5,
  });
  const [options, setOptions] = useState({
    include_rigid_diaphragm: true,
    use_repeat_load_combos: false,
    include_lrfd_combos: true,
    include_asd_combos: true,
    f1_live_load_factor: 0.5,
  });

  async function apiJson(url, opts) {
    const res = await fetch(url, opts);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return body;
  }

  function syncDrafts(lg) {
    if (!lg) return;
    setFloorLoads((lg.floor_loads || []).map((f) => ({ ...f })));
    if (lg.seismic) setSeismic({ ...lg.seismic });
    if (lg.wind) setWind({ ...lg.wind });
    if (lg.options) setOptions({ ...lg.options });
  }

  async function loadProjectState() {
    setBusy(true);
    setError("");
    try {
      const p = await apiJson(`/api/projects/${pid}`);
      setFullProject(p);
      syncDrafts(p.load_generation);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    loadProjectState();
  }, [project.project_id]);

  const lg = fullProject?.load_generation;
  const model = fullProject?.structural_model;
  const sizing = fullProject?.member_sizing;
  const hasPrereqs = Boolean(
    model?.nodes?.length && model?.members?.length && sizing?.member_assignments?.length
  );

  const memberById = useMemo(() => {
    const m = {};
    for (const mem of model?.members || []) m[mem.member_id] = mem;
    return m;
  }, [model?.members]);

  const gravityTotals = useMemo(() => {
    let dl = 0;
    let ll = 0;
    let lr = 0;
    let wall = 0;
    const loads = lg?.beam_loads || [];
    for (const ld of loads) {
      const len = memberById[ld.member_id]?.length_m || 0;
      const f = computeBeamLoadForceKn(ld, len);
      if (ld.load_case === "DL") {
        dl += f;
        if (ld.source === "ext_chb_wall" || ld.source === "int_chb_wall") wall += f;
      } else if (ld.load_case === "LL") {
        ll += f;
      } else if (ld.load_case === "LR") {
        lr += f;
      }
    }
    return { dl, ll, lr, wall, count: loads.length };
  }, [lg?.beam_loads, memberById]);

  const filteredCombos = useMemo(() => {
    const list = lg?.combinations || [];
    const q = comboSearch.trim().toLowerCase();
    return list.filter((c) => {
      if (comboFilter !== "ALL" && c.design_basis !== comboFilter) return false;
      if (q && !c.name.toLowerCase().includes(q) && !String(c.combo_id).includes(q)) return false;
      return true;
    });
  }, [lg?.combinations, comboFilter, comboSearch]);

  const validationIssues = lg?.validation_issues || [];
  const errorCount = validationIssues.filter((i) => i.severity === "error").length;
  const warningCount = validationIssues.filter((i) => i.severity === "warning").length;

  async function runGenerate(overridePayload = null, msg = "NSCP 2015 loads & combinations generated.") {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = overridePayload || {
        floor_loads: floorLoads.length > 0 ? floorLoads : null,
        seismic,
        wind,
        options,
      };
      const updatedLg = await apiJson(`/api/projects/${pid}/loads/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setFullProject((prev) => (prev ? { ...prev, load_generation: updatedLg } : prev));
      syncDrafts(updatedLg);
      setNotice(msg);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveConfig(partial, msg = "Configuration saved & loads recalculated.") {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updatedLg = await apiJson(`/api/projects/${pid}/loads/config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partial),
      });
      setFullProject((prev) => (prev ? { ...prev, load_generation: updatedLg } : prev));
      syncDrafts(updatedLg);
      setNotice(msg);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function applyRegionalPreset(preset) {
    const nextSeis = { ...seismic, ...preset.seismic };
    const nextWind = { ...wind, ...preset.wind };
    setSeismic(nextSeis);
    setWind(nextWind);
    await saveConfig(
      { seismic: nextSeis, wind: nextWind, options },
      `Applied preset: ${preset.label}`
    );
  }

  async function handleToggleOption(key, value) {
    const nextOpts = { ...options, [key]: value };
    setOptions(nextOpts);
    if (lg?.generated_at) {
      await saveConfig({ options: nextOpts }, "Load & export options updated.");
    }
  }

  async function handleApprove() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updatedLg = await apiJson(`/api/projects/${pid}/loads/approve`, {
        method: "POST",
      });
      setFullProject((prev) => (prev ? { ...prev, load_generation: updatedLg } : prev));
      setNotice("Engineer approval recorded for Module 05 NSCP 2015 loads.");
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handlePreviewStaad() {
    setBusy(true);
    setError("");
    try {
      const res = await apiJson(`/api/projects/${pid}/staad-preview`, {
        method: "POST",
      });
      setStdPreview(res);
      setNotice(`STAAD.Pro .std preview generated (${res.nodes} nodes, ${res.members} members).`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function handleDownloadStaad() {
    const url = `/api/projects/${pid}/staad/download`;
    const a = document.createElement("a");
    a.href = url;
    a.download = `${project.project_id}.std`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function updateFloorField(idx, field, value) {
    setFloorLoads((prev) =>
      prev.map((row, i) => {
        if (i !== idx) return row;
        if (field === "occupancy_preset" && OCCUPANCY_PRESET_MAP[value] && value !== "custom") {
          const { label, ...presetFields } = OCCUPANCY_PRESET_MAP[value];
          return { ...row, occupancy_preset: value, ...presetFields };
        }
        return { ...row, [field]: value };
      })
    );
  }

  const seisSum = lg?.seismic_summary;
  const windSum = lg?.wind_summary;
  const maxStoryFx = Math.max(
    1,
    ...(seisSum?.stories || []).map((s) => Math.max(s.fx_kn, s.fz_kn))
  );
  const maxWindFx = Math.max(
    1,
    ...(windSum?.stories || []).map((s) => Math.max(s.fx_kn, s.fz_kn))
  );

  const lrfdCount = (lg?.combinations || []).filter((c) => c.design_basis === "LRFD").length;
  const asdCount = (lg?.combinations || []).filter((c) => c.design_basis === "ASD").length;

  return (
    <section className="loads-builder">
      {/* Header */}
      <div className="geom-header">
        <div>
          <label>MODULE 05 · NSCP 2015 LOAD GENERATION &amp; STAAD.PRO EXPORT</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="geom-header-actions">
          <button disabled={busy || !hasPrereqs} onClick={() => runGenerate()}>
            {lg?.generated_at ? "Regenerate NSCP Loads" : "Generate NSCP Loads"}
          </button>
          <button
            className="secondary"
            disabled={busy || !lg?.generated_at || errorCount > 0}
            onClick={handleApprove}
          >
            {lg?.engineer_approved ? "Approved ✓" : "Approve Loads"}
          </button>
          <button className="secondary" disabled={busy} onClick={handlePreviewStaad}>
            Preview STAAD.Pro (.std)
          </button>
          <button
            className="secondary loads-download-btn"
            disabled={busy || !hasPrereqs}
            onClick={handleDownloadStaad}
          >
            Download .std
          </button>
          <button className="secondary" disabled={busy} onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {!hasPrereqs && fullProject && (
        <div className="pdf-error">
          Phase 3 geometry and Phase 4 member sizing must be generated before running Module 05.
          Open Module 03 &amp; Module 04 first.
        </div>
      )}
      {error && <div className="pdf-error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {/* Quick Preset & Export Options Bar */}
      <div className="geom-toolbar loads-preset-bar">
        <div className="loads-presets-row">
          <span className="loads-bar-label">Quick Presets:</span>
          {REGIONAL_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              className="secondary mini"
              disabled={busy || !hasPrereqs}
              onClick={() => applyRegionalPreset(p)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="loads-toggles-row">
          <label className="pdf-check">
            <input
              type="checkbox"
              checked={options.include_rigid_diaphragm}
              onChange={(e) => handleToggleOption("include_rigid_diaphragm", e.target.checked)}
            />
            Rigid Diaphragm
          </label>
          <label className="pdf-check">
            <input
              type="checkbox"
              checked={options.include_lrfd_combos}
              onChange={(e) => handleToggleOption("include_lrfd_combos", e.target.checked)}
            />
            LRFD Combos
          </label>
          <label className="pdf-check">
            <input
              type="checkbox"
              checked={options.include_asd_combos}
              onChange={(e) => handleToggleOption("include_asd_combos", e.target.checked)}
            />
            ASD Combos
          </label>
          <label className="pdf-check">
            <input
              type="checkbox"
              checked={options.use_repeat_load_combos}
              onChange={(e) => handleToggleOption("use_repeat_load_combos", e.target.checked)}
            />
            REPEAT LOAD (P-Delta)
          </label>
          <label className="loads-inline-field">
            <span>f1 Live:</span>
            <select
              value={String(options.f1_live_load_factor)}
              onChange={(e) =>
                handleToggleOption("f1_live_load_factor", parseFloat(e.target.value))
              }
            >
              <option value="0.5">0.5 (Res/Office)</option>
              <option value="1">1.0 (Garage/Public)</option>
            </select>
          </label>
        </div>
      </div>

      {/* Main Split Workspace */}
      <div className="geom-workspace">
        <div className="sizing-main-col">
          {/* 4 Workspace Tabs */}
          <div className="geom-floor-tabs loads-tabs">
            {[
              ["gravity", "1. Gravity & Wall Loads"],
              ["seismic", "2. Seismic (NSCP 208)"],
              ["wind", "3. Wind (NSCP 207)"],
              ["combos", `4. Load Combinations (${(lg?.combinations || []).length})`],
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

          {/* TAB 1: GRAVITY & WALL LOADS */}
          {activeTab === "gravity" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>Floor Gravity &amp; CHB Wall Loads (NSCP 2015 Table 205-1)</strong>
                  <small>
                    Tributary slab DL/SDL/LL/LR automatically deducts traced floor openings and
                    applies perimeter CHB line loads.
                  </small>
                </div>
                <button
                  disabled={busy || !hasPrereqs || floorLoads.length === 0}
                  onClick={() =>
                    saveConfig({ floor_loads: floorLoads }, "Floor gravity & wall loads updated.")
                  }
                >
                  Save Floor Loads &amp; Recalculate
                </button>
              </div>

              <div className="loads-kpi-strip">
                <div>
                  <small>Beam DL Reaction</small>
                  <strong>{gravityTotals.dl.toFixed(1)} kN</strong>
                </div>
                <div>
                  <small>CHB Wall DL</small>
                  <strong>{gravityTotals.wall.toFixed(1)} kN</strong>
                </div>
                <div>
                  <small>Floor Live (LL)</small>
                  <strong>{gravityTotals.ll.toFixed(1)} kN</strong>
                </div>
                <div>
                  <small>Roof Live (LR)</small>
                  <strong>{gravityTotals.lr.toFixed(1)} kN</strong>
                </div>
                <div>
                  <small>Beam Load Entries</small>
                  <strong>{gravityTotals.count}</strong>
                </div>
              </div>

              <div className="sizing-table-wrap">
                {floorLoads.length === 0 ? (
                  <div className="loads-empty">
                    Click <b>Generate NSCP Loads</b> to auto-seed floor load criteria from your
                    project levels.
                  </div>
                ) : (
                  <table className="sizing-table loads-edit-table">
                    <thead>
                      <tr>
                        <th>Floor Level</th>
                        <th>Occupancy Preset</th>
                        <th>Finish (kPa)</th>
                        <th>MEP/Ceil (kPa)</th>
                        <th>Part. (kPa)</th>
                        <th>Live (kPa)</th>
                        <th>Roof?</th>
                        <th>Ext Wall (kN/m)</th>
                        <th>Int Wall (kN/m)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {floorLoads.map((fl, idx) => (
                        <tr key={fl.floor_level}>
                          <td>
                            <b>{fl.floor_level}</b>
                          </td>
                          <td>
                            <select
                              value={fl.occupancy_preset}
                              onChange={(e) =>
                                updateFloorField(idx, "occupancy_preset", e.target.value)
                              }
                            >
                              {Object.entries(OCCUPANCY_PRESET_MAP).map(([k, v]) => (
                                <option key={k} value={k}>
                                  {v.label}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.1"
                              min="0"
                              value={fl.sdl_finish_kpa}
                              onChange={(e) =>
                                updateFloorField(
                                  idx,
                                  "sdl_finish_kpa",
                                  parseFloat(e.target.value) || 0
                                )
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.1"
                              min="0"
                              value={fl.sdl_ceiling_mep_kpa}
                              onChange={(e) =>
                                updateFloorField(
                                  idx,
                                  "sdl_ceiling_mep_kpa",
                                  parseFloat(e.target.value) || 0
                                )
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.1"
                              min="0"
                              value={fl.sdl_partitions_kpa}
                              onChange={(e) =>
                                updateFloorField(
                                  idx,
                                  "sdl_partitions_kpa",
                                  parseFloat(e.target.value) || 0
                                )
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.1"
                              min="0"
                              value={fl.live_load_kpa}
                              onChange={(e) =>
                                updateFloorField(
                                  idx,
                                  "live_load_kpa",
                                  parseFloat(e.target.value) || 0
                                )
                              }
                            />
                          </td>
                          <td>
                            <input
                              type="checkbox"
                              checked={fl.is_roof}
                              onChange={(e) => updateFloorField(idx, "is_roof", e.target.checked)}
                            />
                          </td>
                          <td>
                            <div className="loads-wall-cell">
                              <input
                                type="checkbox"
                                title="Apply exterior wall to perimeter primary beams"
                                checked={fl.apply_exterior_wall_perimeter}
                                onChange={(e) =>
                                  updateFloorField(
                                    idx,
                                    "apply_exterior_wall_perimeter",
                                    e.target.checked
                                  )
                                }
                              />
                              <input
                                type="number"
                                step="0.5"
                                min="0"
                                disabled={!fl.apply_exterior_wall_perimeter}
                                value={fl.exterior_wall_line_kn_m}
                                onChange={(e) =>
                                  updateFloorField(
                                    idx,
                                    "exterior_wall_line_kn_m",
                                    parseFloat(e.target.value) || 0
                                  )
                                }
                              />
                            </div>
                          </td>
                          <td>
                            <div className="loads-wall-cell">
                              <input
                                type="checkbox"
                                title="Apply interior wall to interior primary beams"
                                checked={fl.apply_interior_wall_beams}
                                onChange={(e) =>
                                  updateFloorField(
                                    idx,
                                    "apply_interior_wall_beams",
                                    e.target.checked
                                  )
                                }
                              />
                              <input
                                type="number"
                                step="0.5"
                                min="0"
                                disabled={!fl.apply_interior_wall_beams}
                                value={fl.interior_wall_line_kn_m}
                                onChange={(e) =>
                                  updateFloorField(
                                    idx,
                                    "interior_wall_line_kn_m",
                                    parseFloat(e.target.value) || 0
                                  )
                                }
                              />
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}

          {/* TAB 2: SEISMIC (NSCP 208) */}
          {activeTab === "seismic" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>NSCP 2015 Section 208 · Static Lateral Force Procedure</strong>
                  <small>
                    Interpolates Near-Source Na / Nv (Tables 208-5 &amp; 208-6) and Seismic
                    Coefficients Ca / Cv (Tables 208-7 &amp; 208-8).
                  </small>
                </div>
                <button
                  disabled={busy || !hasPrereqs}
                  onClick={() => saveConfig({ seismic }, "Seismic parameters updated & recalculated.")}
                >
                  Update Seismic &amp; Recalculate
                </button>
              </div>

              <div className="loads-form-grid">
                <label>
                  Seismic Zone
                  <select
                    value={String(seismic.zone)}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, zone: parseInt(e.target.value, 10) }))
                    }
                  >
                    <option value="4">Zone 4 (Z = 0.40)</option>
                    <option value="2">Zone 2 (Z = 0.20)</option>
                  </select>
                </label>
                <label>
                  Soil Profile
                  <select
                    value={seismic.soil_profile}
                    onChange={(e) => setSeismic((s) => ({ ...s, soil_profile: e.target.value }))}
                  >
                    <option value="SA">SA (Hard Rock)</option>
                    <option value="SB">SB (Rock)</option>
                    <option value="SC">SC (Very Dense Soil)</option>
                    <option value="SD">SD (Stiff Soil)</option>
                    <option value="SE">SE (Soft Soil)</option>
                  </select>
                </label>
                <label>
                  Seismic Source Type
                  <select
                    value={seismic.seismic_source_type}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, seismic_source_type: e.target.value }))
                    }
                  >
                    <option value="A">Type A (M &ge; 7.0)</option>
                    <option value="B">Type B (6.5 &le; M &lt; 7.0)</option>
                    <option value="C">Type C (M &lt; 6.5)</option>
                  </select>
                </label>
                <label>
                  Fault Distance (km)
                  <input
                    type="number"
                    step="0.5"
                    min="0"
                    value={seismic.fault_distance_km}
                    onChange={(e) =>
                      setSeismic((s) => ({
                        ...s,
                        fault_distance_km: parseFloat(e.target.value) || 0,
                      }))
                    }
                  />
                </label>
                <label>
                  Occupancy Category
                  <select
                    value={seismic.occupancy_category}
                    onChange={(e) => {
                      const cat = e.target.value;
                      const imp = cat === "I_ESSENTIAL" || cat === "II_HAZARDOUS" ? 1.5 : 1.0;
                      setSeismic((s) => ({
                        ...s,
                        occupancy_category: cat,
                        importance_factor_i: imp,
                      }));
                    }}
                  >
                    <option value="I_ESSENTIAL">I — Essential (I=1.5)</option>
                    <option value="II_HAZARDOUS">II — Hazardous (I=1.5)</option>
                    <option value="III_SPECIAL">III — Special (I=1.0)</option>
                    <option value="IV_STANDARD">IV — Standard (I=1.0)</option>
                    <option value="V_MISC">V — Misc (I=1.0)</option>
                  </select>
                </label>
                <label>
                  Importance Factor (I)
                  <input
                    type="number"
                    step="0.25"
                    min="0.5"
                    value={seismic.importance_factor_i}
                    onChange={(e) =>
                      setSeismic((s) => ({
                        ...s,
                        importance_factor_i: parseFloat(e.target.value) || 1.0,
                      }))
                    }
                  />
                </label>
                <label>
                  R Factor (X)
                  <input
                    type="number"
                    step="0.5"
                    min="1"
                    value={seismic.r_factor_x}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, r_factor_x: parseFloat(e.target.value) || 8.5 }))
                    }
                  />
                </label>
                <label>
                  R Factor (Z)
                  <input
                    type="number"
                    step="0.5"
                    min="1"
                    value={seismic.r_factor_z}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, r_factor_z: parseFloat(e.target.value) || 8.5 }))
                    }
                  />
                </label>
                <label>
                  Ct Coeff (m)
                  <input
                    type="number"
                    step="0.001"
                    min="0.01"
                    value={seismic.ct_coeff}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, ct_coeff: parseFloat(e.target.value) || 0.0731 }))
                    }
                  />
                </label>
                <label>
                  Redundancy (&rho;)
                  <input
                    type="number"
                    step="0.1"
                    min="1.0"
                    max="1.5"
                    value={seismic.redundancy_rho}
                    onChange={(e) =>
                      setSeismic((s) => ({
                        ...s,
                        redundancy_rho: parseFloat(e.target.value) || 1.0,
                      }))
                    }
                  />
                </label>
                <label>
                  Live Load Mass Fraction
                  <input
                    type="number"
                    step="0.05"
                    min="0"
                    max="1"
                    value={seismic.live_load_seismic_fraction}
                    onChange={(e) =>
                      setSeismic((s) => ({
                        ...s,
                        live_load_seismic_fraction: parseFloat(e.target.value) || 0,
                      }))
                    }
                  />
                </label>
                <label>
                  STAAD Seismic Method
                  <select
                    value={seismic.staad_seismic_method}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, staad_seismic_method: e.target.value }))
                    }
                  >
                    <option value="ubc_auto">UBC Auto (DEFINE UBC LOAD)</option>
                    <option value="explicit_story_forces">Explicit Story Forces (JOINT LOAD)</option>
                  </select>
                </label>
                <label className="pdf-check loads-check-field">
                  <input
                    type="checkbox"
                    checked={seismic.include_vertical_ev}
                    onChange={(e) =>
                      setSeismic((s) => ({ ...s, include_vertical_ev: e.target.checked }))
                    }
                  />
                  Include Vertical Ev = 0.5 Ca I D
                </label>
              </div>

              {seisSum && (
                <>
                  <div className="loads-kpi-strip">
                    <div>
                      <small>Na / Nv</small>
                      <strong>
                        {seisSum.na.toFixed(2)} / {seisSum.nv.toFixed(2)}
                      </strong>
                    </div>
                    <div>
                      <small>Ca / Cv</small>
                      <strong>
                        {seisSum.ca.toFixed(3)} / {seisSum.cv.toFixed(3)}
                      </strong>
                    </div>
                    <div>
                      <small>Period T (hn={seisSum.building_height_m.toFixed(1)}m)</small>
                      <strong>{seisSum.period_t_sec.toFixed(3)} s</strong>
                    </div>
                    <div>
                      <small>Total Weight W</small>
                      <strong>{seisSum.total_seismic_weight_kn.toFixed(1)} kN</strong>
                    </div>
                    <div>
                      <small>Base Shear Vx / Vz</small>
                      <strong>
                        {seisSum.v_design_x_kn.toFixed(1)} / {seisSum.v_design_z_kn.toFixed(1)} kN
                      </strong>
                    </div>
                    <div>
                      <small>V / W Ratio</small>
                      <strong>{(seisSum.base_shear_ratio_x * 100).toFixed(2)}%</strong>
                    </div>
                  </div>

                  <div className="sizing-table-wrap">
                    <table className="sizing-table">
                      <thead>
                        <tr>
                          <th>Story Level</th>
                          <th>Elev (m)</th>
                          <th>Weight Wi (kN)</th>
                          <th>Fx (kN)</th>
                          <th>Fz (kN)</th>
                          <th>Cum. Shear Vx (kN)</th>
                          <th>Story Force Profile (Fx)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...seisSum.stories].reverse().map((st) => (
                          <tr key={st.floor_level}>
                            <td>
                              <b>{st.floor_level}</b>
                            </td>
                            <td>{st.elevation_m.toFixed(2)}</td>
                            <td>{st.weight_kn.toFixed(1)}</td>
                            <td>{st.fx_kn.toFixed(2)}</td>
                            <td>{st.fz_kn.toFixed(2)}</td>
                            <td>{st.cumulative_shear_x_kn.toFixed(1)}</td>
                            <td className="loads-bar-cell">
                              <div className="loads-bar-track">
                                <div
                                  className="loads-bar-fill seismic"
                                  style={{
                                    width: `${Math.min(100, (st.fx_kn / maxStoryFx) * 100)}%`,
                                  }}
                                />
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          )}

          {/* TAB 3: WIND (NSCP 207) */}
          {activeTab === "wind" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>NSCP 2015 Section 207 · MWFRS Directional Wind Procedure</strong>
                  <small>
                    Computes Kz per Table 207B.3-1, velocity pressure qz (kPa), and story lateral
                    wind forces Fx / Fz.
                  </small>
                </div>
                <button
                  disabled={busy || !hasPrereqs}
                  onClick={() => saveConfig({ wind }, "Wind parameters updated & recalculated.")}
                >
                  Update Wind &amp; Recalculate
                </button>
              </div>

              <div className="loads-form-grid">
                <label>
                  Basic Wind Speed (kph)
                  <input
                    type="number"
                    step="10"
                    min="50"
                    value={wind.basic_wind_speed_kph}
                    onChange={(e) =>
                      setWind((w) => ({
                        ...w,
                        basic_wind_speed_kph: parseFloat(e.target.value) || 250,
                      }))
                    }
                  />
                </label>
                <label>
                  Exposure Category
                  <select
                    value={wind.exposure_category}
                    onChange={(e) => setWind((w) => ({ ...w, exposure_category: e.target.value }))}
                  >
                    <option value="B">Exposure B (Urban / Suburban)</option>
                    <option value="C">Exposure C (Open Terrain)</option>
                    <option value="D">Exposure D (Flat / Coastal)</option>
                  </select>
                </label>
                <label>
                  Topographic Kzt
                  <input
                    type="number"
                    step="0.05"
                    min="1.0"
                    value={wind.topographic_kzt}
                    onChange={(e) =>
                      setWind((w) => ({
                        ...w,
                        topographic_kzt: parseFloat(e.target.value) || 1.0,
                      }))
                    }
                  />
                </label>
                <label>
                  Directionality Kd
                  <input
                    type="number"
                    step="0.05"
                    min="0.5"
                    max="1.0"
                    value={wind.directionality_kd}
                    onChange={(e) =>
                      setWind((w) => ({
                        ...w,
                        directionality_kd: parseFloat(e.target.value) || 0.85,
                      }))
                    }
                  />
                </label>
                <label>
                  Gust Factor G
                  <input
                    type="number"
                    step="0.05"
                    min="0.5"
                    value={wind.gust_factor_g}
                    onChange={(e) =>
                      setWind((w) => ({
                        ...w,
                        gust_factor_g: parseFloat(e.target.value) || 0.85,
                      }))
                    }
                  />
                </label>
                <label>
                  Cp Windward
                  <input
                    type="number"
                    step="0.05"
                    value={wind.cp_windward}
                    onChange={(e) =>
                      setWind((w) => ({ ...w, cp_windward: parseFloat(e.target.value) || 0.8 }))
                    }
                  />
                </label>
                <label>
                  Cp Leeward (X)
                  <input
                    type="number"
                    step="0.05"
                    value={wind.cp_leeward_x}
                    onChange={(e) =>
                      setWind((w) => ({ ...w, cp_leeward_x: parseFloat(e.target.value) || -0.5 }))
                    }
                  />
                </label>
                <label>
                  Cp Leeward (Z)
                  <input
                    type="number"
                    step="0.05"
                    value={wind.cp_leeward_z}
                    onChange={(e) =>
                      setWind((w) => ({ ...w, cp_leeward_z: parseFloat(e.target.value) || -0.5 }))
                    }
                  />
                </label>
              </div>

              {windSum && (
                <div className="sizing-table-wrap">
                  <table className="sizing-table">
                    <thead>
                      <tr>
                        <th>Story Level</th>
                        <th>Elev (m)</th>
                        <th>Kz</th>
                        <th>qz (kPa)</th>
                        <th>Wind Fx (kN)</th>
                        <th>Wind Fz (kN)</th>
                        <th>Lateral Profile (Fx)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...windSum.stories].reverse().map((st) => (
                        <tr key={st.floor_level}>
                          <td>
                            <b>{st.floor_level}</b>
                          </td>
                          <td>{st.elevation_m.toFixed(2)}</td>
                          <td>{st.kz.toFixed(3)}</td>
                          <td>{st.qz_kpa.toFixed(3)}</td>
                          <td>{st.fx_kn.toFixed(2)}</td>
                          <td>{st.fz_kn.toFixed(2)}</td>
                          <td className="loads-bar-cell">
                            <div className="loads-bar-track">
                              <div
                                className="loads-bar-fill wind"
                                style={{
                                  width: `${Math.min(100, (st.fx_kn / maxWindFx) * 100)}%`,
                                }}
                              />
                            </div>
                          </td>
                        </tr>
                      ))}
                      <tr className="loads-total-row">
                        <td colSpan={4}>
                          <b>Total Wind Base Shear</b>
                        </td>
                        <td>
                          <b>{windSum.base_shear_x_kn.toFixed(2)} kN</b>
                        </td>
                        <td>
                          <b>{windSum.base_shear_z_kn.toFixed(2)} kN</b>
                        </td>
                        <td />
                      </tr>
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* TAB 4: LOAD COMBINATIONS */}
          {activeTab === "combos" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>NSCP 2015 Load Combinations (Sec. 203.3.1 LRFD &amp; Sec. 203.4.2 ASD)</strong>
                  <small>
                    Primary STAAD Cases: 1–4 Seismic (EX±/EZ±), 5–8 Wind (WX±/WZ±), 9 DL, 10 LL, 11 LR.
                  </small>
                </div>
                <div className="geom-floor-tabs">
                  {["ALL", "LRFD", "ASD"].map((b) => (
                    <button
                      key={b}
                      type="button"
                      className={`mini ${comboFilter === b ? "" : "secondary"}`}
                      onClick={() => setComboFilter(b)}
                    >
                      {b}
                    </button>
                  ))}
                  <input
                    className="loads-search"
                    type="text"
                    placeholder="Filter combos..."
                    value={comboSearch}
                    onChange={(e) => setComboSearch(e.target.value)}
                  />
                </div>
              </div>

              <div className="sizing-table-wrap">
                <table className="sizing-table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Basis</th>
                      <th>Combination Name</th>
                      <th>Factored Formula</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredCombos.map((c) => (
                      <tr key={c.combo_id}>
                        <td>
                          <code>{c.combo_id}</code>
                        </td>
                        <td>
                          <span
                            className={`sizing-tag-pill ${
                              c.design_basis === "LRFD" ? "loads-pill-lrfd" : "loads-pill-asd"
                            }`}
                          >
                            {c.design_basis}
                          </span>
                        </td>
                        <td>{c.name}</td>
                        <td>
                          <div className="loads-factor-pills">
                            {Object.entries(c.factors || {}).map(([k, v]) => (
                              <code key={k}>
                                {Number(v).toFixed(3).replace(/\.?0+$/, "")}·{k}
                              </code>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ))}
                    {filteredCombos.length === 0 && (
                      <tr>
                        <td colSpan={4} className="loads-empty">
                          No combinations match the current filter.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        {/* Right Sidebar */}
        <aside className="geom-sidebar">
          <div className="geom-panel">
            <div className="geom-panel-head">
              <div>
                <strong>NSCP 2015 Load Summary</strong>
                <small>
                  {lg?.generated_at
                    ? `Updated ${new Date(lg.generated_at).toLocaleTimeString()}`
                    : "Not yet generated"}
                </small>
              </div>
            </div>
            <div style={{ padding: "10px 12px" }}>
              <div className="geom-stats-grid loads-kpi-sidebar">
                <div>
                  <strong>{seisSum ? `${seisSum.total_seismic_weight_kn.toFixed(0)}` : "—"}</strong>
                  <small>Weight W (kN)</small>
                </div>
                <div>
                  <strong>{seisSum ? `${seisSum.v_design_x_kn.toFixed(0)}` : "—"}</strong>
                  <small>Seismic Vx (kN)</small>
                </div>
                <div>
                  <strong>{seisSum ? `${seisSum.v_design_z_kn.toFixed(0)}` : "—"}</strong>
                  <small>Seismic Vz (kN)</small>
                </div>
                <div>
                  <strong>{windSum ? `${windSum.base_shear_x_kn.toFixed(0)}` : "—"}</strong>
                  <small>Wind Wx (kN)</small>
                </div>
                <div>
                  <strong>{windSum ? `${windSum.base_shear_z_kn.toFixed(0)}` : "—"}</strong>
                  <small>Wind Wz (kN)</small>
                </div>
                <div>
                  <strong>{lrfdCount + asdCount}</strong>
                  <small>
                    Combos ({lrfdCount}/{asdCount})
                  </small>
                </div>
              </div>
            </div>
          </div>

          <div className="geom-panel">
            <div className="geom-panel-head">
              <div className="geom-checks-head" style={{ width: "100%" }}>
                <h4>NSCP 2015 Load Checks</h4>
                <small>
                  {errorCount} err · {warningCount} warn
                </small>
              </div>
            </div>
            <div style={{ padding: "10px 12px", display: "flex", flexDirection: "column", gap: "8px" }}>
              {validationIssues.length === 0 ? (
                <div className="geom-issue">
                  <div className="geom-issue-top">
                    <span className="geom-sev-badge sev-info">PASS</span>
                    <code>READY</code>
                  </div>
                  <p>
                    {lg?.generated_at
                      ? "All NSCP 2015 load checks passed. Ready for engineer approval & STAAD.Pro export."
                      : "Generate NSCP 2015 loads to run code validation checks."}
                  </p>
                </div>
              ) : (
                validationIssues.map((iss, idx) => (
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
          </div>
        </aside>
      </div>

      {/* STAAD.Pro Preview Drawer */}
      {stdPreview && (
        <div className="geom-std-drawer">
          <div className="geom-panel-head">
            <div>
              <strong>STAAD.Pro Full Input Preview ({stdPreview.source})</strong>
              <small>
                {stdPreview.path} · {stdPreview.nodes} nodes · {stdPreview.members} members
              </small>
            </div>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                className="secondary mini"
                onClick={() => {
                  navigator.clipboard?.writeText(stdPreview.std_preview || "");
                  setNotice("Copied full STAAD.Pro .std input to clipboard.");
                }}
              >
                Copy .std
              </button>
              <button className="secondary mini" onClick={handleDownloadStaad}>
                Download .std
              </button>
              <button className="secondary mini" onClick={() => setStdPreview(null)}>
                Close Preview
              </button>
            </div>
          </div>
          <pre className="geom-std-code">{stdPreview.std_preview}</pre>
        </div>
      )}
    </section>
  );
}
