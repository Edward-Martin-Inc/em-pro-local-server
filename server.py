"""
Unity Dev Server — zero-dependency Python local server
  Serves static files from ./public
  JSON CRUD API at /api/json
  Admin UI at /_admin

Usage:  python server.py
        python server.py --port 8080
"""

import http.server
import json
import os
import sys
import urllib.parse
import mimetypes
import collections
import threading
from pathlib import Path
from datetime import datetime

PORT = 3000

# ─── Server-side request log (ring buffer) ───────────────────
MAX_LOG = 200
request_log = collections.deque(maxlen=MAX_LOG)
log_lock = threading.Lock()
log_counter = 0  # monotonic ID so the UI can ask "give me entries after X"


def add_to_log(method, path, status, body_size=0):
    global log_counter
    with log_lock:
        log_counter += 1
        request_log.append({
            "id": log_counter,
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "method": method,
            "path": path,
            "status": status,
            "size": body_size,
        })
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
PUBLIC_DIR = BASE_DIR / "public"

DATA_DIR.mkdir(exist_ok=True)
PUBLIC_DIR.mkdir(exist_ok=True)

# Extra MIME types for 3D / Unity
EXTRA_MIME = {
    ".glb": "model/gltf-binary",
    ".gltf": "application/gltf+json",
    ".hdr": "application/octet-stream",
    ".exr": "application/octet-stream",
    ".fbx": "application/octet-stream",
    ".obj": "text/plain",
    ".mtl": "text/plain",
    ".wasm": "application/wasm",
    ".unityweb": "application/octet-stream",
    ".data": "application/octet-stream",
    ".mem": "application/octet-stream",
    ".ktx2": "image/ktx2",
    ".basis": "application/octet-stream",
    ".dds": "application/octet-stream",
    ".br": "application/octet-stream",
}
for ext, mt in EXTRA_MIME.items():
    mimetypes.add_type(mt, ext)


def safe_path(base: Path, rel: str):
    """Resolve a relative path under base, rejecting traversal."""
    resolved = (base / rel).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        return None
    return resolved


def list_json_files(directory: Path, prefix=""):
    files = []
    if not directory.exists():
        return files
    for entry in sorted(directory.iterdir()):
        rel = f"{prefix}/{entry.name}" if prefix else entry.name
        if entry.is_dir():
            files.extend(list_json_files(entry, rel))
        elif entry.suffix == ".json":
            stat = entry.stat()
            files.append({
                "name": rel,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return files


def parse_body(raw: bytes, content_type: str):
    """Parse request body — handles JSON, form-urlencoded (Unity default), raw text."""
    text = raw.decode("utf-8", errors="replace").strip()
    ct = (content_type or "").lower()

    # Try JSON first regardless of content-type (Unity sometimes lies)
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    if "application/json" in ct:
        return json.loads(text)

    # Form-urlencoded — Unity's UnityWebRequest.Post default
    if "application/x-www-form-urlencoded" in ct:
        params = urllib.parse.parse_qs(text, keep_blank_values=True)
        for key in ("json", "data", "body"):
            if key in params:
                try:
                    return json.loads(params[key][0])
                except (json.JSONDecodeError, IndexError):
                    pass
        # Convert all params to an object
        obj = {}
        for k, v in params.items():
            val = v[0] if len(v) == 1 else v
            try:
                obj[k] = json.loads(val) if isinstance(val, str) else val
            except (json.JSONDecodeError, TypeError):
                obj[k] = val
        return obj

    # Fallback — try JSON
    return json.loads(text)


# ─── Admin HTML ──────────────────────────────────────────────
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Server Admin</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=DM+Sans:wght@400;500;700&display=swap');

  :root {
    --bg: #0e1117;
    --surface: #161b22;
    --surface2: #1c2230;
    --border: #2a3140;
    --text: #e2e8f0;
    --text2: #8891a4;
    --accent: #58a6ff;
    --accent-dim: #1a3a5c;
    --green: #3fb950;
    --green-dim: #1a3d2a;
    --red: #f85149;
    --red-dim: #4a1c1a;
    --orange: #d29922;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'DM Sans', system-ui, sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  .header {
    padding: 24px 32px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    backdrop-filter: blur(8px);
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(14,17,23,0.85);
  }

  .header h1 {
    font-family: var(--mono);
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.5px;
  }

  .header h1 span { color: var(--accent); }

  .status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text2);
    font-family: var(--mono);
  }

  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .container {
    max-width: 1100px;
    margin: 0 auto;
    padding: 32px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }

  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }

  .panel-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface2);
  }

  .panel-header h2 {
    font-size: 13px;
    font-family: var(--mono);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text2);
  }

  .panel-body { padding: 20px; }

  .file-list { list-style: none; }

  .file-item {
    display: grid;
    grid-template-columns: 1fr auto auto auto;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border-radius: 8px;
    font-family: var(--mono);
    font-size: 13px;
    cursor: pointer;
    transition: background 0.15s;
    border: 1px solid transparent;
  }

  .file-item:hover { background: var(--surface2); }
  .file-item.active { background: var(--accent-dim); border-color: var(--accent); }

  .file-name { color: var(--accent); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-size { color: var(--text2); font-size: 11px; }
  .file-date { color: var(--text2); font-size: 11px; }

  .file-delete {
    background: none; border: none; color: var(--red); cursor: pointer;
    opacity: 0; transition: opacity 0.15s;
    font-size: 16px; padding: 4px 8px; border-radius: 4px;
  }
  .file-item:hover .file-delete { opacity: 1; }
  .file-delete:hover { background: var(--red-dim); }

  .empty-state {
    text-align: center;
    padding: 40px;
    color: var(--text2);
    font-size: 14px;
  }

  .editor-toolbar {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
    align-items: center;
  }

  .editor-filename {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 12px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    outline: none;
  }

  .editor-filename:focus { border-color: var(--accent); }

  textarea {
    width: 100%;
    min-height: 320px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.6;
    resize: vertical;
    outline: none;
    tab-size: 2;
  }

  textarea:focus { border-color: var(--accent); }

  .btn {
    padding: 8px 16px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }

  .btn:hover { background: var(--border); }
  .btn-accent { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }
  .btn-accent:hover { background: var(--accent); color: var(--bg); }
  .btn-green { background: var(--green-dim); border-color: var(--green); color: var(--green); }
  .btn-green:hover { background: var(--green); color: var(--bg); }

  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    padding: 12px 20px;
    border-radius: 8px;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    transform: translateY(100px);
    opacity: 0;
    transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    z-index: 100;
  }

  .toast.show { transform: translateY(0); opacity: 1; }
  .toast.success { background: var(--green-dim); border: 1px solid var(--green); color: var(--green); }
  .toast.error { background: var(--red-dim); border: 1px solid var(--red); color: var(--red); }

  .log {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.8;
    max-height: 180px;
    overflow-y: auto;
    color: var(--text2);
  }

  .log-entry { padding: 2px 0; }
  .log-method { font-weight: 700; }
  .log-method.GET { color: var(--green); }
  .log-method.POST, .log-method.PUT, .log-method.PATCH { color: var(--orange); }
  .log-method.DELETE { color: var(--red); }
  .log-time { color: var(--text2); opacity: 0.5; }
  .log-status { color: var(--green); font-weight: 600; margin: 0 2px; }
  .log-status.error { color: var(--red); }
  .log-path { color: var(--text); }
  .log-size { color: var(--text2); opacity: 0.6; font-size: 11px; }

  .snippet {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.6;
    color: var(--text2);
    overflow-x: auto;
    white-space: pre;
    position: relative;
  }

  .snippet .kw { color: var(--accent); }
  .snippet .str { color: var(--green); }
  .snippet .cmt { color: #555; font-style: italic; }

  .copy-btn {
    position: absolute;
    top: 8px;
    right: 8px;
    padding: 4px 10px;
    font-size: 11px;
  }

  @media (max-width: 800px) {
    .container { grid-template-columns: 1fr; padding: 16px; }
  }
</style>
</head>
<body>

<div class="header">
  <h1><span>&gt;</span> unity-dev-server</h1>
  <div class="status"><div class="status-dot"></div>localhost:__PORT__</div>
</div>

<div class="container">
  <div class="panel">
    <div class="panel-header">
      <h2>JSON Files</h2>
      <button class="btn" onclick="loadFiles()">Refresh</button>
    </div>
    <div class="panel-body">
      <ul class="file-list" id="fileList">
        <li class="empty-state">Loading...</li>
      </ul>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Editor</h2>
      <button class="btn btn-accent" onclick="formatJSON()">Format</button>
    </div>
    <div class="panel-body">
      <div class="editor-toolbar">
        <input type="text" id="filename" class="editor-filename" placeholder="filename (e.g. config or levels/level1)">
        <button class="btn btn-green" onclick="saveFile()">Save</button>
        <button class="btn" onclick="newFile()">New</button>
      </div>
      <textarea id="editor" placeholder='{ "enter": "json here" }' spellcheck="false"></textarea>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Request Log</h2>
      <button class="btn" onclick="clearLog()">Clear</button>
    </div>
    <div class="panel-body">
      <div class="log" id="log">
        <div class="log-entry log-placeholder"><span class="log-time">--:--:--</span> Waiting for requests...</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <h2>Unity C# Usage</h2>
    </div>
    <div class="panel-body">
      <div class="snippet" id="snippet"><button class="btn copy-btn" onclick="copySnippet()">Copy</button><span class="cmt">// Read JSON</span>
<span class="kw">IEnumerator</span> GetJSON(<span class="kw">string</span> file) {
  <span class="kw">var</span> req = UnityWebRequest.Get(
    <span class="str">"http://localhost:__PORT__/api/json/"</span> + file);
  <span class="kw">yield return</span> req.SendWebRequest();
  Debug.Log(req.downloadHandler.text);
}

<span class="cmt">// Write JSON</span>
<span class="kw">IEnumerator</span> PostJSON(<span class="kw">string</span> file, <span class="kw">string</span> json) {
  <span class="kw">byte</span>[] bytes = Encoding.UTF8.GetBytes(json);
  <span class="kw">var</span> req = <span class="kw">new</span> UnityWebRequest(
    <span class="str">"http://localhost:__PORT__/api/json/"</span> + file,
    <span class="str">"POST"</span>);
  req.uploadHandler =
    <span class="kw">new</span> UploadHandlerRaw(bytes);
  req.downloadHandler =
    <span class="kw">new</span> DownloadHandlerBuffer();
  req.SetRequestHeader(
    <span class="str">"Content-Type"</span>, <span class="str">"application/json"</span>);
  <span class="kw">yield return</span> req.SendWebRequest();
}</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
var API = "/api/json";
var currentFile = null;
var lastLogId = 0;

async function loadFiles() {
  try {
    var res = await fetch(API);
    var data = await res.json();
    var list = document.getElementById("fileList");

    if (!data.files.length) {
      list.innerHTML = '<li class="empty-state">No JSON files yet. Create one!</li>';
      return;
    }

    list.innerHTML = data.files.map(function(f) {
      var sizeStr = f.size < 1024 ? f.size + " B" : (f.size / 1024).toFixed(1) + " KB";
      var date = new Date(f.modified).toLocaleTimeString();
      var active = currentFile === f.name ? "active" : "";
      var escaped = f.name.replace(/'/g, "\\'");
      return '<li class="file-item ' + active + '" onclick="openFile(\'' + escaped + '\')">'
        + '<span class="file-name">' + f.name + '</span>'
        + '<span class="file-size">' + sizeStr + '</span>'
        + '<span class="file-date">' + date + '</span>'
        + '<button class="file-delete" onclick="event.stopPropagation(); deleteFile(\'' + escaped + '\')" title="Delete">&times;</button>'
        + '</li>';
    }).join("");
  } catch (e) {
    toast("Failed to load files", "error");
  }
}

async function openFile(name) {
  try {
    var res = await fetch(API + "/" + name);
    var data = await res.json();
    document.getElementById("filename").value = name.replace(/\.json$/, "");
    document.getElementById("editor").value = JSON.stringify(data, null, 2);
    currentFile = name;
    loadFiles();
  } catch (e) {
    toast("Failed to load " + name, "error");
  }
}

async function saveFile() {
  var name = document.getElementById("filename").value.trim();
  var body = document.getElementById("editor").value;
  if (!name) return toast("Enter a filename", "error");

  try { JSON.parse(body); } catch (e) { return toast("Invalid JSON: " + e.message, "error"); }

  try {
    var res = await fetch(API + "/" + name, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    });
    var data = await res.json();
    if (data.ok) {
      toast("Saved " + data.file, "success");
      currentFile = data.file;
      loadFiles();
    } else {
      toast(data.error, "error");
    }
  } catch (e) {
    toast("Save failed", "error");
  }
}

async function deleteFile(name) {
  if (!confirm("Delete " + name + "?")) return;
  await fetch(API + "/" + name, { method: "DELETE" });
  if (currentFile === name) { currentFile = null; newFile(); }
  toast("Deleted " + name, "success");
  loadFiles();
}

function newFile() {
  currentFile = null;
  document.getElementById("filename").value = "";
  document.getElementById("editor").value = "";
  loadFiles();
}

function formatJSON() {
  var editor = document.getElementById("editor");
  try {
    editor.value = JSON.stringify(JSON.parse(editor.value), null, 2);
    toast("Formatted", "success");
  } catch (e) {
    toast("Invalid JSON", "error");
  }
}

// ── Server-side log polling ──────────────────
async function pollLog() {
  try {
    var res = await fetch("/api/log?after=" + lastLogId);
    var data = await res.json();
    if (data.entries && data.entries.length > 0) {
      var el = document.getElementById("log");
      // Remove "waiting" placeholder
      var placeholder = el.querySelector(".log-placeholder");
      if (placeholder) placeholder.remove();

      data.entries.forEach(function(e) {
        lastLogId = e.id;
        var entry = document.createElement("div");
        entry.className = "log-entry";

        var statusClass = e.status >= 400 ? "error" : "";
        var sizeStr = e.size > 0 ? ' <span class="log-size">' + formatSize(e.size) + '</span>' : '';

        entry.innerHTML = '<span class="log-time">' + e.time + '</span> '
          + '<span class="log-status ' + statusClass + '">' + e.status + '</span> '
          + '<span class="log-method ' + e.method + '">' + e.method + '</span> '
          + '<span class="log-path">' + e.path + '</span>'
          + sizeStr;
        el.prepend(entry);
      });

      // Cap visible entries
      while (el.children.length > 100) el.removeChild(el.lastChild);
    }
  } catch (e) { /* silently retry next tick */ }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  return (bytes / 1024).toFixed(1) + " KB";
}

async function clearLog() {
  await fetch("/api/log", { method: "DELETE" });
  lastLogId = 0;
  document.getElementById("log").innerHTML = '<div class="log-entry log-placeholder"><span class="log-time">--:--:--</span> Log cleared</div>';
}

function toast(msg, type) {
  var el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show " + type;
  clearTimeout(el._t);
  el._t = setTimeout(function() { el.className = "toast"; }, 2500);
}

function copySnippet() {
  var text = document.getElementById("snippet").innerText.replace("Copy", "").trim();
  navigator.clipboard.writeText(text);
  toast("Copied to clipboard", "success");
}

loadFiles();
pollLog();
setInterval(loadFiles, 10000);
setInterval(pollLog, 5000);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    """Handles API + static file serving."""

    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  {ts}  {args[0]}")

    # ── CORS ─────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Authorization, X-Unity-Version")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self._response_status = status
        self._response_size = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ── OPTIONS (preflight) ──────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── Routing ──────────────────────────────────
    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        pathname = urllib.parse.unquote(parsed.path)

        # Admin UI
        if pathname in ("/_admin", "/_admin/"):
            html = ADMIN_HTML.replace("__PORT__", str(PORT)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self._cors()
            self.end_headers()
            self.wfile.write(html)
            return

        # API: request log — GET /api/log?after=<id>
        if pathname == "/api/log" and method == "GET":
            qs = urllib.parse.parse_qs(parsed.query)
            after_id = int(qs.get("after", [0])[0])
            with log_lock:
                entries = [e for e in request_log if e["id"] > after_id]
            return self._send_json(200, {"entries": entries})

        # API: clear log — DELETE /api/log
        if pathname == "/api/log" and method == "DELETE":
            with log_lock:
                request_log.clear()
            return self._send_json(200, {"ok": True})

        # API: list files
        if pathname == "/api/json" and method == "GET":
            return self._send_json(200, {"files": list_json_files(DATA_DIR)})

        # API: CRUD
        if pathname.startswith("/api/json/"):
            filename = pathname[len("/api/json/"):]
            if not filename or ".." in filename:
                return self._send_json(400, {"error": "Invalid filename"})
            if not filename.endswith(".json"):
                filename += ".json"

            file_path = safe_path(DATA_DIR, filename)
            if not file_path:
                return self._send_json(400, {"error": "Invalid path"})

            if method == "GET":
                if not file_path.exists():
                    return self._send_json(404, {"error": "Not found", "file": filename})
                try:
                    content = json.loads(file_path.read_text("utf-8"))
                    return self._send_json(200, content)
                except Exception as e:
                    return self._send_json(500, {"error": "Parse error", "detail": str(e)})

            if method in ("POST", "PUT", "PATCH"):
                try:
                    raw = self._read_body()
                    data = parse_body(raw, self.headers.get("Content-Type", ""))
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(json.dumps(data, indent=2), "utf-8")
                    return self._send_json(200, {"ok": True, "file": filename})
                except Exception as e:
                    return self._send_json(400, {
                        "error": "Could not parse body as JSON",
                        "detail": str(e),
                        "hint": "Send JSON body, or form-urlencoded with a 'json' field.",
                    })

            if method == "DELETE":
                if not file_path.exists():
                    return self._send_json(404, {"error": "Not found"})
                file_path.unlink()
                return self._send_json(200, {"ok": True, "deleted": filename})

            return self._send_json(405, {"error": "Method not allowed"})

        # Static files
        if method == "GET":
            rel = "index.html" if pathname == "/" else pathname.lstrip("/")
            file_path = safe_path(PUBLIC_DIR, rel)
            if not file_path:
                self._response_status = 403
                self.send_error(403)
                return

            if file_path.is_dir():
                file_path = file_path / "index.html"

            if not file_path.exists():
                self._response_status = 404
                self.send_error(404)
                return

            mime_type, _ = mimetypes.guess_type(str(file_path))
            mime_type = mime_type or "application/octet-stream"

            # Unity compressed builds
            ext = file_path.suffix.lower()
            encoding = None
            if ext == ".gz":
                orig_mime, _ = mimetypes.guess_type(file_path.stem)
                mime_type = orig_mime or "application/octet-stream"
                encoding = "gzip"
            elif ext == ".br":
                orig_mime, _ = mimetypes.guess_type(file_path.stem)
                mime_type = orig_mime or "application/octet-stream"
                encoding = "br"

            data = file_path.read_bytes()
            self._response_status = 200
            self._response_size = len(data)
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(data)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        self._response_status = 404
        self.send_error(404)

    def _handle(self, method):
        """Route + log every request (except internal admin/log polling)."""
        self._response_status = 200
        self._response_size = 0
        parsed = urllib.parse.urlparse(self.path)
        pathname = urllib.parse.unquote(parsed.path)

        # Determine if this is an internal request we shouldn't log
        is_internal = (
            pathname in ("/_admin", "/_admin/")
            or pathname == "/api/log"
            or (pathname == "/api/json" and method == "GET")  # file list polling
        )

        self._route(method)

        if not is_internal:
            add_to_log(method, pathname, self._response_status, self._response_size)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


if __name__ == "__main__":
    # Parse --port flag
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            PORT = int(args[i + 1])
        elif arg.startswith("--port="):
            PORT = int(arg.split("=", 1)[1])

    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"""
  \033[36m┌──────────────────────────────────────────┐
  │   Unity Dev Server  (Python)             │
  ├──────────────────────────────────────────┤
  │                                          │
  │   App:    http://localhost:{PORT}          │
  │   Admin:  http://localhost:{PORT}/_admin   │
  │                                          │
  │   Static: ./public/                      │
  │   Data:   ./data/                        │
  │                                          │
  │   API:                                   │
  │     GET    /api/json       (list)        │
  │     GET    /api/json/:n    (read)        │
  │     POST   /api/json/:n    (write)       │
  │     PUT    /api/json/:n    (write)       │
  │     DELETE /api/json/:n    (delete)      │
  │                                          │
  └──────────────────────────────────────────┘\033[0m
  """)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.server_close()
