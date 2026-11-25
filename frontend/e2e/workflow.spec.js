import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLE_PDF = path.resolve(__dirname, ".tmp-data", "sample_plan.pdf");
const PROJECT_NAME = "E2E Browser Frame";

function attachRuntimeGuards(page) {
  const pageErrors = [];
  const serverErrors = [];
  page.on("pageerror", (err) => pageErrors.push(err.message || String(err)));
  page.on("response", (res) => {
    if (res.status() >= 500) {
      serverErrors.push(`${res.status()} ${res.url()}`);
    }
  });
  return {
    assertClean() {
      expect(pageErrors, `Uncaught browser JS errors: ${pageErrors.join("; ")}`).toEqual([]);
      expect(serverErrors, `HTTP 5xx server errors: ${serverErrors.join("; ")}`).toEqual([]);
    },
  };
}

async function openProjectModule(page, buttonName) {
  await page.goto("/");
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  const card = page.locator("section.projects article", { hasText: PROJECT_NAME });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: buttonName }).click();
}

test.describe.serial("Structural AI Office — Full Browser E2E (Modules 01 to 06)", () => {
  test("Test 1 — Module 01 & 02: Project Creation, PDF Upload, Zoom, Manual Grid Undo/Redo & Transfer", async ({
    page,
  }) => {
    const guards = attachRuntimeGuards(page);

    // 1. Open Dashboard & create a brand-new project in isolated storage
    await page.goto("/");
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "+ New Project" }).click();
    await page.getByLabel("Project name").fill(PROJECT_NAME);
    await page.getByRole("button", { name: "Create Project" }).click();

    const projectCard = page.locator("section.projects article", { hasText: PROJECT_NAME });
    await expect(projectCard).toBeVisible();

    // 2. Open Module 02 (Import PDF) and upload deterministic 2-page architectural PDF
    await projectCard.getByRole("button", { name: "Import PDF" }).click();
    await expect(page.getByText("MODULE 02 · PDF PLAN IMPORTER")).toBeVisible();

    await page.locator('input[type="file"][accept*="pdf"]').setInputFiles(SAMPLE_PDF);

    // Verify page triage pills render for both pages
    const sheetPills = page.locator(".pdf-sheet-pill");
    await expect(sheetPills).toHaveCount(2);

    // 3. Test precision canvas zoom controls (+, -, Fit)
    const zoomInBtn = page.locator('button[title="Zoom in"]');
    const zoomOutBtn = page.locator('button[title="Zoom out"]');
    const fitBtn = page.locator('button[title="Fit to viewport"]');
    await expect(zoomInBtn).toBeVisible();
    await zoomInBtn.click();
    await expect(page.locator('button[title="Reset to 100%"]')).toHaveText("125%");
    await zoomOutBtn.click();
    await expect(page.locator('button[title="Reset to 100%"]')).toHaveText("100%");
    await fitBtn.click();

    // 4. Switch to Page 1 (Elevation), extract floor-to-floor levels, approve all 4, and transfer
    await sheetPills.nth(0).click();
    await expect(page.getByRole("button", { name: "Extract Floor Levels" })).toBeVisible();
    await page.getByRole("button", { name: "Extract Floor Levels" }).click();

    const levelCards = page.locator(".pdf-level-card");
    await expect(levelCards).toHaveCount(4);
    await expect(page.locator(".pdf-review-col")).not.toContainText("NATURAL GRADE");

    for (let i = 0; i < 4; i++) {
      await levelCards.nth(i).getByRole("button", { name: "Approve", exact: true }).click();
    }
    await page.getByRole("button", { name: /Transfer Approved Levels to Project \(4\)/ }).click();
    await expect(page.locator(".notice")).toContainText(/level/i);

    // 5. Switch to Page 2 (Floor Plan), calibrate scale via 2 clicks on SVG canvas & confirm
    await sheetPills.nth(1).click();
    await expect(page.getByRole("button", { name: "Calibrate (2-Pt)" })).toBeVisible();

    const pageImg = page.locator("img.pdf-page-img");
    await expect(pageImg).toBeVisible();
    await pageImg.evaluate(
      (img) =>
        img.complete && img.naturalWidth > 0
          ? true
          : new Promise((resolve) => {
              img.onload = () => resolve(true);
            })
    );

    const svgOverlay = page.locator("svg.pdf-overlay-svg");
    await expect(svgOverlay).toBeVisible();

    async function clickSvgFraction(fx, fy) {
      await svgOverlay.scrollIntoViewIfNeeded();
      const b = await svgOverlay.boundingBox();
      expect(b).not.toBeNull();
      await svgOverlay.click({
        position: {
          x: Math.round(b.width * fx),
          y: Math.round(b.height * fy),
        },
      });
    }

    // Grid A is at x = 180/760, Grid C is at x = 420/760 (8.0 m apart), Grid 1 at y = 380/520
    await page.getByRole("button", { name: "Calibrate (2-Pt)" }).click();
    await clickSvgFraction(180 / 760, 380 / 520);
    await clickSvgFraction(420 / 760, 380 / 520);

    await page.getByLabel("Known distance").fill("8");
    await expect(page.getByRole("button", { name: "Apply Scale" })).toBeEnabled();
    await page.getByRole("button", { name: "Apply Scale" }).click();
    await expect(page.getByRole("button", { name: "Confirm Scale" })).toBeEnabled();
    await page.getByRole("button", { name: "Confirm Scale" }).click();
    await expect(page.getByRole("button", { name: "Scale Confirmed" })).toBeDisabled();

    // 6. Trace a manual grid line on Page 2, then verify Ctrl+Z (Undo) & Ctrl+Y (Redo)
    await page.getByRole("button", { name: "Trace Grid X (V)" }).click();
    await clickSvgFraction(180 / 760, 0.5);
    await expect(page.getByRole("heading", { name: "Grid Lines (1)" })).toBeVisible();
    await expect(page.locator('button[title*="Undo last manual grid"]')).toContainText("Undo (1)");

    await page.keyboard.press("Control+z");
    await expect(page.getByRole("heading", { name: "Grid Lines (0)" })).toBeVisible();
    await expect(page.locator('button[title*="Redo manual grid"]')).toContainText("Redo (1)");

    await page.keyboard.press("Control+y");
    await expect(page.getByRole("heading", { name: "Grid Lines (1)" })).toBeVisible();

    // 7. Scan shaded columns (preserving traced grid), approve all, and transfer to ProjectState
    await page.getByRole("button", { name: "Scan Columns Only" }).click();
    await expect(page.getByRole("heading", { name: /Columns \(9\)/ })).toBeVisible();

    await page.getByRole("button", { name: "Approve All" }).click();
    const transferBtn = page.getByRole("button", { name: /Transfer Approved to Project/ });
    await expect(transferBtn).toBeEnabled();
    await transferBtn.click();
    await expect(page.locator(".notice")).toContainText(/transfer/i);

    await page.getByRole("button", { name: "Back to Dashboard" }).click();
    guards.assertClean();
  });

  test("Test 2 — Module 03: 3D Frame Generation, 2D Floor Framing & Approval", async ({ page }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "3D Geometry");
    await expect(page.getByText("MODULE 03 · STRUCTURAL GEOMETRY GENERATION")).toBeVisible();

    await page.getByRole("button", { name: "Generate 3D Frame" }).click();

    // Verify non-zero Nodes, Columns, Beams and 0 Errors
    const statValue = (label) =>
      page
        .locator(".geom-stats-grid > div", { has: page.locator(`small:text-is("${label}")`) })
        .locator("strong");

    await expect(statValue("Nodes")).not.toHaveText("0");
    await expect(statValue("Columns")).not.toHaveText("0");
    await expect(statValue("Beams")).not.toHaveText("0");
    await expect(statValue("Errors")).toHaveText("0");

    // 3D wireframe canvas is visible
    await expect(page.locator("canvas.geom-canvas-3d")).toBeVisible();

    // Switch to 2D Floor Framing and verify SVG framing renders
    await page.getByRole("button", { name: "2D Floor Framing" }).click();
    const svg2d = page.locator("svg.geom-svg-2d");
    await expect(svg2d).toBeVisible();
    expect(await svg2d.locator("line").count()).toBeGreaterThan(0);
    expect(await svg2d.locator("circle").count()).toBeGreaterThan(0);

    // Approve geometry
    await page.getByRole("button", { name: "Approve Geometry" }).click();
    await expect(page.getByRole("button", { name: /Geometry Approved/ })).toBeVisible();

    guards.assertClean();
  });

  test("Test 3 — Module 04: Auto-Sizing Preliminary Sections & Approval", async ({ page }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "Member Sizing");
    await expect(page.getByText("MODULE 04 · PRELIMINARY MEMBER SIZING & PROPERTIES")).toBeVisible();

    await page.getByRole("button", { name: "Auto-Size Preliminary Sections" }).click();

    // Assert section catalog cards populate (C1, B1, etc.) and 0 unassigned members remain
    const secCards = page.locator(".sizing-sec-card");
    await expect(secCards.first()).toBeVisible();
    await expect(page.locator(".sizing-sec-list")).toContainText("C1");
    await expect(page.locator(".sizing-sec-list")).toContainText("B1");

    const unassignedVal = page
      .locator(".geom-stats-grid > div", { has: page.locator('small:text-is("Unassigned")') })
      .locator("strong");
    await expect(unassignedVal).toHaveText("0");

    // Approve properties
    await page.getByRole("button", { name: "Approve Properties" }).click();
    await expect(page.getByRole("button", { name: /Properties Approved/ })).toBeVisible();

    guards.assertClean();
  });

  test("Test 4 — Module 05: NSCP 2015 Loads, Combinations & STAAD .std Download", async ({
    page,
  }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "NSCP Loads");
    await expect(page.getByText("MODULE 05 · NSCP 2015 LOAD GENERATION & STAAD.PRO EXPORT")).toBeVisible();

    await page.getByRole("button", { name: "Generate NSCP Loads" }).click();
    await expect(page.locator(".notice")).toContainText(/generated/i);

    // Verify Seismic (NSCP 208) story shear table populates
    await page.getByRole("button", { name: "2. Seismic (NSCP 208)" }).click();
    const seismicRows = page.locator("table.sizing-table tbody tr");
    expect(await seismicRows.count()).toBeGreaterThanOrEqual(3);

    // Verify Load Combinations (LRFD & ASD) tab populates
    await page.getByRole("button", { name: /4\. Load Combinations/ }).click();
    const comboTable = page.locator("table.sizing-table tbody");
    await expect(comboTable).toContainText("LRFD");
    await expect(comboTable).toContainText("ASD");
    expect(await comboTable.locator("tr").count()).toBeGreaterThan(10);

    // Approve loads
    await page.getByRole("button", { name: "Approve Loads" }).click();
    await expect(page.getByRole("button", { name: "Approved ✓" })).toBeVisible();

    // Trigger Download .std and verify downloaded content
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Download .std" }).click(),
    ]);
    const stdPath = await download.path();
    expect(stdPath).toBeTruthy();
    const stdText = fs.readFileSync(stdPath, "utf-8");
    expect(stdText).toContain("STAAD SPACE");
    expect(stdText).toContain("JOINT COORDINATES");
    expect(stdText).toContain("MEMBER INCIDENCES");
    expect(stdText).toContain("DEFINE REFERENCE LOADS");
    expect(stdText).toContain("END DEFINE REFERENCE LOADS");
    expect(stdText).toContain("FLOOR DIAPHRAGM");
    expect(stdText).toContain("R1 1.0");
    expect(stdText.indexOf("END DEFINE REFERENCE LOADS")).toBeLessThan(
      stdText.indexOf("FLOOR DIAPHRAGM")
    );
    expect(stdText).toContain("FINISH");

    guards.assertClean();
  });

  test("Test 5 — Module 06: 3D Analysis, RC Design Schedules & CSV Export", async ({ page }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "Analysis & Design");
    await expect(
      page.getByText("MODULE 06 · ANALYSIS BRIDGE, NSCP 2015 DRIFT CHECKS & RC DESIGN")
    ).toBeVisible();

    await page.getByRole("button", { name: "Run Analysis & RC Design" }).click();
    await expect(page.locator(".notice")).toContainText(/completed/i);

    // 1. Verify Story Drift & Reactions tab renders rows
    await page.getByRole("button", { name: "2. Story Drift & Reactions" }).click();
    expect(await page.locator("table.sizing-table").nth(0).locator("tbody tr").count()).toBeGreaterThanOrEqual(3);
    expect(await page.locator("table.sizing-table").nth(1).locator("tbody tr").count()).toBeGreaterThanOrEqual(9);

    // 2. Verify Beam Schedule tab renders rows
    await page.getByRole("button", { name: /3\. Beam Schedule/ }).click();
    expect(await page.locator("table.sizing-table tbody tr").count()).toBeGreaterThan(0);

    // 3. Verify Column Schedule tab renders rows
    await page.getByRole("button", { name: /4\. Column Schedule/ }).click();
    expect(await page.locator("table.sizing-table tbody tr").count()).toBeGreaterThan(0);

    // 4. Verify Footing Schedule tab renders rows
    await page.getByRole("button", { name: /5\. Footing Schedule/ }).click();
    expect(await page.locator("table.sizing-table tbody tr").count()).toBeGreaterThanOrEqual(9);

    // 5. Trigger Export Schedules (CSV) download and verify non-empty CSV headers
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Export Schedules (CSV)" }).click(),
    ]);
    const csvPath = await download.path();
    expect(csvPath).toBeTruthy();
    const csvText = fs.readFileSync(csvPath, "utf-8");
    expect(csvText.length).toBeGreaterThan(200);
    expect(csvText).toContain("=== BEAM REINFORCEMENT SCHEDULE ===");
    expect(csvText).toContain("=== COLUMN REINFORCEMENT SCHEDULE ===");
    expect(csvText).toContain("=== ISOLATED FOOTING SCHEDULE ===");

    guards.assertClean();
  });

  test("Test 6 — Module 08: Calculation Reports (DOCX & PDF) & Structural BOM", async ({ page }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "Reports & BOM");
    await expect(
      page.getByText("MODULE 08 · CALCULATION REPORTS (DOCX & PDF) & STRUCTURAL BOM")
    ).toBeVisible();

    // 1. Fill signatory details and generate Report & BOM
    await page.getByLabel("Engineer Name").fill("Engr. Playwright Verifier, CE");
    await page.getByLabel("PRC License No.").fill("0192837");
    await page.getByRole("button", { name: "Generate Report & BOM" }).click();
    await expect(page.locator(".notice")).toContainText(/generated/i);

    // 2. Switch to BOM & Cost Estimate tab and verify all 3 tables render
    await page.getByRole("button", { name: "2. Bill of Materials & Cost Estimate (PHP)" }).click();
    await expect(page.locator(".report-grand-banner")).toBeVisible();
    await expect(page.locator("table.sizing-table").nth(0)).toContainText("Isolated Footings");
    await expect(page.locator("table.sizing-table").nth(1)).toContainText("Grade");
    await expect(page.locator("table.sizing-table").nth(2)).toContainText("GRAND TOTAL");

    // 3. Switch to Report Preview tab and verify A4 executive summary renders
    await page.getByRole("button", { name: "3. Report Preview" }).click();
    await expect(page.locator(".report-paper")).toContainText(PROJECT_NAME);
    await expect(page.locator(".report-paper")).toContainText("Engr. Playwright Verifier, CE");

    // 4. Download DOCX, PDF, and BOM CSV and verify magic headers / content
    const [docxDl] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Download DOCX" }).click(),
    ]);
    const docxBytes = fs.readFileSync(await docxDl.path());
    expect(docxBytes.subarray(0, 2).toString("ascii")).toBe("PK");

    const [pdfDl] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Download PDF" }).click(),
    ]);
    const pdfBytes = fs.readFileSync(await pdfDl.path());
    expect(pdfBytes.subarray(0, 5).toString("ascii")).toBe("%PDF-");

    const [bomDl] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Export BOM (CSV)" }).click(),
    ]);
    const bomCsv = fs.readFileSync(await bomDl.path(), "utf-8");
    expect(bomCsv).toContain("=== CONCRETE & FORMWORK TAKEOFF ===");
    expect(bomCsv).toContain("=== PNS 49:2020 REINFORCING STEEL BREAKDOWN ===");

    guards.assertClean();
  });

  test("Test 7 — Module 07: Structural Drawings (AutoCAD .dxf & Live SVG Viewer)", async ({
    page,
  }) => {
    const guards = attachRuntimeGuards(page);
    await openProjectModule(page, "CAD / DXF");
    await expect(
      page.getByText("MODULE 07 · STRUCTURAL DRAWINGS (AUTOCAD .DXF PLANS, SCHEDULES & DETAILS)")
    ).toBeVisible();

    // 1. Generate CAD drawings and verify S-1 SVG preview renders
    await page.getByRole("button", { name: "Generate CAD Drawings" }).click();
    await expect(page.locator(".notice")).toContainText(/Generated 3 AutoCAD sheet/i);
    await expect(page.locator("svg.cad-svg-canvas")).toBeVisible();
    await expect(page.locator('svg.cad-svg-canvas g[data-layer="S-TITLEBLK"]')).toBeVisible();

    // 2. Switch to S-2 and S-3 sheet tabs
    await page.getByRole("button", { name: /S-2 ·/ }).click();
    await expect(page.locator(".geom-legend")).toContainText("S-2");
    await page.getByRole("button", { name: /S-3 ·/ }).click();
    await expect(page.locator(".geom-legend")).toContainText("S-3");

    // 3. Toggle a CAD layer visibility checkbox in the sidebar
    const schedLayerToggle = page.locator(".cad-layer-item", { hasText: "S-SCHED" }).locator('input[type="checkbox"]');
    await schedLayerToggle.uncheck();
    await expect(page.locator('svg.cad-svg-canvas g[data-layer="S-SCHED"]')).toBeHidden();
    await schedLayerToggle.check();

    // 4. Download AutoCAD .dxf and vector PDF and verify valid headers / structure
    const [dxfDl] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Download DXF (.dxf)" }).click(),
    ]);
    const dxfText = fs.readFileSync(await dxfDl.path(), "utf-8");
    expect(dxfText).toContain("SECTION");
    expect(dxfText).toContain("S-TITLEBLK");
    expect(dxfText).toContain("EOF");

    const [cadPdfDl] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "Download PDF (.pdf)" }).click(),
    ]);
    const cadPdfBytes = fs.readFileSync(await cadPdfDl.path());
    expect(cadPdfBytes.subarray(0, 5).toString("ascii")).toBe("%PDF-");

    guards.assertClean();
  });
});
