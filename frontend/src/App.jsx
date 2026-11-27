import React, { useEffect, useState } from "react";
import ProjectManagement from "./ProjectManagement.jsx";
import PdfImporter from "./PdfImporter.jsx";
import GeometryBuilder from "./GeometryBuilder.jsx";
import MemberSizing from "./MemberSizing.jsx";
import LoadGenerator from "./LoadGenerator.jsx";
import DesignReview from "./DesignReview.jsx";
import CadDrawings from "./CadDrawings.jsx";
import ReportBuilder from "./ReportBuilder.jsx";

const MODULES = [
  ["01", "Project Management", "Project metadata, seismic/wind site parameters & revision snapshots"],
  ["02", "Plan Import", "Direct AutoCAD .dxf/.dwg & PDF import, Master Grid tracing & openings"],
  ["03", "Structural Geometry", "3D wireframe generator, 2D floor framing editor & STAAD .std export"],
  ["04", "Member Sizing & Properties", "Preliminary NSCP 2015 sections, cracked modifiers & slab thickness"],
  ["05", "NSCP 2015 Loads & Export", "Gravity tributary loads, Method B Rayleigh seismic, P-Delta & full .std"],
  ["06", "Analysis & RC Design", "3D DSM solver, Auto-Resolve, SMRF SCWB, retaining walls & 2D/3D Studio"],
  ["07", "Structural Drawings", "Per-page Sheet Composer, anti-collision CAD & .dwg/.dxf/.pdf/.zip export"],
  ["08", "Calculation Reports & BOM", "NBCP 302-5 DOCX/PDF reports, PNS 49 takeoff, regional rates & rev deltas"],
];

const PHASE_KEYS = ["01", "02", "03", "04", "05", "06", "07", "08"];
const ACTIVE_PROJECT_STORAGE_KEY = "structural_ai_active_project_id";

export default function App() {
  const [health, setHealth] = useState("Checking");
  const [projects, setProjects] = useState([]);
  const [activeProjectId, setActiveProjectId] = useState(() => {
    try {
      return window.localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY) || "";
    } catch {
      return "";
    }
  });
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [showModule01, setShowModule01] = useState(false);
  const [importProject, setImportProject] = useState(null);
  const [geometryProject, setGeometryProject] = useState(null);
  const [sizingProject, setSizingProject] = useState(null);
  const [loadsProject, setLoadsProject] = useState(null);
  const [designProject, setDesignProject] = useState(null);
  const [cadProject, setCadProject] = useState(null);
  const [reportProject, setReportProject] = useState(null);
  const [showNewForm, setShowNewForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newLocation, setNewLocation] = useState("Baguio City, Philippines");
  const [revProject, setRevProject] = useState(null);
  const [revLabel, setRevLabel] = useState("");
  const [revNotes, setRevNotes] = useState("");

  // Phase 1 Beta Auth state
  const [authChecked, setAuthChecked] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [authUsername, setAuthUsername] = useState("");
  const [loginUser, setLoginUser] = useState("");
  const [loginPass, setLoginPass] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);

  async function openRevisions(p) {
    setBusy(true);
    setMessage("");
    try {
      const full = await fetch(`/api/projects/${encodeURIComponent(p.project_id)}`).then((r) => r.json());
      setRevProject(full);
      setRevLabel(`Rev. ${(full.revisions || []).length} — Milestone Snapshot`);
      setRevNotes("");
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveRevision(e) {
    e.preventDefault();
    if (!revProject) return;
    setBusy(true);
    setMessage("");
    try {
      const pid = encodeURIComponent(revProject.project_id);
      const r = await fetch(`/api/projects/${pid}/revisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: revLabel.trim() || "Revision Snapshot",
          notes: revNotes.trim(),
        }),
      });
      const b = await r.json();
      if (!r.ok) throw Error(b.detail || "Failed to save revision");
      const full = await fetch(`/api/projects/${pid}`).then((res) => res.json());
      setRevProject(full);
      setRevLabel(`Rev. ${(full.revisions || []).length} — Milestone Snapshot`);
      setRevNotes("");
      setMessage(`Saved revision ${b.revision_id} (${b.label}).`);
      await refresh();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function restoreRevision(revId) {
    if (!revProject) return;
    setBusy(true);
    setMessage("");
    try {
      const pid = encodeURIComponent(revProject.project_id);
      const r = await fetch(`/api/projects/${pid}/revisions/${encodeURIComponent(revId)}/restore`, {
        method: "POST",
      });
      const b = await r.json();
      if (!r.ok) throw Error(b.detail || "Restore failed");
      setRevProject(b);
      setMessage(`Restored working state to ${revId} (full revision history preserved).`);
      await refresh();
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  function persistActiveProjectId(pid) {
    setActiveProjectId(pid || "");
    try {
      if (pid) window.localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, pid);
      else window.localStorage.removeItem(ACTIVE_PROJECT_STORAGE_KEY);
    } catch {
      // Ignore storage errors in restricted environments
    }
  }

  async function refresh() {
    try {
      const h = await fetch("/api/health").then((r) => r.json());
      setHealth(h.status === "ok" ? "Connected" : "Unavailable");
      const list = await fetch("/api/projects").then((r) => r.json());
      const safeList = Array.isArray(list) ? list : [];
      setProjects(safeList);

      // Reconcile persisted activeProjectId so missing/deleted/corrupted IDs never cause 404s
      const validProjects = safeList.filter((p) => !p.corrupted);
      setActiveProjectId((prevId) => {
        const stillValid = prevId && validProjects.some((p) => p.project_id === prevId);
        const nextId = stillValid ? prevId : (validProjects[0]?.project_id || "");
        try {
          if (nextId) window.localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, nextId);
          else window.localStorage.removeItem(ACTIVE_PROJECT_STORAGE_KEY);
        } catch {
          // ignore
        }
        return nextId;
      });
      return safeList;
    } catch {
      setHealth("Backend unavailable");
      return [];
    }
  }

  function clearWorkspaceState() {
    setProjects([]);
    setShowModule01(false);
    setImportProject(null);
    setGeometryProject(null);
    setSizingProject(null);
    setLoadsProject(null);
    setDesignProject(null);
    setCadProject(null);
    setReportProject(null);
    setRevProject(null);
    setShowNewForm(false);
    setMessage("");
    persistActiveProjectId("");
  }

  useEffect(() => {
    let cancelled = false;
    async function bootstrapSession() {
      try {
        const res = await fetch("/api/auth/me", { credentials: "include" });
        const data = await res.json().catch(() => ({ auth_enabled: false, authenticated: false, username: null }));
        if (cancelled) return;
        const enabled = Boolean(data.auth_enabled);
        const authed = Boolean(data.authenticated);
        setAuthEnabled(enabled);
        setAuthenticated(authed);
        setAuthUsername(data.username || "");
        setAuthChecked(true);
        if (!enabled || authed) {
          await refresh();
        }
      } catch {
        if (cancelled) return;
        setAuthEnabled(false);
        setAuthenticated(false);
        setAuthChecked(true);
        await refresh();
      }
    }
    bootstrapSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onUnauthorized() {
      if (!authEnabled) return;
      clearWorkspaceState();
      setAuthenticated(false);
      setAuthUsername("");
      setLoginError("Your session has expired. Please sign in again.");
    }
    window.addEventListener("sao:unauthorized", onUnauthorized);
    return () => window.removeEventListener("sao:unauthorized", onUnauthorized);
  }, [authEnabled]);

  async function handleLoginSubmit(e) {
    e.preventDefault();
    if (!loginUser.trim() || !loginPass) return;
    setLoginBusy(true);
    setLoginError("");
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: loginUser.trim(),
          password: loginPass,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setLoginError(body.detail || "Invalid username or password.");
        return;
      }
      setAuthenticated(true);
      setAuthUsername(body.username || loginUser.trim().toLowerCase());
      setLoginPass("");
      setLoginError("");
      await refresh();
    } catch {
      setLoginError("Unable to reach backend server. Please retry.");
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleLogout() {
    setBusy(true);
    try {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    } catch {
      // ignore
    } finally {
      clearWorkspaceState();
      setAuthenticated(false);
      setAuthUsername("");
      setLoginError("");
      setBusy(false);
    }
  }

  async function loadSample() {
    setBusy(true);
    setMessage("");
    try {
      const sample = await fetch("/sample_project.json").then((r) => r.json());
      const r = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sample),
      });
      const b = await r.json();
      if (!r.ok) throw Error(b.detail || "Save failed");
      if (b.project_id) persistActiveProjectId(b.project_id);
      setMessage("Demonstration project saved.");
      await refresh();
    } catch (e) {
      setMessage(e.message);
      throw e;
    } finally {
      setBusy(false);
    }
  }

  async function createProjectByFields(name, location) {
    const trimmed = name.trim();
    if (!trimmed) return null;
    const slug =
      trimmed
        .toUpperCase()
        .replace(/[^A-Z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 24) || "PROJ";
    const pid = `${slug}-01`;
    const payload = {
      project_info: {
        project_id: pid,
        project_name: trimmed,
        location: (location || "").trim() || "Philippines",
        design_code: "NSCP 2015",
        materials: {
          fc_mpa: 28.0,
          fy_mpa: 415.0,
          concrete_density_kn_m3: 23.54,
        },
      },
      levels: [{ id: "BASE", elevation_m: 0.0 }],
      columns: [],
    };
    const r = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const b = await r.json();
    if (!r.ok) throw Error(b.detail || "Create failed");
    persistActiveProjectId(pid);
    await refresh();
    return pid;
  }

  async function createNewProject(e) {
    e.preventDefault();
    const trimmed = newName.trim();
    if (!trimmed) return;
    setBusy(true);
    setMessage("");
    try {
      const pid = await createProjectByFields(trimmed, newLocation);
      setNewName("");
      setShowNewForm(false);
      setMessage(`Created project ${trimmed} (${pid}).`);
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeletedProject(deletedId) {
    if (importProject?.project_id === deletedId) setImportProject(null);
    if (geometryProject?.project_id === deletedId) setGeometryProject(null);
    if (sizingProject?.project_id === deletedId) setSizingProject(null);
    if (loadsProject?.project_id === deletedId) setLoadsProject(null);
    if (designProject?.project_id === deletedId) setDesignProject(null);
    if (cadProject?.project_id === deletedId) setCadProject(null);
    if (reportProject?.project_id === deletedId) setReportProject(null);
    if (revProject?.project_info?.project_id === deletedId) setRevProject(null);

    const updatedList = await refresh();
    if (activeProjectId === deletedId) {
      const nextValid = updatedList.find((p) => !p.corrupted && p.project_id !== deletedId);
      persistActiveProjectId(nextValid ? nextValid.project_id : "");
    }
  }

  async function action(id, a) {
    setBusy(true);
    setMessage("");
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(id)}/${a}`, { method: "POST" });
      const b = await r.json();
      if (!r.ok) throw Error(b.detail || "Request failed");
      setMessage(
        a === "staad-preview"
          ? `STAAD.Pro export created: ${b.path} · ${b.nodes} nodes · ${b.members} members. ${b.notice}`
          : a === "validate"
          ? `Schema valid. ${b.warnings.join(" ") || "No basic schema warnings."}`
          : b.message
      );
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }

  const activeWorkspace =
    showModule01 ||
    importProject ||
    geometryProject ||
    sizingProject ||
    loadsProject ||
    designProject ||
    cadProject ||
    reportProject;

  const validProjects = projects.filter((p) => !p.corrupted);
  const activeLeadProject =
    validProjects.find((p) => p.project_id === activeProjectId) || validProjects[0] || null;
  const activePhases = activeLeadProject?.phases_done || {};

  function openModuleForProject(n, proj) {
    if (n === "01") {
      setShowModule01(true);
      return;
    }
    if (!proj || proj.corrupted) return;
    setShowModule01(false);
    persistActiveProjectId(proj.project_id);
    if (n === "02") setImportProject(proj);
    if (n === "03") setGeometryProject(proj);
    if (n === "04") setSizingProject(proj);
    if (n === "05") setLoadsProject(proj);
    if (n === "06") setDesignProject(proj);
    if (n === "07") setCadProject(proj);
    if (n === "08") setReportProject(proj);
  }

  const disclaimerText =
    "Engineering aid only — all structural calculations, BOMs, and plans must be independently verified by a licensed Civil/Structural Engineer prior to signing and sealing.";

  if (!authChecked) {
    return (
      <main className="shell" style={{ minHeight: "100vh", display: "grid", placeItems: "center" }}>
        <div className="geom-panel" style={{ padding: "24px 32px", textAlign: "center" }}>
          <strong style={{ letterSpacing: "0.06em" }}>STRUCTURAL AI OFFICE</strong>
          <p style={{ color: "#94a3b8", marginTop: 8, marginBottom: 0 }}>
            Checking workspace session...
          </p>
        </div>
      </main>
    );
  }

  if (authEnabled && !authenticated) {
    return (
      <main className="shell" style={{ minHeight: "100vh", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
        <div style={{ flex: 1, display: "grid", placeItems: "center", padding: "32px 16px" }}>
          <form
            className="geom-panel"
            onSubmit={handleLoginSubmit}
            style={{ width: "100%", maxWidth: 400, padding: 24, display: "grid", gap: 14 }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <div className="logo">SE</div>
              <div>
                <b style={{ display: "block", letterSpacing: "0.06em" }}>STRUCTURAL AI OFFICE</b>
                <small style={{ color: "#94a3b8" }}>Colleague Beta Access · NSCP 2015</small>
              </div>
            </div>
            {loginError && <div className="pdf-alert error" style={{ margin: 0 }}>{loginError}</div>}
            <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
              <span>Username</span>
              <input
                type="text"
                autoComplete="username"
                placeholder="e.g. engr_juan"
                value={loginUser}
                onChange={(e) => setLoginUser(e.target.value)}
                required
                autoFocus
              />
            </label>
            <label className="pdf-field-chip" style={{ flexDirection: "column", alignItems: "stretch" }}>
              <span>Password</span>
              <input
                type="password"
                autoComplete="current-password"
                placeholder="Enter password"
                value={loginPass}
                onChange={(e) => setLoginPass(e.target.value)}
                required
              />
            </label>
            <button type="submit" disabled={loginBusy || !loginUser.trim() || !loginPass}>
              {loginBusy ? "Signing In..." : "Sign In"}
            </button>
          </form>
        </div>
        <footer>{disclaimerText}</footer>
      </main>
    );
  }

  return (
    <main className="shell">
      {/* Workstation Command Header */}
      <header className="cmd-header">
        <div className="cmd-brand">
          <div className="logo">SE</div>
          <div>
            <b>STRUCTURAL AI OFFICE</b>
            <small>Technical CAD-BIM Structural Engineering Workstation</small>
          </div>
          <span className="code-compliance-pill">NSCP 2015 · NBCP 302-5 · PNS 49</span>
        </div>
        <div className="cmd-actions">
          {!activeWorkspace && (
            <>
              <button disabled={busy} onClick={() => setShowNewForm((v) => !v)}>
                + New Project
              </button>
              <button className="secondary" disabled={busy} onClick={loadSample}>
                Load demonstration project
              </button>
            </>
          )}
          {authEnabled && authenticated && authUsername && (
            <span
              className="dash-active-pill"
              style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
            >
              <code>{authUsername}</code>
              <button
                type="button"
                className="mini-btn"
                disabled={busy}
                onClick={handleLogout}
              >
                Sign Out
              </button>
            </span>
          )}
          <span className={health === "Connected" ? "online" : "status"}>{health}</span>
        </div>
      </header>

      {showModule01 ? (
        <ProjectManagement
          projects={projects}
          activeProject={activeLeadProject}
          onSelectProject={(p) => persistActiveProjectId(p.project_id)}
          onDeletedProject={handleDeletedProject}
          onRefreshProjects={refresh}
          onLoadSample={loadSample}
          onCreateProject={createProjectByFields}
          onOpenModule={(modNum, proj) => openModuleForProject(modNum, proj)}
          onClose={() => setShowModule01(false)}
        />
      ) : importProject ? (
        <PdfImporter
          project={importProject}
          onClose={() => setImportProject(null)}
          onTransferred={async (res) => {
            setMessage(res.notice);
            await refresh();
          }}
        />
      ) : geometryProject ? (
        <GeometryBuilder
          project={geometryProject}
          onClose={() => setGeometryProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : sizingProject ? (
        <MemberSizing
          project={sizingProject}
          onClose={() => setSizingProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : loadsProject ? (
        <LoadGenerator
          project={loadsProject}
          onClose={() => setLoadsProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : designProject ? (
        <DesignReview
          project={designProject}
          onClose={() => setDesignProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : cadProject ? (
        <CadDrawings
          project={cadProject}
          onClose={() => setCadProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : reportProject ? (
        <ReportBuilder
          project={reportProject}
          onClose={() => setReportProject(null)}
          onUpdated={async () => {
            await refresh();
          }}
        />
      ) : (
        <>
          {showNewForm && (
            <form className="new-proj-form cmd-new-proj" onSubmit={createNewProject}>
              <input
                type="text"
                placeholder="Project name (e.g. E2E Browser Frame)"
                aria-label="Project name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                required
              />
              <input
                type="text"
                placeholder="Location"
                aria-label="Project location"
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

          {message && <div className="notice">{message}</div>}

          <div className="dash-section-head">
            <div>
              <label>WORKSPACE</label>
              <h2>Engineering modules</h2>
            </div>
            {activeLeadProject && (
              <span className="dash-active-pill">
                Quick-launch target: <code>{activeLeadProject.project_id}</code>
              </span>
            )}
          </div>

          <section className="grid">
            {MODULES.map(([n, t, d]) => {
              const done = Boolean(activePhases[n]);
              const clickable = n === "01" || Boolean(activeLeadProject);
              return (
                <article
                  key={n}
                  className={clickable ? "module-clickable" : ""}
                  onClick={() => clickable && openModuleForProject(n, activeLeadProject)}
                >
                  <div className="module-card-top">
                    <i>{n}</i>
                    {activeLeadProject && (
                      <span className={`module-phase-dot ${done ? "done" : "pending"}`}>
                        {done ? "READY" : "PENDING"}
                      </span>
                    )}
                  </div>
                  <h3>{t}</h3>
                  <p>{d}</p>
                </article>
              );
            })}
          </section>

          <div className="dash-section-head">
            <div>
              <label>PROJECT DATA</label>
              <h2>Recent projects</h2>
            </div>
            <span className="dash-count-pill">{projects.length} project(s)</span>
          </div>

          <section className="projects">
            {projects.length === 0 ? (
              <div className="proj-empty">
                No projects yet. Click <strong>+ New Project</strong> or{" "}
                <strong>Load demonstration project</strong> above to begin.
              </div>
            ) : (
              projects.map((p) => {
                const ph = p.phases_done || {};
                const completedCount = PHASE_KEYS.filter((k) => ph[k]).length;
                return (
                  <article key={p.project_id}>
                    <div className="proj-meta-col">
                      <div className="proj-title-row">
                        <code className="proj-id-pill">{p.project_id}</code>
                        <h3>{p.project_name}</h3>
                        <span className="proj-zone-pill">Zone {p.seismic_zone || 4}</span>
                        <span className="proj-code-pill">{p.design_code}</span>
                      </div>
                      <p className="proj-sub-row">
                        <span>{p.location}</span>
                        <span>·</span>
                        <span>Revisions: {p.revision_count || 0}</span>
                        <span>·</span>
                        <span>
                          Pipeline: {completedCount}/8 phases
                        </span>
                      </p>
                      <div className="proj-phase-bar" title="Completed modules 01 to 08">
                        {PHASE_KEYS.map((k) => (
                          <span key={k} className={ph[k] ? "phase-seg done" : "phase-seg"}>
                            {k}
                          </span>
                        ))}
                      </div>
                    </div>

                    <nav>
                      {p.corrupted ? (
                        <button
                          className="secondary"
                          onClick={() => setShowModule01(true)}
                          title={p.error || "Corrupted project file — manage in Module 01"}
                        >
                          Manage Corrupted in Module 01
                        </button>
                      ) : (
                        <>
                          <button
                            className="secondary pdf-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("02", p)}
                          >
                            Import PDF
                          </button>
                          <button
                            className="secondary geom-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("03", p)}
                          >
                            3D Geometry
                          </button>
                          <button
                            className="secondary sizing-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("04", p)}
                          >
                            Member Sizing
                          </button>
                          <button
                            className="secondary loads-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("05", p)}
                          >
                            NSCP Loads
                          </button>
                          <button
                            className="secondary design-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("06", p)}
                          >
                            Analysis &amp; Design
                          </button>
                          <button
                            className="secondary cad-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("07", p)}
                          >
                            CAD / DXF
                          </button>
                          <button
                            className="secondary report-launch-btn"
                            disabled={busy}
                            onClick={() => openModuleForProject("08", p)}
                          >
                            Reports &amp; BOM
                          </button>
                          <button
                            className="secondary rev-launch-btn"
                            disabled={busy}
                            onClick={() => openRevisions(p)}
                          >
                            Revisions ({p.revision_count || 0})
                          </button>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() => action(p.project_id, "validate")}
                          >
                            Validate
                          </button>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() => action(p.project_id, "staad-preview")}
                          >
                            STAAD preview
                          </button>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() => action(p.project_id, "simulate")}
                          >
                            Simulation status
                          </button>
                        </>
                      )}
                    </nav>
                  </article>
                );
              })
            )}
          </section>

          {revProject && (
            <section className="rev-drawer">
              <div className="geom-panel">
                <div className="geom-panel-head">
                  <div>
                    <strong>
                      Revision Snapshots &amp; History — {revProject.project_info.project_name} (
                      {revProject.project_info.project_id})
                    </strong>
                    <small>
                      Freeze immutable design milestones (Rev. 0, Rev. 1…) for BOM delta comparison
                      or safe rollback.
                    </small>
                  </div>
                  <button className="secondary" onClick={() => setRevProject(null)}>
                    Close
                  </button>
                </div>
                <form className="rev-create-form" onSubmit={saveRevision}>
                  <input
                    type="text"
                    placeholder="Revision label (e.g. Rev. 0 — Initial Permit Submission)"
                    aria-label="Revision label"
                    value={revLabel}
                    onChange={(e) => setRevLabel(e.target.value)}
                    required
                  />
                  <input
                    type="text"
                    placeholder="Optional engineering notes"
                    aria-label="Revision notes"
                    value={revNotes}
                    onChange={(e) => setRevNotes(e.target.value)}
                  />
                  <button type="submit" disabled={busy}>
                    Save Revision
                  </button>
                </form>
                {(revProject.revisions || []).length === 0 ? (
                  <div className="loads-empty">
                    No saved revisions yet. Click <strong>Save Revision</strong> above to freeze
                    Rev. 0.
                  </div>
                ) : (
                  <div className="sizing-table-wrap">
                    <table className="sizing-table">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>Label</th>
                          <th>Saved</th>
                          <th>Nodes / Members</th>
                          <th>Concrete (m³)</th>
                          <th>Rebar (kg)</th>
                          <th>Est. Cost (PHP)</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...revProject.revisions].reverse().map((r) => (
                          <tr key={r.revision_id}>
                            <td>
                              <strong>{r.revision_id}</strong>
                            </td>
                            <td>
                              {r.label}
                              {r.notes ? (
                                <small style={{ display: "block", color: "#8999ae" }}>
                                  {r.notes}
                                </small>
                              ) : null}
                            </td>
                            <td>{new Date(r.created_at).toLocaleString()}</td>
                            <td>
                              {r.node_count} / {r.member_count}
                            </td>
                            <td>{Number(r.total_concrete_m3 || 0).toFixed(2)}</td>
                            <td>{Number(r.total_rebar_kg || 0).toLocaleString()}</td>
                            <td>
                              ₱
                              {Number(r.grand_total_cost_php || 0).toLocaleString("en-PH", {
                                minimumFractionDigits: 2,
                                maximumFractionDigits: 2,
                              })}
                            </td>
                            <td>
                              <button
                                type="button"
                                className="pdf-chip"
                                disabled={busy}
                                onClick={() => restoreRevision(r.revision_id)}
                              >
                                Restore
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </section>
          )}
        </>
      )}

      <footer>{disclaimerText}</footer>
    </main>
  );
}
