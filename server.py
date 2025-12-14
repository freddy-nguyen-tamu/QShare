import os
import socket
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory, abort
from werkzeug.utils import secure_filename

from zeroconf import IPVersion, ServiceInfo, Zeroconf

APP_NAME = "QShare"
SERVICE_TYPE = "_qshare._tcp.local."
SERVICE_NAME = f"{APP_NAME}._qshare._tcp.local."

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(BASE_DIR, "shared")
os.makedirs(SHARED_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024  # 5GB

def get_local_ip() -> str:
    """
    Finds the LAN IP used to reach the local network.
    This avoids 127.0.0.1 and usually picks your Wi-Fi adapter.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable; just forces OS to choose an interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip

def list_shared_files():
    items = []
    for name in os.listdir(SHARED_DIR):
        path = os.path.join(SHARED_DIR, name)
        if os.path.isfile(path):
            st = os.stat(path)
            items.append({
                "name": name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items

@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "name": APP_NAME, "time": int(time.time())})

@app.get("/api/list")
def api_list():
    return jsonify({
        "ok": True,
        "files": list_shared_files(),
        "serverTime": int(time.time())
    })

@app.get("/download/<path:filename>")
def download(filename):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(SHARED_DIR, safe_name)
    if not os.path.isfile(file_path):
        abort(404)
    return send_from_directory(SHARED_DIR, safe_name, as_attachment=True)

@app.post("/upload")
def upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file field"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    filename = secure_filename(f.filename)
    if not filename:
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    out_path = os.path.join(SHARED_DIR, filename)

    # Avoid overwriting by default: foo.txt -> foo (1).txt
    if os.path.exists(out_path):
        base, ext = os.path.splitext(filename)
        i = 1
        while True:
            candidate = f"{base} ({i}){ext}"
            out_path = os.path.join(SHARED_DIR, candidate)
            if not os.path.exists(out_path):
                filename = candidate
                break
            i += 1

    f.save(out_path)
    return jsonify({"ok": True, "savedAs": filename})

def register_mdns_service(host_ip: str, port: int):
    """
    Advertise the current IP:port as a Zeroconf service so the phone can find it.
    """
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=SERVICE_NAME,
        addresses=[socket.inet_aton(host_ip)],
        port=port,
        properties={
            b"path": b"/",
            b"api": b"/api/list",
            b"app": APP_NAME.encode("utf-8"),
        },
        server=f"{APP_NAME}.local.",
    )

    zeroconf.register_service(info)

    print(f"\n[{APP_NAME}] mDNS advertised as: {SERVICE_NAME}")
    print(f"[{APP_NAME}] Open URL (example): http://{host_ip}:{port}")
    print(f"[{APP_NAME}] Shared folder: {SHARED_DIR}\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        zeroconf.unregister_service(info)
        zeroconf.close()

def run():
    host_ip = get_local_ip()

    # Bind to a free port by asking OS (port 0), then re-use it for Flask
    temp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    temp.bind(("0.0.0.0", 0))
    port = temp.getsockname()[1]
    temp.close()

    t = threading.Thread(target=register_mdns_service, args=(host_ip, port), daemon=True)
    t.start()

    # Run Flask server
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

if __name__ == "__main__":
    run()
