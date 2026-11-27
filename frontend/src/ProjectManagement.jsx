import React, { useEffect, useMemo, useState } from "react";

function formatTimestamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

export default function ProjectManagement({
  projects = [],
  activeProject = null,
  onSelectProject,
  onDeletedProject,
  onRefreshProjects,
  onLoadSample,
  onCreateProject,
  onOpenModule,
  onClose,
}) {
  const [search, setSearch] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);
  const [deletingId, setDeletingId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // Inline "+ New Project" form inside Module 01
  const [showNewForm, setShowNewForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newLocation, setNewLocation] = useState("Baguio City, Philippines");

  // Full active project state for metadata / seismic / wind form
  const [fullProject, setFullProject] = useState(null);
  const [loadingActive, setLoadingActive] = useState(false);
  const [metaName, setMetaName] = useState("");
  const [metaLocation, setMetaLocation] = useState("");
  const [metaCode, setMetaCode] = useState("NSCP 2015");
  const [metaFc, setMetaFc] = useState("28");
  const [metaFy, setMetaFy] = useState("415");
  const [metaDensity, setMetaDensity] = useState("23.54");

  useEffect(() => {
    let cancelled = false;
    async function loadActive() {
      if (!activeProject || activeProject.corrupted) {
        setFullProject(null);
        return;
      }
      setLoadingActive(true);
      try {
        const res = await fetch(
          `/api/projects/${encodeURIComponent(activeProject.project_id)}`
        );
        if (!res.ok) {
          if (!cancelled) setFullProject(null);
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setFullProject(data);
        const info = data.project_info || {};
        setMetaName(info.project_name || "");
        setMetaLocation(info.location || "");
        setMetaCode(info.design_code || "NSCP 2015");
        setMetaFc(String(info.materials?.fc_mpa ?? 28));
        setMetaFy(String(info.materials?.fy_mpa ?? 415));
        setMetaDensity(String(info.materials?.concrete_density_kn_m3 ?? 23.54));
      } catch {
        if (!cancelled) setFullProject(null);
      } finally {
        if (!cancelled) setLoadingActive(false);
      }
    }
    loadActive();
    return () => {
      cancelled = true;
    };
  }, [activeProject?.project_id, activeProject?.corrupted]);

  const filteredProjects = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter(
      (p) =>
        (p.project_id || "").toLowerCase().includes(q) ||
        (p.project_name || "").toLowerCase().includes(q) ||
        (p.location || "").toLowerCase().includes(q)
    );
  }, [projects, search]);

  async function handleRefresh() {
    setBusy(true);
    setError("");
    try {
      await onRefreshProjects?.();
    } catch (e) {
      setError(e.message || "Failed to refresh saved projects.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateSubmit(e) {
    e.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const createdId = await onCreateProject?.(trimmed, newLocation.trim() || "Philippines");
      setNewName("");
      setShowNewForm(false);
      setNotice(`Created project ${trimmed}${createdId ? ` (${createdId})` : ""}.`);
    } catch (err) {
      setError(err.message || "Failed to create project.");
    } finally {
      setBusy(false);
    }
  }

  async function handleLoadDemoClick() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await onLoadSample?.();
      setNotice("Demonstration project loaded and ready.");
    } catch (err) {
      setError(err.message || "Failed to load demonstration project.");
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirmDelete(projectId) {
    setDeletingId(projectId);
    setError("");
    setNotice("");
    try {
      const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
        method: "DELETE",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `Delete failed (${res.status})`);
      }
      setConfirmDeleteId(null);
      await onDeletedProject?.(projectId);
      setNotice(`Deleted project ${projectId} and all associated artifacts.`);
    } catch (err) {
      setError(err.message || "Failed to delete project.");
    } finally {
      setDeletingId(null);
    }
  }

  async function handleSaveMetadata(e) {
    e.preventDefault();
    if (!fullProject) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updatedPayload = {
        ...fullProject,
        project_info: {
          ...fullProject.project_info,
          project_name: metaName.trim() || fullProject.project_info.project_name,
          location: metaLocation.trim() || "Philippines",
          design_code: metaCode.trim() || "NSCP 2015",
          materials: {
            ...(fullProject.project_info.materials || {}),
            fc_mpa: Math.max(17, parseFloat(metaFc) || 28),
            fy_mpa: Math.max(230, parseFloat(metaFy) || 415),
            concrete_density_kn_m3: Math.max(18, parseFloat(metaDensity) || 23.54),
          },
        },
      };
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updatedPayload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || "Failed to save project metadata.");
      }
      setFullProject(updatedPayload);
      await onRefreshProjects?.();
      setNotice(`Saved metadata for ${updatedPayload.project_info.project_id}.`);
    } catch (err) {
      setError(err.message || "Failed to save project metadata.");
    } finally {
      setBusy(false);
    }
  }

  const seismic = fullProject?.load_generation?.seismic || null;
  const wind = fullProject?.load_generation?.wind || null;

  return (
    <section className="geom-builder">
      <div className="pdf-topbar">
        <div>
          <label>MODULE 01 · PROJECT MANAGEMENT &amp; SAVED PROJECTS HUB</label>
          <h2>
            {activeProject
              ? `${activeProject.project_name} (${activeProject.project_id})`
              : "Saved Projects Directory"}
          </h2>
        </div>
        <div className="pdf-topbar-actions">
          <button
            type="button"
            disabled={busy || Boolean(deletingId)}
            onClick={() => setShowNewForm((v) => !v)}
          >
            + New Project
          </button>
          <button
            type="button"
            className="secondary"
            disabled={busy || Boolean(deletingId)}
            onClick={handleLoadDemoClick}
          >
            Load Demonstration Project
          </button>
          <button type="button" className="secondary" onClick={onClose}>
            Back to Dashboard
          </button>
        </div>
      </div>

      {error && <div className="pdf-alert error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {showNewForm && (
        <form className="new-proj-form cmd-new-proj" onSubmit={handleCreateSubmit}>
          <input
            type="text"
            placeholder="Project name (e.g. 3-Story Residential Frame)"
            aria-label="New project name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            required
          />
          <input
            type="text"
            placeholder="Location"
            aria-label="New project location"
            value={newLocation}
            onChange={(e) => setNewLocation(e.target.value)}
          />
          <button type="submit" disabled={busy || !newName.trim()}>
            Create Project
          </button>
          <button type="button" className="secondary" onClick={() => setShowNewForm(false)}>
            Cancel
          </button>
        </form>
      )}

      {/* Saved Projects Hub */}
      <div className="geom-panel" style={{ marginBottom: 16 }}>
        <div className="geom-panel-head">
          <div>
            <strong>Saved Projects ({projects.length})</strong>
            <small style={{ marginLeft: 8, color: "#94a3b8" }}>
              Switch active workspace project or safely delete obsolete / corrupted files.
            </small>
          </div>
          <div className="pdf-inline-form" style={{ gap: 8 }}>
            <input
              type="search"
              className="pdf-select"
              placeholder="Search by ID or name..."
              aria-label="Search saved projects"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ minWidth: 220 }}
            />
            <button
              type="button"
              className="mini-btn"
              disabled={busy || Boolean(deletingId)}
              onClick={handleRefresh}
            >
              Refresh
            </button>
          </div>
        </div>

        {filteredProjects.length === 0 ? (
          <div className="proj-empty" style={{ padding: 20 }}>
            {projects.length === 0 ? (
              <>
                No saved projects in <code>data/projects/</code> yet. Click{" "}
                <strong>+ New Project</strong> or <strong>Load Demonstration Project</strong> above
                to begin.
              </>
            ) : (
              <>No saved projects match &ldquo;{search}&rdquo;.</>
            )}
          </div>
        ) : (
          <div className="sizing-table-wrap">
            <table className="sizing-table">
              <thead>
                <tr>
                  <th>Project ID</th>
                  <th>Project Name</th>
                  <th>Last Modified</th>
                  <th>Status / Stage</th>
                  <th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredProjects.map((p) => {
                  const isActive = Boolean(
                    activeProject && activeProject.project_id === p.project_id && !p.corrupted
                  );
                  const isConfirming = confirmDeleteId === p.project_id;
                  const isDeleting = deletingId === p.project_id;
                  return (
                    <tr
                      key={p.project_id}
                      style={
                        isActive
                          ? { background: "rgba(16, 185, 129, 0.08)" }
                          : p.corrupted
                            ? { background: "rgba(244, 63, 94, 0.06)" }
                            : undefined
                      }
                    >
                      <td>
                        <code className="proj-id-pill">{p.project_id}</code>
                        {isActive && (
                          <span
                            className="module-phase-dot done"
                            style={{ marginLeft: 8, fontSize: 10 }}
                          >
                            Active
                          </span>
                        )}
                      </td>
                      <td>
                        <strong>{p.project_name}</strong>
                        {p.corrupted && p.error && (
                          <small style={{ display: "block", color: "#fda4af" }}>
                            {p.error}
                          </small>
                        )}
                        {!p.corrupted && p.location && (
                          <small style={{ display: "block", color: "#94a3b8" }}>
                            {p.location} · {p.design_code || "NSCP 2015"}
                          </small>
                        )}
                      </td>
                      <td style={{ whiteSpace: "nowrap", color: "#cbd5e1" }}>
                        {formatTimestamp(p.updated_at)}
                      </td>
                      <td>
                        {p.corrupted ? (
                          <span
                            className="geom-sev-badge sev-error"
                            title={p.error || "Invalid JSON or schema"}
                          >
                            Corrupted
                          </span>
                        ) : (
                          <span className="proj-code-pill">{p.stage || "Module 01 · Setup"}</span>
                        )}
                      </td>
                      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                        <div style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                          <button
                            type="button"
                            className={`mini-btn ${isActive ? "on-approve" : ""}`}
                            disabled={Boolean(p.corrupted) || isDeleting || busy}
                            onClick={() => {
                              onSelectProject?.(p);
                              setNotice(`Active project switched to ${p.project_id}.`);
                            }}
                            title={
                              p.corrupted
                                ? "Corrupted project cannot be opened"
                                : `Set ${p.project_id} as active project`
                            }
                          >
                            {isActive ? "Active" : "Open"}
                          </button>

                          {!isConfirming ? (
                            <button
                              type="button"
                              className="mini-btn on-reject"
                              disabled={Boolean(deletingId) || busy}
                              onClick={() => setConfirmDeleteId(p.project_id)}
                            >
                              Delete
                            </button>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="mini-btn on-reject"
                                disabled={isDeleting}
                                onClick={() => handleConfirmDelete(p.project_id)}
                                style={{ fontWeight: 700 }}
                              >
                                {isDeleting ? "Deleting..." : "Confirm Delete"}
                              </button>
                              <button
                                type="button"
                                className="mini-btn"
                                disabled={isDeleting}
                                onClick={() => setConfirmDeleteId(null)}
                              >
                                Cancel
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Active Project Metadata & Site Parameters (strictly guarded against null activeProject) */}
      <div className="geom-panel">
        <div className="geom-panel-head">
          <strong>
            {activeProject
              ? `Project Metadata & Site Parameters — ${activeProject.project_id}`
              : "No Active Project Selected"}
          </strong>
          {activeProject && !activeProject.corrupted && (
            <div style={{ display: "flex", gap: 6 }}>
              <button
                type="button"
                className="mini-btn"
                onClick={() => onOpenModule?.("02", activeProject)}
              >
                Open Plan Import (02)
              </button>
              <button
                type="button"
                className="mini-btn"
                onClick={() => onOpenModule?.("03", activeProject)}
              >
                Open 3D Geometry (03)
              </button>
            </div>
          )}
        </div>

        {!activeProject || activeProject.corrupted ? (
          <div className="proj-empty" style={{ padding: 20 }}>
            <p style={{ marginBottom: 12 }}>
              Select a valid project above or create a new project to configure project metadata,
              concrete/steel material grades, and NSCP 2015 seismic/wind site parameters.
            </p>
            <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
              <button
                type="button"
                disabled={busy}
                onClick={() => setShowNewForm(true)}
              >
                + New Project
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={handleLoadDemoClick}
              >
                Load Demonstration Project
              </button>
            </div>
          </div>
        ) : loadingActive || !fullProject ? (
          <div className="proj-empty" style={{ padding: 16 }}>
            Loading project configuration...
          </div>
        ) : (
          <form onSubmit={handleSaveMetadata} style={{ padding: 14, display: "grid", gap: 14 }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                gap: 10,
              }}
            >
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Project Name</span>
                <input
                  type="text"
                  value={metaName}
                  onChange={(e) => setMetaName(e.target.value)}
                  required
                />
              </label>
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Location</span>
                <input
                  type="text"
                  value={metaLocation}
                  onChange={(e) => setMetaLocation(e.target.value)}
                />
              </label>
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Design Code</span>
                <input
                  type="text"
                  value={metaCode}
                  onChange={(e) => setMetaCode(e.target.value)}
                />
              </label>
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Concrete f&apos;c (MPa)</span>
                <input
                  type="number"
                  step="1"
                  min="17"
                  value={metaFc}
                  onChange={(e) => setMetaFc(e.target.value)}
                />
              </label>
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Rebar fy (MPa)</span>
                <input
                  type="number"
                  step="5"
                  min="230"
                  value={metaFy}
                  onChange={(e) => setMetaFy(e.target.value)}
                />
              </label>
              <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <span>Concrete Density (kN/m³)</span>
                <input
                  type="number"
                  step="0.01"
                  min="18"
                  value={metaDensity}
                  onChange={(e) => setMetaDensity(e.target.value)}
                />
              </label>
            </div>

            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 12,
                alignItems: "center",
                justifyContent: "space-between",
                paddingTop: 8,
                borderTop: "1px solid #1e293b",
              }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 12, color: "#94a3b8" }}>
                <span>
                  Seismic Zone: <strong style={{ color: "#e2e8f0" }}>{seismic?.zone ?? 4}</strong> (Soil{" "}
                  {seismic?.soil_profile || "SD"})
                </span>
                <span>·</span>
                <span>
                  Wind: <strong style={{ color: "#e2e8f0" }}>{wind?.basic_wind_speed_kph ?? 250} kph</strong>{" "}
                  (Exp. {wind?.exposure_category || "B"})
                </span>
                <span>·</span>
                <span>
                  Revisions: <strong style={{ color: "#e2e8f0" }}>{(fullProject.revisions || []).length}</strong>
                </span>
              </div>
              <button type="submit" disabled={busy}>
                Save Project Info
              </button>
            </div>
          </form>
        )}
      </div>
    </section>
  );
}
