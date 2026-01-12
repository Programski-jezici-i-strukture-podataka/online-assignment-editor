import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
import uuid
import time

from flask import Flask, request, send_file, abort, render_template_string, redirect, url_for

app = Flask(__name__)

UPLOAD_FORM = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Build PDF</title>
<style>
  body {
    font-family: sans-serif;
    padding: 2rem;
  }

  button {
    padding: 0.5rem 1.2rem;
    font-size: 1rem;
  }

  #spinner {
    display: none;
    margin-top: 1.5rem;
    align-items: center;
    gap: 0.75rem;
  }

  .loader {
    width: 28px;
    height: 28px;
    border: 4px solid #ddd;
    border-top: 4px solid #333;
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>

<script>
  function startBuild() {
    document.getElementById("spinner").style.display = "flex";
    document.getElementById("submitBtn").disabled = true;
    document.getElementById("submitBtn").innerText = "Building...";
  }
</script>
</head>

<body>
  <h1>Upload ZIP</h1>
  <p>The PDF will be generated after upload.</p>

  <form method="post"
      enctype="multipart/form-data"
      action="/build"
      onsubmit="startBuild()">
    <input type="file" name="file" accept=".zip" required>
    <br><br>
    <button id="submitBtn" type="submit">Build PDF</button>
  </form>

  <div id="spinner">
    <div class="loader"></div>
    <div>Building PDF, please wait…</div>
  </div>
</body>
</html>
"""

# Optional: cap upload size (e.g., 10 MB)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

JOBS: dict[str, tuple[Path, float]] = {}
TTL_SECONDS = 15 * 60

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
    pdf = make_dir / "build" / "pdf" / "zadatak.pdf"

    if not pdf.exists():
        raise ValueError("Error while trying to get the generated PDF.")

    return pdf

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

    job_id = uuid.uuid4().hex
    workdir = Path(tempfile.mkdtemp(prefix=f"job_{job_id}_"))
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

        JOBS[job_id] = (pdf_path, time.time())

        return redirect(url_for("done", job_id=job_id))

    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        return "Build timed out.", 504

@app.get("/done/<job_id>")
def done(job_id):
    if job_id not in JOBS:
        abort(404)

    return f"""
    <!doctype html>
    <html>
    <head>
      <title>PDF Ready</title>
      <style>
        body {{ font-family: sans-serif; padding: 2rem; }}
        a {{ font-size: 1.1rem; }}
      </style>
    </head>
    <body>
      <h1>✅ PDF ready</h1>
      <p>Your document has been generated.</p>
      <a href="/download/{job_id}">Download PDF</a>
      <br><br>
      <a href="/">Build another</a>
    </body>
    </html>
    """

@app.get("/download/<job_id>")
def download(job_id):
    entry = JOBS.get(job_id)
    if not entry:
        abort(404)

    pdf_path, _ = entry
    if not pdf_path or not pdf_path.exists():
        abort(404)

    response = send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=pdf_path.name,
    )

    return response

def cleanup_expired_jobs():
    now = time.time()
    expired = []

    for job_id, (pdf_path, created) in list(JOBS.items()):
        if now - created > TTL_SECONDS:
            expired.append(job_id)

    for job_id in expired:
        pdf_path, _ = JOBS.pop(job_id, (None, None))
        if pdf_path:
            # job root = parent of Makefile dir; adjust depth if needed
            job_root = pdf_path.parents[2]
            shutil.rmtree(job_root, ignore_errors=True)

@app.before_request
def housekeeping():
    cleanup_expired_jobs()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)