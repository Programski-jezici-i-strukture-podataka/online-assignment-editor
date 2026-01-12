import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, request, send_file, abort, render_template_string

app = Flask(__name__)

UPLOAD_FORM = """
<!doctype html>
<title>Build PDF from ZIP</title>
<h1>Upload ZIP (must contain a directory with a Makefile)</h1>
<form method=post enctype=multipart/form-data action="/build">
  <input type=file name=file accept=".zip" required>
  <input type=submit value="Build PDF">
</form>
"""

# Optional: cap upload size (e.g., 10 MB)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract zip into dest_dir, preventing Zip Slip path traversal."""
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            member_path = Path(member.filename)

            # Disallow absolute paths and parent traversal
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Unsafe path in zip: {member.filename}")

            target_path = dest_dir / member_path
            target_path_parent = target_path.parent.resolve()
            if dest_dir.resolve() not in target_path_parent.parents and target_path_parent != dest_dir.resolve():
                raise ValueError(f"Unsafe extraction target: {member.filename}")

        z.extractall(dest_dir)


def pick_make_dir(extract_root: Path) -> Path:
    """
    Choose which directory to run make in.
    Strategy:
      1) If there's exactly one top-level directory and it (or its subtree) contains Makefile, prefer that.
      2) Prefer a Makefile located in a shallow path.
    """
    # List top-level entries
    entries = [p for p in extract_root.iterdir() if p.name not in ("__MACOSX",)]
    top_dirs = [p for p in entries if p.is_dir()]
    top_files = [p for p in entries if p.is_file()]

    # Helper: find all Makefiles
    makefiles = list(extract_root.rglob("Makefile"))
    # Exclude junky macOS metadata paths if present
    makefiles = [m for m in makefiles if "__MACOSX" not in m.parts]

    if not makefiles:
        raise FileNotFoundError("No Makefile found in ZIP.")

    # Prefer: a Makefile at the top level of the extracted root
    for m in makefiles:
        if m.parent.resolve() == extract_root.resolve():
            return extract_root

    # Prefer: if single top-level directory, use the directory containing the shallowest Makefile within it
    if len(top_dirs) == 1 and not top_files:
        base = top_dirs[0]
        candidates = list(base.rglob("Makefile"))
        candidates = [c for c in candidates if "__MACOSX" not in c.parts]
        candidates.sort(key=lambda p: len(p.relative_to(base).parts))
        return candidates[0].parent

    # Otherwise: choose shallowest Makefile overall
    makefiles.sort(key=lambda p: len(p.relative_to(extract_root).parts))
    return makefiles[0].parent


def find_output_pdf(make_dir: Path) -> Path:
    """
    Decide which PDF to return.
    If your Makefile always produces a specific path, hardcode it here.
    Otherwise, pick the newest .pdf under make_dir after build.
    """
    # Example hardcoded output (recommended if you can standardize it):
    # out = make_dir / "out.pdf"
    # if out.exists(): return out

    pdfs = list(make_dir.rglob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError("No PDF produced by build.")
    pdfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0]


@app.get("/")
def index():
    return render_template_string(UPLOAD_FORM)


@app.post("/build")
def build():
    if "file" not in request.files:
        abort(400, "Missing file field")

    f = request.files["file"]
    if not f or not f.filename:
        abort(400, "No file selected")

    if not f.filename.lower().endswith(".zip"):
        abort(400, "Please upload a .zip file")

    workdir = Path(tempfile.mkdtemp(prefix="job_"))
    zip_path = workdir / "upload.zip"
    extract_dir = workdir / "src"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        f.save(zip_path)

        try:
            safe_extract_zip(zip_path, extract_dir)
        except (zipfile.BadZipFile, ValueError) as e:
            abort(400, f"Invalid or unsafe ZIP: {e}")

        make_dir = pick_make_dir(extract_dir)

        # Run make. If your target is different, change "pdf" to "all" or your target name.
        proc = subprocess.run(
            ["make", "-C", str(make_dir), "pdf"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )

        if proc.returncode != 0:
            return (
                "Build failed.\n\n----- build log -----\n" + proc.stdout,
                500,
                {"Content-Type": "text/plain; charset=utf-8"},
            )

        pdf_path = find_output_pdf(make_dir)

        return send_file(
            pdf_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=pdf_path.name if pdf_path.name else "result.pdf",
        )

    except subprocess.TimeoutExpired:
        return "Build timed out.", 504

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)