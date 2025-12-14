import os
import socket
import threading
import time
import subprocess

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


# -------------------- helpers --------------------

def is_wsl() -> bool:
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            v = f.read().lower()
        return "microsoft" in v or "wsl" in v
    except Exception:
        return False


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def get_wsl_ip() -> str:
    # IP of the WSL distro (usually 172.17.x.x or similar)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def get_windows_wifi_ip() -> str | None:
    """
    From WSL: ask Windows for the Wi-Fi IPv4.
    """
    ps = r"""
$ip = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.InterfaceAlias -match 'Wi-Fi' -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
  Select-Object -ExpandProperty IPAddress -First 1
$ip
"""
    code, out, _ = run_cmd(["powershell.exe", "-NoProfile", "-Command", ps])
    if code == 0 and out:
        return out.strip()
    return None


def get_native_ip() -> str:
    """
    For native Linux: choose interface used for outbound routing.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# -------------------- Windows port forwarding (WSL) --------------------

def ensure_windows_portproxy(listen_ip: str, listen_port: int, wsl_ip: str, wsl_port: int) -> None:
    """
    On WSL: forward Windows listen_ip:listen_port -> wsl_ip:wsl_port.
    Requires elevated privileges on Windows; if not admin, we print commands.
    """
    del_cmd = f'netsh interface portproxy delete v4tov4 listenport={listen_port} listenaddress={listen_ip}'
    add_cmd = f'netsh interface portproxy add v4tov4 listenport={listen_port} listenaddress={listen_ip} connectport={wsl_port} connectaddress={wsl_ip}'
    fw_cmd  = f'netsh advfirewall firewall add rule name="QShare {listen_port}" dir=in action=allow protocol=TCP localport={listen_port}'

    # Try to run (may fail if not admin)
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", del_cmd], capture_output=True, text=True)
    add = subprocess.run(["powershell.exe", "-NoProfile", "-Command", add_cmd], capture_output=True, text=True)
    fw  = subprocess.run(["powershell.exe", "-NoProfile", "-Command", fw_cmd], capture_output=True, text=True)

    if add.returncode != 0 or fw.returncode != 0:
        print("\n[QShare] Detected WSL. Windows port forwarding is required so your phone can reach the server.")
        print("[QShare] Could not configure portproxy/firewall automatically (needs Admin).")
        print("\nRun this ONE TIME in an *elevated* PowerShell (Run as Administrator):\n")
        print(f"  {del_cmd}")
        print(f"  {add_cmd}")
        print(f"  {fw_cmd}\n")
        if add.stderr:
            print("[QShare] portproxy error:", add.stderr.strip())
        if fw.stderr:
            print("[QShare] firewall error:", fw.stderr.strip())
    else:
        print(f"\n[QShare] Windows portproxy OK: {listen_ip}:{listen_port} -> {wsl_ip}:{wsl_port}")


# -------------------- file listing & API --------------------

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


# -------------------- mDNS registration --------------------

def register_mdns_service(advertise_ip: str, port: int):
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    info = ServiceInfo(
        type_=SERVICE_TYPE,
        name=SERVICE_NAME,
        addresses=[socket.inet_aton(advertise_ip)],
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
    print(f"[{APP_NAME}] Advertised IP: {advertise_ip}")
    print(f"[{APP_NAME}] Open URL: http://{advertise_ip}:{port}")
    print(f"[{APP_NAME}] Shared folder: {SHARED_DIR}\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            zeroconf.unregister_service(info)
        except Exception:
            pass
        zeroconf.close()


def run():
    port = int(os.environ.get("QSHARE_PORT", "54837"))

    # ✅ Always respect explicit override first
    advertise_ip = os.environ.get("QSHARE_IP", "").strip()

    if is_wsl():
        wsl_ip = get_wsl_ip()

        if not advertise_ip:
            # If user didn't override, auto-pick Windows Wi-Fi IP
            win_ip = get_windows_wifi_ip()
            advertise_ip = win_ip or wsl_ip

        # If we are advertising a Windows LAN IP, ensure portproxy points to WSL
        # (If advertise_ip == wsl_ip, portproxy isn't useful for the phone anyway)
        if advertise_ip != wsl_ip:
            ensure_windows_portproxy(advertise_ip, port, wsl_ip, port)

    else:
        if not advertise_ip:
            advertise_ip = get_native_ip()

    t = threading.Thread(target=register_mdns_service, args=(advertise_ip, port), daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run()
