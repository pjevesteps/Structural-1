import React, { useEffect, useMemo, useState } from "react";

const DEFAULT_SIGNATORY = {
  engineer_name: "",
  title: "Civil / Structural Engineer",
  prc_license_no: "",
  prc_validity: "",
  ptr_no: "",
  ptr_date: "",
  ptr_place: "",
  tin: "",
  project_owner: "",
  project_address: "",
  geotech_report_ref:
    "Pending geotechnical / soil boring report (required for >= 3 storeys per NBCP Sec. 302-5)",
};

const DEFAULT_BOM_PARAMS = {
  region_preset: "car_baguio",
  concrete_waste_pct: 5.0,
  rebar_waste_splice_pct: 12.0,
  commercial_bar_length_m: 6.0,
  tie_wire_pct_of_rebar: 1.5,
  rate_concrete_php_m3: 6500.0,
  rate_rebar_php_kg: 52.0,
  rate_tie_wire_php_kg: 95.0,
  rate_formwork_php_m2: 650.0,
  rate_chb_wall_php_m2: 950.0,
  labor_pct_of_materials: 35.0,
  labor_concrete_pct: null,
  labor_rebar_pct: null,
  labor_formwork_pct: null,
  include_chb_masonry: true,
};

const DEFAULT_OPTIONS = {
  include_load_combinations: true,
  include_full_member_schedules: true,
  include_bom_cost_estimate: true,
  include_staad_snippet: false,
  report_revision: "Rev. 0 — For Building Permit Submission",
};

function fmtNum(val, dec = 2) {
  const n = Number(val || 0);
  return n.toLocaleString("en-PH", {
    minimumFractionDigits: dec,
    maximumFractionDigits: dec,
  });
}

function fmtPhp(val) {
  return `₱${fmtNum(val, 2)}`;
}

export default function ReportBuilder({ project, onClose, onUpdated }) {
  const pid = encodeURIComponent(project.project_id);

  const [fullProject, setFullProject] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState("setup"); // setup | bom | preview

  const [signatory, setSignatory] = useState(DEFAULT_SIGNATORY);
  const [bomParams, setBomParams] = useState(DEFAULT_BOM_PARAMS);
  const [options, setOptions] = useState(DEFAULT_OPTIONS);
  const [regionalPresets, setRegionalPresets] = useState([]);
  const [compareRevId, setCompareRevId] = useState("");
  const [revDelta, setRevDelta] = useState(null);

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
      const [p, presetRes] = await Promise.all([
        apiJson(`/api/projects/${pid}`),
        apiJson("/api/reports/cost-presets").catch(() => ({ presets: [] })),
      ]);
      setFullProject(p);
      setRegionalPresets(presetRes.presets || []);
      if (p.reporting?.signatory) {
        const sig = { ...DEFAULT_SIGNATORY, ...p.reporting.signatory };
        if (!sig.project_address && p.project_info?.location) {
          sig.project_address = p.project_info.location;
        }
        setSignatory((prev) => {
          const merged = { ...sig };
          for (const key of Object.keys(merged)) {
            if (prev[key] && prev[key] !== DEFAULT_SIGNATORY[key]) {
              merged[key] = prev[key];
            }
          }
          return merged;
        });
      } else if (p.project_info?.location) {
        setSignatory((prev) => ({
          ...prev,
          project_address: prev.project_address || p.project_info.location,
        }));
      }
      if (p.reporting?.bom_params) {
        setBomParams({ ...DEFAULT_BOM_PARAMS, ...p.reporting.bom_params });
      }
      if (p.reporting?.options) {
        setOptions({ ...DEFAULT_OPTIONS, ...p.reporting.options });
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
  const rep = fullProject?.reporting;
  const bom = rep?.bom || null;

  const hasPrereqs = Boolean(
    model?.nodes?.length &&
      model?.members?.length &&
      sizing?.member_assignments?.length &&
      ad?.generated_at
  );

  const designStats = useMemo(() => {
    const beams = ad?.beam_designs || [];
    const cols = ad?.column_designs || [];
    const ftgs = ad?.footing_designs || [];
    const drifts = ad?.story_drifts || [];
    return {
      beamPass: beams.filter((b) => b.status === "PASS").length,
      beamTotal: beams.length,
      colPass: cols.filter((c) => c.status === "PASS").length,
      colTotal: cols.length,
      ftgPass: ftgs.filter((f) => f.status === "PASS").length,
      ftgTotal: ftgs.length,
      driftPass: drifts.filter((d) => d.status === "PASS").length,
      driftTotal: drifts.length,
      maxBeamDcr: beams.reduce((acc, b) => Math.max(acc, b.governing_dcr), 0),
      maxColDcr: cols.reduce((acc, c) => Math.max(acc, c.governing_dcr), 0),
      maxFtgDcr: ftgs.reduce((acc, f) => Math.max(acc, f.governing_dcr), 0),
    };
  }, [ad]);

  async function handleGenerate(switchTab = null, overrideBomParams = null) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const activeBp = overrideBomParams || bomParams;
      const payload = {
        signatory,
        bom_params: {
          ...activeBp,
          commercial_bar_length_m: Number(activeBp.commercial_bar_length_m),
          labor_concrete_pct:
            activeBp.labor_concrete_pct === "" || activeBp.labor_concrete_pct == null
              ? null
              : Number(activeBp.labor_concrete_pct),
          labor_rebar_pct:
            activeBp.labor_rebar_pct === "" || activeBp.labor_rebar_pct == null
              ? null
              : Number(activeBp.labor_rebar_pct),
          labor_formwork_pct:
            activeBp.labor_formwork_pct === "" || activeBp.labor_formwork_pct == null
              ? null
              : Number(activeBp.labor_formwork_pct),
        },
        options,
      };
      const updated = await apiJson(`/api/projects/${pid}/reports/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setFullProject((prev) => (prev ? { ...prev, reporting: updated } : prev));
      setNotice(
        `Calculation report & BOM generated: ${fmtNum(updated.bom?.total_concrete_gross_m3, 2)} m³ concrete, ` +
          `${fmtNum(updated.bom?.total_rebar_kg, 0)} kg rebar, Est. ${fmtPhp(updated.bom?.grand_total_cost_php)}.`
      );
      if (compareRevId) {
        const delta = await apiJson(
          `/api/projects/${pid}/revisions/compare?base_rev=${encodeURIComponent(compareRevId)}`
        ).catch(() => null);
        setRevDelta(delta);
      }
      if (switchTab) setActiveTab(switchTab);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function handleSelectRegionPreset(presetKey) {
    if (presetKey === "custom") {
      setBomParams({ ...bomParams, region_preset: "custom" });
      return;
    }
    const found = regionalPresets.find((p) => p.key === presetKey);
    if (!found) {
      setBomParams({ ...bomParams, region_preset: presetKey });
      return;
    }
    const next = {
      ...bomParams,
      region_preset: presetKey,
      rate_concrete_php_m3: found.rate_concrete_php_m3,
      rate_rebar_php_kg: found.rate_rebar_php_kg,
      rate_tie_wire_php_kg: found.rate_tie_wire_php_kg,
      rate_formwork_php_m2: found.rate_formwork_php_m2,
      rate_chb_wall_php_m2: found.rate_chb_wall_php_m2,
      labor_pct_of_materials: found.labor_pct_of_materials,
      labor_concrete_pct: found.labor_concrete_pct,
      labor_rebar_pct: found.labor_rebar_pct,
      labor_formwork_pct: found.labor_formwork_pct,
    };
    setBomParams(next);
    if (hasPrereqs) {
      handleGenerate(null, next);
    }
  }

  async function handleCompareRevision(revId) {
    setCompareRevId(revId);
    if (!revId) {
      setRevDelta(null);
      return;
    }
    try {
      const delta = await apiJson(
        `/api/projects/${pid}/revisions/compare?base_rev=${encodeURIComponent(revId)}`
      );
      setRevDelta(delta);
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleQuickSaveRevision() {
    setBusy(true);
    setError("");
    try {
      const nextNum = (fullProject?.revisions || []).length;
      const snap = await apiJson(`/api/projects/${pid}/revisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: `Rev. ${nextNum} — ${options.report_revision || "BOM Snapshot"}`,
          notes: `Region: ${bomParams.region_preset}`,
        }),
      });
      setFullProject((prev) =>
        prev ? { ...prev, revisions: [...(prev.revisions || []), snap] } : prev
      );
      setNotice(`Saved ${snap.revision_id} (${snap.label}) for future BOM delta comparisons.`);
      if (onUpdated) await onUpdated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function triggerDownload(endpoint, filename) {
    const a = document.createElement("a");
    a.href = `/api/projects/${pid}/reports/${endpoint}`;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  const warnings = rep?.readiness_warnings || [];
  const checklistItems = useMemo(() => {
    const allDesignPass =
      designStats.beamPass === designStats.beamTotal &&
      designStats.colPass === designStats.colTotal &&
      designStats.ftgPass === designStats.ftgTotal &&
      designStats.driftPass === designStats.driftTotal &&
      designStats.beamTotal > 0;
    const hasSig = Boolean(signatory.engineer_name.trim() && signatory.prc_license_no.trim());
    const elevatedCount = Math.max(0, (fullProject?.levels?.length || 1) - 1);
    const geotechOk =
      elevatedCount < 3 ||
      (Boolean(signatory.geotech_report_ref.trim()) &&
        !signatory.geotech_report_ref.toLowerCase().includes("pending"));

    return [
      { label: "Phases 3, 4 & 6 Design Data Populated", ok: hasPrereqs },
      { label: "BOM & NBCP Sec. 302-5f Cost Estimate Computed", ok: Boolean(bom) },
      { label: "All Drift, Beam, Column & Footing Checks PASS", ok: allDesignPass },
      { label: "Engineer Name & PRC License Entered", ok: hasSig },
      { label: "NBCP 302-5 Geotechnical Requirement Met", ok: geotechOk },
    ];
  }, [hasPrereqs, bom, designStats, signatory, fullProject?.levels]);

  return (
    <section className="report-builder">
      {/* Header */}
      <div className="geom-header">
        <div>
          <label>MODULE 08 · CALCULATION REPORTS (DOCX &amp; PDF) &amp; STRUCTURAL BOM</label>
          <h2>
            {project.project_name} <small>({project.project_id})</small>
          </h2>
        </div>
        <div className="geom-header-actions">
          <button disabled={busy || !hasPrereqs} onClick={() => handleGenerate()}>
            {rep?.generated_at ? "Regenerate Report & BOM" : "Generate Report & BOM"}
          </button>
          <button
            className="secondary report-docx-btn"
            disabled={busy || !hasPrereqs}
            onClick={() =>
              triggerDownload("download/docx", `${project.project_id}_Structural_Calculation_Report.docx`)
            }
          >
            Download DOCX
          </button>
          <button
            className="secondary report-pdf-btn"
            disabled={busy || !hasPrereqs}
            onClick={() =>
              triggerDownload("download/pdf", `${project.project_id}_Structural_Calculation_Report.pdf`)
            }
          >
            Download PDF
          </button>
          <button
            className="secondary loads-download-btn"
            disabled={busy || !hasPrereqs}
            onClick={() => triggerDownload("download/bom-csv", `${project.project_id}_Structural_BOM.csv`)}
          >
            Export BOM (CSV)
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
          before generating Calculation Reports &amp; BOM.
        </div>
      )}

      {/* Workspace Grid */}
      <div className="geom-workspace">
        <div className="sizing-main-col">
          {/* Tabs */}
          <div className="geom-floor-tabs loads-tabs">
            <button
              className={activeTab === "setup" ? "pdf-chip active" : "pdf-chip"}
              onClick={() => setActiveTab("setup")}
            >
              1. Report Setup &amp; Signatory
            </button>
            <button
              className={activeTab === "bom" ? "pdf-chip active" : "pdf-chip"}
              onClick={() => setActiveTab("bom")}
            >
              2. Bill of Materials &amp; Cost Estimate (PHP)
            </button>
            <button
              className={activeTab === "preview" ? "pdf-chip active" : "pdf-chip"}
              onClick={() => setActiveTab("preview")}
            >
              3. Report Preview
            </button>
          </div>

          {/* TAB 1: REPORT SETUP & SIGNATORY */}
          {activeTab === "setup" && (
            <div className="geom-panel">
              <div className="geom-panel-head">
                <div>
                  <strong>Engineer Sign &amp; Seal Block (NBCP PD 1096 / OBO Permit)</strong>
                  <small>
                    Populates the Cover Page and Section 7 Professional Certification in DOCX &amp; PDF exports.
                  </small>
                </div>
                <button
                  className="secondary"
                  disabled={busy || !hasPrereqs}
                  onClick={() => handleGenerate("bom")}
                >
                  Save &amp; Compute BOM →
                </button>
              </div>

              <div className="report-form-grid">
                <label>
                  <span>Engineer Name (Full)</span>
                  <input
                    type="text"
                    aria-label="Engineer Name"
                    placeholder="Engr. Juan Dela Cruz, CE"
                    value={signatory.engineer_name}
                    onChange={(e) => setSignatory({ ...signatory, engineer_name: e.target.value })}
                  />
                </label>
                <label>
                  <span>Title / Designation</span>
                  <input
                    type="text"
                    aria-label="Engineer Title"
                    value={signatory.title}
                    onChange={(e) => setSignatory({ ...signatory, title: e.target.value })}
                  />
                </label>
                <label>
                  <span>PRC License No.</span>
                  <input
                    type="text"
                    aria-label="PRC License No."
                    placeholder="0123456"
                    value={signatory.prc_license_no}
                    onChange={(e) => setSignatory({ ...signatory, prc_license_no: e.target.value })}
                  />
                </label>
                <label>
                  <span>PRC Validity Date</span>
                  <input
                    type="text"
                    aria-label="PRC Validity"
                    placeholder="2028-12-31"
                    value={signatory.prc_validity}
                    onChange={(e) => setSignatory({ ...signatory, prc_validity: e.target.value })}
                  />
                </label>
                <label>
                  <span>PTR No.</span>
                  <input
                    type="text"
                    aria-label="PTR No."
                    placeholder="PTR-987654"
                    value={signatory.ptr_no}
                    onChange={(e) => setSignatory({ ...signatory, ptr_no: e.target.value })}
                  />
                </label>
                <label>
                  <span>PTR Date Issued</span>
                  <input
                    type="text"
                    aria-label="PTR Date"
                    placeholder="2026-01-15"
                    value={signatory.ptr_date}
                    onChange={(e) => setSignatory({ ...signatory, ptr_date: e.target.value })}
                  />
                </label>
                <label>
                  <span>PTR Place Issued</span>
                  <input
                    type="text"
                    aria-label="PTR Place"
                    placeholder="Baguio City"
                    value={signatory.ptr_place}
                    onChange={(e) => setSignatory({ ...signatory, ptr_place: e.target.value })}
                  />
                </label>
                <label>
                  <span>TIN</span>
                  <input
                    type="text"
                    aria-label="TIN"
                    placeholder="123-456-789-000"
                    value={signatory.tin}
                    onChange={(e) => setSignatory({ ...signatory, tin: e.target.value })}
                  />
                </label>
                <label>
                  <span>Project Owner / Client</span>
                  <input
                    type="text"
                    aria-label="Project Owner"
                    placeholder="Mr. & Mrs. Santos"
                    value={signatory.project_owner}
                    onChange={(e) => setSignatory({ ...signatory, project_owner: e.target.value })}
                  />
                </label>
                <label>
                  <span>Project Address</span>
                  <input
                    type="text"
                    aria-label="Project Address"
                    value={signatory.project_address}
                    onChange={(e) => setSignatory({ ...signatory, project_address: e.target.value })}
                  />
                </label>
                <label className="report-span-2">
                  <span>Geotechnical / Soil Boring Report Reference (NBCP Sec. 302-5)</span>
                  <input
                    type="text"
                    aria-label="Geotechnical Report Reference"
                    value={signatory.geotech_report_ref}
                    onChange={(e) =>
                      setSignatory({ ...signatory, geotech_report_ref: e.target.value })
                    }
                  />
                </label>
                <label className="report-span-2">
                  <span>Report Revision Label</span>
                  <input
                    type="text"
                    aria-label="Report Revision"
                    value={options.report_revision}
                    onChange={(e) => setOptions({ ...options, report_revision: e.target.value })}
                  />
                </label>
              </div>

              <div className="geom-panel-head" style={{ borderTop: "1px solid #223147" }}>
                <strong>Report Sections &amp; Appendices</strong>
              </div>
              <div className="report-toggles-grid">
                <label className="loads-check-field">
                  <input
                    type="checkbox"
                    checked={options.include_load_combinations}
                    onChange={(e) =>
                      setOptions({ ...options, include_load_combinations: e.target.checked })
                    }
                  />
                  <span>Include NSCP 2015 LRFD &amp; ASD Load Combinations Table</span>
                </label>
                <label className="loads-check-field">
                  <input
                    type="checkbox"
                    checked={options.include_full_member_schedules}
                    onChange={(e) =>
                      setOptions({ ...options, include_full_member_schedules: e.target.checked })
                    }
                  />
                  <span>Include Full Beam, Column &amp; Footing Reinforcement Schedules</span>
                </label>
                <label className="loads-check-field">
                  <input
                    type="checkbox"
                    checked={options.include_bom_cost_estimate}
                    onChange={(e) =>
                      setOptions({ ...options, include_bom_cost_estimate: e.target.checked })
                    }
                  />
                  <span>Include Structural Bill of Materials &amp; PHP Cost Estimate (Sec. 6)</span>
                </label>
                <label className="loads-check-field">
                  <input
                    type="checkbox"
                    checked={options.include_staad_snippet}
                    onChange={(e) =>
                      setOptions({ ...options, include_staad_snippet: e.target.checked })
                    }
                  />
                  <span>Append STAAD.Pro Input File Excerpt (Appendix A)</span>
                </label>
              </div>
            </div>
          )}

          {/* TAB 2: BILL OF MATERIALS & COST ESTIMATE */}
          {activeTab === "bom" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              {/* Editable Unit-Rate, Regional Preset & Trade-Split Labor Bar */}
              <div className="sizing-bulk-bar design-param-bar">
                <label className="design-param-field">
                  <span>Regional Rate Preset</span>
                  <select
                    aria-label="Regional Rate Preset"
                    style={{ width: "170px" }}
                    value={bomParams.region_preset || "car_baguio"}
                    onChange={(e) => handleSelectRegionPreset(e.target.value)}
                  >
                    <option value="car_baguio">CAR / Baguio City</option>
                    <option value="ncr_manila">NCR / Metro Manila</option>
                    <option value="cebu_visayas">Central Visayas / Cebu</option>
                    <option value="davao_mindanao">Davao / Mindanao</option>
                    <option value="custom">Custom Rates</option>
                  </select>
                </label>
                <label className="design-param-field">
                  <span>Concrete ₱/m³</span>
                  <input
                    type="number"
                    step="100"
                    value={bomParams.rate_concrete_php_m3}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        rate_concrete_php_m3: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Rebar ₱/kg</span>
                  <input
                    type="number"
                    step="1"
                    value={bomParams.rate_rebar_php_kg}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        rate_rebar_php_kg: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Formwork ₱/m²</span>
                  <input
                    type="number"
                    step="25"
                    value={bomParams.rate_formwork_php_m2}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        rate_formwork_php_m2: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>CHB ₱/m²</span>
                  <input
                    type="number"
                    step="25"
                    value={bomParams.rate_chb_wall_php_m2}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        rate_chb_wall_php_m2: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Base Labor %</span>
                  <input
                    type="number"
                    step="1"
                    value={bomParams.labor_pct_of_materials}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        labor_pct_of_materials: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Conc Lab %</span>
                  <input
                    type="number"
                    placeholder="Base"
                    value={bomParams.labor_concrete_pct ?? ""}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        labor_concrete_pct: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Rebar Lab %</span>
                  <input
                    type="number"
                    placeholder="Base"
                    value={bomParams.labor_rebar_pct ?? ""}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        labor_rebar_pct: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Form Lab %</span>
                  <input
                    type="number"
                    placeholder="Base"
                    value={bomParams.labor_formwork_pct ?? ""}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        region_preset: "custom",
                        labor_formwork_pct: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Bar Length</span>
                  <select
                    value={bomParams.commercial_bar_length_m}
                    onChange={(e) =>
                      setBomParams({
                        ...bomParams,
                        commercial_bar_length_m: Number(e.target.value),
                      })
                    }
                  >
                    <option value={6.0}>6.0 m</option>
                    <option value={7.5}>7.5 m</option>
                    <option value={9.0}>9.0 m</option>
                    <option value={10.5}>10.5 m</option>
                    <option value={12.0}>12.0 m</option>
                  </select>
                </label>
                <label className="design-param-field">
                  <span>Conc. Waste %</span>
                  <input
                    type="number"
                    step="0.5"
                    value={bomParams.concrete_waste_pct}
                    onChange={(e) =>
                      setBomParams({ ...bomParams, concrete_waste_pct: Number(e.target.value) })
                    }
                  />
                </label>
                <label className="design-param-field">
                  <span>Rebar Splice %</span>
                  <input
                    type="number"
                    step="1"
                    value={bomParams.rebar_waste_splice_pct}
                    onChange={(e) =>
                      setBomParams({ ...bomParams, rebar_waste_splice_pct: Number(e.target.value) })
                    }
                  />
                </label>
                <label className="loads-inline-field" style={{ paddingBottom: "4px" }}>
                  <input
                    type="checkbox"
                    checked={bomParams.include_chb_masonry}
                    onChange={(e) =>
                      setBomParams({ ...bomParams, include_chb_masonry: e.target.checked })
                    }
                  />
                  <span>Include CHB</span>
                </label>
                <button disabled={busy || !hasPrereqs} onClick={() => handleGenerate()}>
                  Recalculate BOM
                </button>
              </div>

              {/* Revision Snapshot & BOM Delta Comparison Bar */}
              <div className="sizing-bulk-bar rev-compare-bar">
                <div className="loads-presets-row">
                  <span className="loads-bar-label">Compare Against Revision:</span>
                  <select
                    aria-label="Compare Against Revision"
                    className="loads-search"
                    value={compareRevId}
                    onChange={(e) => handleCompareRevision(e.target.value)}
                  >
                    <option value="">— Select baseline revision —</option>
                    {(fullProject?.revisions || []).map((r) => (
                      <option key={r.revision_id} value={r.revision_id}>
                        {r.revision_id} · {r.label}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy || !hasPrereqs}
                    onClick={handleQuickSaveRevision}
                  >
                    + Save Snapshot (Rev. {(fullProject?.revisions || []).length})
                  </button>
                </div>
                {revDelta && (
                  <div className="rev-delta-strip">
                    <div>
                      <small>Concrete Delta vs {revDelta.base_rev}</small>
                      <strong className={revDelta.concrete_delta_m3 > 0 ? "delta-up" : "delta-down"}>
                        {revDelta.concrete_delta_m3 >= 0 ? "+" : ""}
                        {fmtNum(revDelta.concrete_delta_m3, 2)} m³ ({revDelta.concrete_delta_pct >= 0 ? "+" : ""}
                        {fmtNum(revDelta.concrete_delta_pct, 1)}%)
                      </strong>
                    </div>
                    <div>
                      <small>Rebar Delta vs {revDelta.base_rev}</small>
                      <strong className={revDelta.rebar_delta_kg > 0 ? "delta-up" : "delta-down"}>
                        {revDelta.rebar_delta_kg >= 0 ? "+" : ""}
                        {fmtNum(revDelta.rebar_delta_kg, 0)} kg ({revDelta.rebar_delta_pct >= 0 ? "+" : ""}
                        {fmtNum(revDelta.rebar_delta_pct, 1)}%)
                      </strong>
                    </div>
                    <div>
                      <small>Cost Delta vs {revDelta.base_rev}</small>
                      <strong className={revDelta.cost_delta_php > 0 ? "delta-up" : "delta-down"}>
                        {revDelta.cost_delta_php >= 0 ? "+" : ""}
                        {fmtPhp(revDelta.cost_delta_php)} ({revDelta.cost_delta_pct >= 0 ? "+" : ""}
                        {fmtNum(revDelta.cost_delta_pct, 1)}%)
                      </strong>
                    </div>
                    <div>
                      <small>Members / Footings</small>
                      <strong>
                        {revDelta.member_count_delta >= 0 ? "+" : ""}
                        {revDelta.member_count_delta} / {revDelta.footing_count_delta >= 0 ? "+" : ""}
                        {revDelta.footing_count_delta}
                      </strong>
                    </div>
                  </div>
                )}
              </div>

              {!bom ? (
                <div className="geom-panel">
                  <div className="loads-empty">
                    Click <strong>Generate Report &amp; BOM</strong> to compute Concrete, PNS 49
                    Rebar, Formwork, and PHP Cost Estimate quantities.
                  </div>
                </div>
              ) : (
                <>
                  {/* Grand Total Cost Banner */}
                  <div className="report-grand-banner">
                    <div>
                      <small>ESTIMATED STRUCTURAL CONSTRUCTION COST (NBCP SEC. 302-5f)</small>
                      <strong>{fmtPhp(bom.grand_total_cost_php)}</strong>
                    </div>
                    <div className="report-grand-split">
                      <span>
                        <small>Materials</small>
                        <b>{fmtPhp(bom.total_material_cost_php)}</b>
                      </span>
                      <span>
                        <small>Labor ({bomParams.labor_pct_of_materials}%)</small>
                        <b>{fmtPhp(bom.total_labor_cost_php)}</b>
                      </span>
                    </div>
                  </div>

                  {/* Table 1: Concrete & Formwork */}
                  <div className="geom-panel">
                    <div className="geom-panel-head">
                      <div>
                        <strong>1. Concrete &amp; Formwork Quantity Takeoff</strong>
                        <small>
                          Net geometry volumes plus {bomParams.concrete_waste_pct}% pour/wastage allowance.
                        </small>
                      </div>
                    </div>
                    <div className="sizing-table-wrap">
                      <table className="sizing-table">
                        <thead>
                          <tr>
                            <th>Element Group</th>
                            <th>Members</th>
                            <th>Net Vol. (m³)</th>
                            <th>Gross Vol. (m³)</th>
                            <th>Formwork (m²)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bom.concrete_rows.map((r) => (
                            <tr key={r.element_group}>
                              <td>{r.element_group}</td>
                              <td>{r.member_count}</td>
                              <td>{fmtNum(r.net_volume_m3, 3)}</td>
                              <td>{fmtNum(r.gross_volume_m3, 3)}</td>
                              <td>{fmtNum(r.formwork_area_m2, 2)}</td>
                            </tr>
                          ))}
                          <tr className="loads-total-row">
                            <td><strong>TOTAL</strong></td>
                            <td>
                              <strong>
                                {bom.concrete_rows.reduce((a, r) => a + r.member_count, 0)}
                              </strong>
                            </td>
                            <td><strong>{fmtNum(bom.total_concrete_net_m3, 3)}</strong></td>
                            <td><strong>{fmtNum(bom.total_concrete_gross_m3, 3)}</strong></td>
                            <td><strong>{fmtNum(bom.total_formwork_m2, 2)}</strong></td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Table 2: PNS 49 Rebar Breakdown */}
                  <div className="geom-panel">
                    <div className="geom-panel-head">
                      <div>
                        <strong>2. PNS 49:2020 Reinforcing Steel Breakdown (kg &amp; pcs)</strong>
                        <small>
                          Nominal linear mass d²/162 kg/m · {bomParams.rebar_waste_splice_pct}%
                          lap/splice waste · {bomParams.commercial_bar_length_m} m commercial lengths ·
                          Steel Ratio: {fmtNum(bom.rebar_ratio_kg_per_m3, 1)} kg/m³
                        </small>
                      </div>
                    </div>
                    <div className="sizing-table-wrap">
                      <table className="sizing-table">
                        <thead>
                          <tr>
                            <th>Diameter</th>
                            <th>Steel Grade</th>
                            <th>Unit Mass (kg/m)</th>
                            <th>Net Length (m)</th>
                            <th>Gross Length (m)</th>
                            <th>Weight (kg)</th>
                            <th>Commercial Pcs ({bomParams.commercial_bar_length_m}m)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bom.rebar_rows.map((r) => (
                            <tr key={r.diameter_mm}>
                              <td><strong>Ø{r.diameter_mm} mm</strong></td>
                              <td>{r.grade_label}</td>
                              <td>{fmtNum(r.unit_mass_kg_m, 4)}</td>
                              <td>{fmtNum(r.net_length_m, 2)}</td>
                              <td>{fmtNum(r.gross_length_m, 2)}</td>
                              <td>{fmtNum(r.total_weight_kg, 2)}</td>
                              <td><strong>{r.commercial_pieces.toLocaleString()} pcs</strong></td>
                            </tr>
                          ))}
                          <tr className="loads-total-row">
                            <td><strong>TOTAL</strong></td>
                            <td>
                              Tie Wire (#16 G.I.): <strong>{fmtNum(bom.total_tie_wire_kg, 2)} kg</strong>
                            </td>
                            <td>—</td>
                            <td>
                              <strong>
                                {fmtNum(
                                  bom.rebar_rows.reduce((a, r) => a + r.net_length_m, 0),
                                  2
                                )}
                              </strong>
                            </td>
                            <td>
                              <strong>
                                {fmtNum(
                                  bom.rebar_rows.reduce((a, r) => a + r.gross_length_m, 0),
                                  2
                                )}
                              </strong>
                            </td>
                            <td><strong>{fmtNum(bom.total_rebar_kg, 2)} kg</strong></td>
                            <td>
                              <strong>
                                {bom.rebar_rows
                                  .reduce((a, r) => a + r.commercial_pieces, 0)
                                  .toLocaleString()}{" "}
                                pcs
                              </strong>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Table 3: Cost Estimate (PHP) */}
                  <div className="geom-panel">
                    <div className="geom-panel-head">
                      <div>
                        <strong>3. NBCP Sec. 302-5f Structural Cost Estimate (PHP)</strong>
                        <small>
                          Material supply rates plus {bomParams.labor_pct_of_materials}% skilled manpower allowance.
                        </small>
                      </div>
                    </div>
                    <div className="sizing-table-wrap">
                      <table className="sizing-table">
                        <thead>
                          <tr>
                            <th>Item</th>
                            <th>Description</th>
                            <th>Qty</th>
                            <th>Unit</th>
                            <th>Unit Cost</th>
                            <th>Material (PHP)</th>
                            <th>Labor (PHP)</th>
                            <th>Total (PHP)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {bom.cost_lines.map((c) => (
                            <tr key={c.item_no}>
                              <td>{c.item_no}</td>
                              <td>{c.description}</td>
                              <td>{fmtNum(c.quantity, 2)}</td>
                              <td>{c.unit}</td>
                              <td>{fmtPhp(c.unit_cost_php)}</td>
                              <td>{fmtPhp(c.material_cost_php)}</td>
                              <td>{fmtPhp(c.labor_cost_php)}</td>
                              <td><strong>{fmtPhp(c.total_cost_php)}</strong></td>
                            </tr>
                          ))}
                          <tr className="loads-total-row">
                            <td colSpan={5}><strong>GRAND TOTAL STRUCTURAL COST</strong></td>
                            <td><strong>{fmtPhp(bom.total_material_cost_php)}</strong></td>
                            <td><strong>{fmtPhp(bom.total_labor_cost_php)}</strong></td>
                            <td><strong>{fmtPhp(bom.grand_total_cost_php)}</strong></td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {/* TAB 3: A4 EXECUTIVE REPORT PREVIEW */}
          {activeTab === "preview" && (
            <div className="report-paper-wrap">
              <div className="report-paper">
                <div className="report-paper-cover">
                  <div className="report-paper-kicker">
                    STRUCTURAL DESIGN CALCULATIONS &amp; BILL OF MATERIALS
                  </div>
                  <h1>{project.project_name}</h1>
                  <p className="report-paper-sub">
                    {project.project_id} · {signatory.project_address || project.location} ·{" "}
                    {options.report_revision}
                  </p>
                  <div className="report-paper-meta">
                    <div>
                      <span>Owner / Client</span>
                      <strong>{signatory.project_owner || "To be specified"}</strong>
                    </div>
                    <div>
                      <span>Applicable Codes</span>
                      <strong>NSCP 2015 (7th Ed.), ACI 318, NBCP PD 1096, PNS 49</strong>
                    </div>
                    <div>
                      <span>Prepared &amp; Sealed By</span>
                      <strong>
                        {signatory.engineer_name || "[Engineer Name]"} · PRC{" "}
                        {signatory.prc_license_no || "TBD"}
                      </strong>
                    </div>
                    <div>
                      <span>Geotech Reference</span>
                      <strong>{signatory.geotech_report_ref}</strong>
                    </div>
                  </div>
                </div>

                <h3>Executive Engineering Summary</h3>
                <div className="report-paper-kpis">
                  <div>
                    <small>Concrete / Steel</small>
                    <strong>
                      f&apos;c = {sizing?.material?.fc_mpa || 28} MPa · fy ={" "}
                      {sizing?.material?.fy_main_mpa || 415} MPa
                    </strong>
                  </div>
                  <div>
                    <small>Seismic Base Shear (Vx)</small>
                    <strong>
                      {lg?.seismic_summary
                        ? `${fmtNum(lg.seismic_summary.v_design_x_kn, 1)} kN (${fmtNum(
                            lg.seismic_summary.base_shear_ratio_x * 100,
                            2
                          )}% W)`
                        : "N/A"}
                    </strong>
                  </div>
                  <div>
                    <small>Gross Concrete / Rebar</small>
                    <strong>
                      {bom
                        ? `${fmtNum(bom.total_concrete_gross_m3, 2)} m³ · ${fmtNum(
                            bom.total_rebar_kg,
                            0
                          )} kg`
                        : "Not generated"}
                    </strong>
                  </div>
                  <div>
                    <small>Est. Structural Cost</small>
                    <strong>{bom ? fmtPhp(bom.grand_total_cost_php) : "Not generated"}</strong>
                  </div>
                </div>

                <h3>Story Drift Verification (NSCP 2015 Sec. 208.6.4)</h3>
                <table className="report-paper-table">
                  <thead>
                    <tr>
                      <th>Story</th>
                      <th>h (m)</th>
                      <th>ΔM X / Z (mm)</th>
                      <th>Allow (mm)</th>
                      <th>Max Ratio</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(ad?.story_drifts || []).map((d) => (
                      <tr key={d.floor_level}>
                        <td>{d.floor_level}</td>
                        <td>{fmtNum(d.story_height_m, 2)}</td>
                        <td>
                          {fmtNum(d.delta_m_x_mm, 2)} / {fmtNum(d.delta_m_z_mm, 2)}
                        </td>
                        <td>{fmtNum(d.allowable_drift_mm, 1)}</td>
                        <td>{fmtNum(Math.max(d.drift_ratio_x, d.drift_ratio_z), 3)}</td>
                        <td>
                          <span
                            className={`report-paper-badge ${
                              d.status === "PASS" ? "pass" : "fail"
                            }`}
                          >
                            {d.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <h3>RC Member Design Compliance Summary</h3>
                <table className="report-paper-table">
                  <thead>
                    <tr>
                      <th>Structural Group</th>
                      <th>Pass / Total</th>
                      <th>Max Governing DCR</th>
                      <th>Overall</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      [
                        "Beams & Tie Beams",
                        `${designStats.beamPass} / ${designStats.beamTotal}`,
                        designStats.maxBeamDcr,
                        designStats.beamPass === designStats.beamTotal ? "PASS" : "FAIL",
                      ],
                      [
                        "Columns & Pedestals",
                        `${designStats.colPass} / ${designStats.colTotal}`,
                        designStats.maxColDcr,
                        designStats.colPass === designStats.colTotal ? "PASS" : "FAIL",
                      ],
                      [
                        "Isolated Footings",
                        `${designStats.ftgPass} / ${designStats.ftgTotal}`,
                        designStats.maxFtgDcr,
                        designStats.ftgPass === designStats.ftgTotal ? "PASS" : "FAIL",
                      ],
                    ].map(([grp, cnt, dcr, st]) => (
                      <tr key={grp}>
                        <td>{grp}</td>
                        <td>{cnt}</td>
                        <td>{fmtNum(dcr, 3)}</td>
                        <td>
                          <span className={`report-paper-badge ${st === "PASS" ? "pass" : "fail"}`}>
                            {st}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {bom && (
                  <div className="report-paper-total">
                    <span>
                      NBCP Sec. 302-5f Estimated Structural Cost (Materials +{" "}
                      {bomParams.labor_pct_of_materials}% Labor)
                    </span>
                    <strong>{fmtPhp(bom.grand_total_cost_php)}</strong>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Right Sidebar: Permit Readiness KPIs & Checklist */}
        <aside className="geom-sidebar">
          <div className="geom-checks-head">
            <h4>BOM &amp; Permit KPIs</h4>
          </div>
          <div className="geom-stats-grid loads-kpi-sidebar">
            <div>
              <strong>{bom ? fmtNum(bom.total_concrete_gross_m3, 2) : "—"}</strong>
              <small>Total Concrete (m³)</small>
            </div>
            <div>
              <strong>{bom ? fmtNum(bom.total_rebar_kg, 0) : "—"}</strong>
              <small>Total Rebar (kg)</small>
            </div>
            <div>
              <strong>{bom ? fmtNum(bom.rebar_ratio_kg_per_m3, 1) : "—"}</strong>
              <small>Steel Ratio (kg/m³)</small>
            </div>
            <div>
              <strong>{bom ? fmtPhp(bom.grand_total_cost_php) : "—"}</strong>
              <small>Estimated Cost</small>
            </div>
          </div>

          <div className="geom-checks-head" style={{ marginTop: "6px" }}>
            <h4>OBO Permit Readiness Checklist</h4>
          </div>
          <div className="report-checklist">
            {checklistItems.map((item) => (
              <div
                key={item.label}
                className={item.ok ? "report-check-row ok" : "report-check-row pending"}
              >
                <span>{item.ok ? "✓" : "!"}</span>
                <small>{item.label}</small>
              </div>
            ))}
          </div>

          {warnings.length > 0 && (
            <>
              <div className="geom-checks-head" style={{ marginTop: "6px" }}>
                <h4>Readiness Warnings ({warnings.length})</h4>
              </div>
              {warnings.map((w, idx) => (
                <div key={idx} className="geom-issue sev-warning">
                  <div className="geom-issue-top">
                    <span className="geom-sev-badge sev-warning">WARNING</span>
                    <code>OBO-CHECK</code>
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
