from __future__ import annotations

import glob
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ezdxf.addons import odafc

INSTALL_HINT = (
    "Native .dwg export requires a local DXF-to-DWG converter binary. "
    "Install the free ODA File Converter (https://www.opendesign.com/guestfiles/oda_file_converter) "
    "or set ODA_FILE_CONVERTER_PATH to ODAFileConverter.exe, or set AUTOCAD_CORE_CONSOLE_PATH "
    "to AutoCAD's accoreconsole.exe (or install LibreDWG dxf2dwg). "
    "You can also download the .dxf file directly — AutoCAD opens .dxf natively."
)

_ODA_VERSION_MAP = {
    "ACAD2010": "R2010",
    "ACAD2013": "R2013",
    "ACAD2018": "R2018",
}

_AUTOCAD_SAVEAS_MAP = {
    "ACAD2010": "2010",
    "ACAD2013": "2013",
    "ACAD2018": "2018",
}

_LIBREDWG_VERSION_MAP = {
    "ACAD2010": "r2010",
    "ACAD2013": "r2013",
    "ACAD2018": "r2018",
}


class DwgConverterUnavailableError(RuntimeError):
    """Raised when no local DXF -> DWG converter binary is installed on the host."""


def _find_oda_executable() -> str | None:
    env_path = os.environ.get("ODA_FILE_CONVERTER_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        return str(Path(env_path))

    win_patterns = [
        r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
        r"C:\Program Files (x86)\ODA\ODAFileConverter*\ODAFileConverter.exe",
    ]
    for pat in win_patterns:
        matches = sorted(glob.glob(pat), reverse=True)
        for m in matches:
            if Path(m).is_file():
                return m

    which_oda = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
    if which_oda:
        return which_oda

    try:
        if odafc.is_installed():
            cfg_path = getattr(odafc, "win_exec_path", None) or getattr(odafc, "unix_exec_path", None)
            return str(cfg_path) if cfg_path else "ODAFileConverter"
    except Exception:
        pass
    return None


def _find_autocad_core_console() -> str | None:
    env_path = os.environ.get("AUTOCAD_CORE_CONSOLE_PATH", "").strip()
    if env_path and Path(env_path).is_file():
        return str(Path(env_path))

    matches = sorted(
        glob.glob(r"C:\Program Files\Autodesk\AutoCAD 20*\accoreconsole.exe"),
        reverse=True,
    )
    for m in matches:
        if Path(m).is_file():
            return m

    which_ac = shutil.which("accoreconsole") or shutil.which("accoreconsole.exe")
    return which_ac


def _find_libredwg() -> str | None:
    return shutil.which("dxf2dwg") or shutil.which("dxf2dwg.exe")


def detect_dwg_converter() -> dict:
    """
    Detect an available DXF -> binary DWG converter in priority order:
      1. ODA File Converter (ezdxf.addons.odafc)
      2. AutoCAD Core Console (accoreconsole.exe)
      3. LibreDWG (dxf2dwg)
    """
    oda = _find_oda_executable()
    if oda:
        return {
            "available": True,
            "backend": "oda_file_converter",
            "executable_path": oda,
            "install_hint": INSTALL_HINT,
        }

    accore = _find_autocad_core_console()
    if accore:
        return {
            "available": True,
            "backend": "autocad_core_console",
            "executable_path": accore,
            "install_hint": INSTALL_HINT,
        }

    ldwg = _find_libredwg()
    if ldwg:
        return {
            "available": True,
            "backend": "libredwg",
            "executable_path": ldwg,
            "install_hint": INSTALL_HINT,
        }

    return {
        "available": False,
        "backend": None,
        "executable_path": None,
        "install_hint": INSTALL_HINT,
    }


def convert_dxf_to_dwg(dxf_bytes: bytes, dwg_version: str = "ACAD2013") -> bytes:
    """Convert DXF UTF-8 bytes into native binary AutoCAD .dwg bytes."""
    info = detect_dwg_converter()
    if not info["available"] or not info["backend"]:
        raise DwgConverterUnavailableError(info["install_hint"])

    backend = info["backend"]
    exe_path = info["executable_path"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        in_dxf = tmp / "input_plans.dxf"
        out_dwg = tmp / "output_plans.dwg"
        in_dxf.write_bytes(dxf_bytes)

        if backend == "oda_file_converter":
            oda_ver = _ODA_VERSION_MAP.get(dwg_version, "R2013")
            if exe_path and os.name == "nt" and Path(exe_path).is_file():
                odafc.win_exec_path = exe_path
            elif exe_path and os.name != "nt" and Path(exe_path).is_file():
                odafc.unix_exec_path = exe_path
            try:
                odafc.convert(str(in_dxf), str(out_dwg), version=oda_ver, replace=True)
            except Exception as exc:
                raise RuntimeError(f"ODA File Converter failed: {exc}") from exc

        elif backend == "autocad_core_console":
            saveas_ver = _AUTOCAD_SAVEAS_MAP.get(dwg_version, "2013")
            scr_path = tmp / "convert.scr"
            # Escape backslashes cleanly for AutoCAD script
            out_dwg_str = str(out_dwg).replace("\\", "/")
            scr_path.write_text(f'SAVEAS {saveas_ver} "{out_dwg_str}"\nQUIT\n', encoding="utf-8")
            proc = subprocess.run(
                [exe_path, "/i", str(in_dxf), "/s", str(scr_path)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if proc.returncode != 0 or not out_dwg.is_file():
                raise RuntimeError(
                    f"AutoCAD Core Console conversion failed (exit {proc.returncode}): "
                    f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
                )

        elif backend == "libredwg":
            ldwg_ver = _LIBREDWG_VERSION_MAP.get(dwg_version, "r2013")
            proc = subprocess.run(
                [exe_path, "--as", ldwg_ver, "-o", str(out_dwg), str(in_dxf)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0 or not out_dwg.is_file():
                raise RuntimeError(
                    f"LibreDWG dxf2dwg failed (exit {proc.returncode}): "
                    f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
                )
        else:
            raise DwgConverterUnavailableError(info["install_hint"])

        if not out_dwg.is_file() or out_dwg.stat().st_size == 0:
            raise RuntimeError("DWG converter completed without producing a non-empty .dwg file.")

        return out_dwg.read_bytes()


def convert_dwg_to_dxf(dwg_bytes: bytes) -> bytes:
    """Convert binary AutoCAD .dwg bytes into R2010 UTF-8 .dxf bytes using local converter."""
    info = detect_dwg_converter()
    if not info["available"] or not info["backend"]:
        raise DwgConverterUnavailableError(info["install_hint"])

    backend = info["backend"]
    exe_path = info["executable_path"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        in_dwg = tmp / "input_plan.dwg"
        out_dxf = tmp / "output_plan.dxf"
        in_dwg.write_bytes(dwg_bytes)

        if backend == "oda_file_converter":
            if exe_path and os.name == "nt" and Path(exe_path).is_file():
                odafc.win_exec_path = exe_path
            elif exe_path and os.name != "nt" and Path(exe_path).is_file():
                odafc.unix_exec_path = exe_path
            try:
                odafc.convert(str(in_dwg), str(out_dxf), version="R2010", replace=True)
            except Exception as exc:
                raise RuntimeError(f"ODA File Converter (.dwg -> .dxf) failed: {exc}") from exc

        elif backend == "autocad_core_console":
            scr_path = tmp / "dwg2dxf.scr"
            out_dxf_str = str(out_dxf).replace("\\", "/")
            scr_path.write_text(f'DXFOUT "{out_dxf_str}" 6\nQUIT\n', encoding="utf-8")
            proc = subprocess.run(
                [exe_path, "/i", str(in_dwg), "/s", str(scr_path)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if proc.returncode != 0 or not out_dxf.is_file():
                raise RuntimeError(
                    f"AutoCAD Core Console (.dwg -> .dxf) failed (exit {proc.returncode})."
                )

        elif backend == "libredwg":
            dwg2dxf = shutil.which("dwg2dxf") or shutil.which("dwg2dxf.exe")
            if not dwg2dxf:
                raise DwgConverterUnavailableError(info["install_hint"])
            proc = subprocess.run(
                [dwg2dxf, "-o", str(out_dxf), str(in_dwg)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0 or not out_dxf.is_file():
                raise RuntimeError(f"LibreDWG dwg2dxf failed (exit {proc.returncode}).")
        else:
            raise DwgConverterUnavailableError(info["install_hint"])

        if not out_dxf.is_file() or out_dxf.stat().st_size == 0:
            raise RuntimeError("DWG converter did not produce a valid .dxf file.")

        return out_dxf.read_bytes()
