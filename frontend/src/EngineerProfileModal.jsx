import React, { useEffect, useState } from "react";
import { apiUrl } from "./api.js";

const EMPTY_PROFILE = {
  full_name: "",
  prc_license_no: "",
  prc_validity: "",
  ptr_no: "",
  ptr_date_issued: "",
  ptr_place_issued: "",
  tin: "",
  firm_name: "",
  office_address: "",
  contact_info: "",
};

const PROFILE_FIELDS = [
  { key: "full_name", label: "Full Name", placeholder: "e.g. Engr. Juan Dela Cruz, RCE" },
  { key: "prc_license_no", label: "PRC License No.", placeholder: "e.g. 0123456" },
  { key: "prc_validity", label: "PRC Validity", placeholder: "e.g. 2028-12-31" },
  { key: "ptr_no", label: "PTR No.", placeholder: "e.g. 9876543" },
  { key: "ptr_date_issued", label: "PTR Date Issued", placeholder: "e.g. 2026-01-15" },
  { key: "ptr_place_issued", label: "PTR Place Issued", placeholder: "e.g. Baguio City" },
  { key: "tin", label: "TIN", placeholder: "e.g. 123-456-789-000" },
  { key: "firm_name", label: "Firm / Practice Name", placeholder: "e.g. Dela Cruz Structural Consultancy" },
  { key: "contact_info", label: "Contact Info", placeholder: "e.g. +63 917 000 0000 · engr@firm.ph" },
];

export default function EngineerProfileModal({ open, onClose }) {
  const [form, setForm] = useState(EMPTY_PROFILE);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setSavedMsg("");
    setError("");

    fetch(apiUrl("/api/profile"), { credentials: "include" })
      .then(async (res) => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `Failed to load profile (${res.status})`);
        return body;
      })
      .then((data) => {
        if (cancelled) return;
        setForm({ ...EMPTY_PROFILE, ...(data || {}) });
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Failed to load engineer profile.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(e) {
      if (e.key === "Escape") onClose?.();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  function updateField(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (savedMsg) setSavedMsg("");
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setSaving(true);
    setSavedMsg("");
    setError("");
    try {
      const res = await fetch(apiUrl("/api/profile"), {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Failed to save profile (${res.status})`);
      setForm({ ...EMPTY_PROFILE, ...body });
      setSavedMsg("Engineer profile saved.");
    } catch (err) {
      setError(err.message || "Failed to save engineer profile.");
    } finally {
      setSaving(false);
    }
  }

  const disabled = loading || saving;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Engineer Profile"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.();
      }}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        background: "rgba(5, 9, 18, 0.78)",
        backdropFilter: "blur(4px)",
        display: "grid",
        placeItems: "center",
        padding: 16,
      }}
    >
      <div
        className="geom-panel"
        style={{
          width: "100%",
          maxWidth: 620,
          maxHeight: "90vh",
          overflowY: "auto",
          padding: 20,
          border: "1px solid #334155",
          boxShadow: "0 20px 60px rgba(0,0,0,0.65)",
        }}
      >
        <div className="geom-panel-head" style={{ marginBottom: 8 }}>
          <div>
            <strong>Engineer Profile</strong>
            <small style={{ display: "block", color: "#94a3b8", marginTop: 2 }}>
              Auto-fills DXF title blocks and OBO reports when project engineer fields are blank.
            </small>
          </div>
          <button type="button" className="mini-btn" onClick={onClose}>
            Close
          </button>
        </div>

        {loading && (
          <div className="notice" style={{ marginBottom: 10 }}>
            Loading saved engineer profile...
          </div>
        )}
        {error && (
          <div className="pdf-alert error" style={{ marginBottom: 10 }}>
            {error}
          </div>
        )}
        {savedMsg && (
          <div className="notice" style={{ marginBottom: 10 }}>
            {savedMsg}
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: "grid", gap: 10 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
              gap: 10,
            }}
          >
            {PROFILE_FIELDS.map((f) => (
              <label
                key={f.key}
                className="pdf-field-chip"
                style={{ flexDirection: "column", alignItems: "stretch" }}
              >
                <span>{f.label}</span>
                <input
                  type="text"
                  value={form[f.key] || ""}
                  placeholder={f.placeholder}
                  disabled={disabled}
                  maxLength={200}
                  onChange={(e) => updateField(f.key, e.target.value)}
                />
              </label>
            ))}
          </div>

          <label
            className="pdf-field-chip"
            style={{ flexDirection: "column", alignItems: "stretch" }}
          >
            <span>Office Address</span>
            <input
              type="text"
              value={form.office_address || ""}
              placeholder="e.g. 2F Engineering Bldg., Session Rd., Baguio City"
              disabled={disabled}
              maxLength={400}
              onChange={(e) => updateField("office_address", e.target.value)}
            />
          </label>

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 6 }}>
            <button type="button" className="secondary" disabled={saving} onClick={onClose}>
              Cancel
            </button>
            <button type="submit" disabled={disabled}>
              {saving ? "Saving..." : "Save Profile"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
