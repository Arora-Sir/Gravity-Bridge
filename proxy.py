"""
GravityBridge 5.38 - Dynamic HTTP Proxy, Phone Drive & ZIP Auto-Extractor
========================================================================
Architecture:
  [Browser] -> HTTP -> [THIS PROXY :PROXY_PORT]
            -> GET /upload: Serves custom dark portal with Phone Drive explorer
            -> GET /adb-ls: Returns list of phone files/directories via ADB
            -> POST /adb-pull-auto: Automatically searches for a dropped folder name on the phone and pulls it
            -> POST /adb-pull: Triggers local ADB server over tailscale/wi-fi to pull folders/files to laptop
            -> POST /upload: Reconstructs folder hierarchies or auto-extracts ZIP archives on laptop
            -> GET /upload-progress: Tracks true server-side received bytes to bypass VPN buffering
            -> OPTIONS /upload, /adb-pull, /adb-pull-auto, /adb-ls & /upload-progress: CORS preflight
            -> Forces Connection: close on static/unary calls
            -> Forces Accept-Encoding: identity to bypass Gzip/Brotli compression
            -> Bypasses connection close for gRPC streaming
            -> Prepends window.nativeStorage shim into main.js directly
            -> Injects SPA iframe overlay switcher into main.js (instant <300ms switching, no page reload)
            -> Automatically wraps the shim inside a valid HTTP chunk if the response is chunked
            -> Rewrites input file accept attributes in main.js to accept="*/*" to unlock mobile file browser
            -> Injects X-Accel-Buffering: no
            -> Redirects root GET / to active conversation path automatically
            -> SSL -> [Antigravity language_server :65286]
"""

import socket
import threading
import sys
import time
import os
import subprocess
import webbrowser
import re
import urllib.request
import urllib.parse
import json
import ssl
import datetime
import zipfile
import hmac
import hashlib
import base64
import uuid
import secrets
import random

LOCAL_HOST = "127.0.0.1"
LISTEN_HOST = "0.0.0.0"

LAST_ACTIVE_PATH = "" # Fallback active path (set by initial request)
LAST_KNOWN_PHONE_IP = "" # Dynamic client tracking fallback (auto-detected)

# Thread-safe global received bytes tracker to bypass VPN buffering
UPLOAD_PROGRESS = {}
PROGRESS_LOCK = threading.Lock()

# ============================================================
# SECURITY LAYER -- PIN Auth, Sessions, Brute Force Protection
# ============================================================
# AUTH_PIN is loaded from .env file in the same directory.
# NEVER hardcode the PIN here -- edit .env instead.

def _load_env_pin():
    """Reads AUTH_PIN from .env file next to proxy.py. No external deps needed."""
    return _load_env_value("AUTH_PIN", "CHANGE_ME")

def _load_env_value(key, default=""):
    """Reads a value from .env file. No external deps needed."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        print(f"[!] WARNING: .env file not found. Create one with {key}=...")
        return default
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                val = v.strip()
                if len(val) >= 2:
                    if (val[0] == '"' and val[-1] == '"') or \
                       (val[0] == "'" and val[-1] == "'"):
                        val = val[1:-1]
                if val and val != "CHANGE_ME" and val != "your\\path\\to\\adb.exe":
                    return val
    if key == "AUTH_PIN":
        print(f"[!] WARNING: AUTH_PIN not set in .env. Please edit .env and set AUTH_PIN=yourpassphrase")
    else:
        print(f"[!] WARNING: {key} not set in .env. Using default.")
    return default

AUTH_PIN = _load_env_pin()
if AUTH_PIN and AUTH_PIN != "CHANGE_ME":
    print(f"[+] AUTH_PIN loaded from .env ({len(AUTH_PIN)} chars)")

ADB_EXECUTABLE_PATH = _load_env_value("ADB_EXECUTABLE_PATH", "adb")
if ADB_EXECUTABLE_PATH != "adb":
    print(f"[+] ADB_EXECUTABLE_PATH loaded from .env: {ADB_EXECUTABLE_PATH}")

PROXY_PORT = int(_load_env_value("PROXY_PORT", "15842"))
if PROXY_PORT > 65535 or PROXY_PORT < 1:
    print(f"[!] Invalid port {PROXY_PORT} -- must be 1-65535. Using default 15842.")
    PROXY_PORT = 15842
print(f"[+] PROXY_PORT set to: {PROXY_PORT}")

OPENCODE_PORT = int(_load_env_value("OPENCODE_PORT", "14096"))
if OPENCODE_PORT > 65535 or OPENCODE_PORT < 1:
    OPENCODE_PORT = 14096
print(f"[+] OPENCODE_PORT set to: {OPENCODE_PORT}")

NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# --- Load GravityBridge logo from favicon.svg (base64 for inline embedding) ---
_FAVICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.svg")
try:
    with open(_FAVICON_PATH, "rb") as _f:
        _raw_svg = _f.read()
    ICON_B64 = base64.b64encode(_raw_svg).decode("ascii")
except Exception:
    print("[!] WARNING: favicon.svg not found, logo will not display")
    ICON_B64 = ""

# Sessions: {token: {ip, created_at, last_seen, user_agent}}
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

# Failed attempts: {ip: {count, first_fail_time, captcha_q, captcha_a}}
FAILED_ATTEMPTS = {}
FAILED_LOCK = threading.Lock()

# Event log (last 200 entries): [{ts, ip, event, detail}]
CONN_LOG = []
CONN_LOG_LOCK = threading.Lock()

def _hash_pin(pin):
    """SHA-256 hash of PIN for constant-time comparison."""
    return hashlib.sha256(pin.strip().encode('utf-8')).hexdigest()

HASHED_PIN = _hash_pin(AUTH_PIN)

def _log_event(ip, event, detail=""):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with CONN_LOG_LOCK:
        CONN_LOG.append({"ts": ts, "ip": ip, "event": event, "detail": detail})
        if len(CONN_LOG) > 200:
            CONN_LOG.pop(0)

def get_target_adb_device():
    """Locate the target connected ADB device based on LAST_KNOWN_PHONE_IP, with a fallback if multiple are connected."""
    try:
        res = subprocess.run([ADB_EXECUTABLE_PATH, "devices"], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
        lines = res.stdout.splitlines()
    except Exception as e:
        print(f"[!] Error running adb devices: {e}", flush=True)
        return None

    devices = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    print(f"[DEBUG] get_target_adb_device() -> Active devices: {devices}", flush=True)
    print(f"[DEBUG] get_target_adb_device() -> LAST_KNOWN_PHONE_IP: {LAST_KNOWN_PHONE_IP}", flush=True)

    # Try to find a device matching LAST_KNOWN_PHONE_IP first
    if LAST_KNOWN_PHONE_IP:
        # Check direct match (starts with IP: or is exact IP)
        for dev in devices:
            if dev.startswith(f"{LAST_KNOWN_PHONE_IP}:") or dev == LAST_KNOWN_PHONE_IP:
                print(f"[DEBUG] get_target_adb_device() -> Direct match found: {dev}", flush=True)
                return dev

        # Check Tailscale direct LAN IP resolution
        if LAST_KNOWN_PHONE_IP.startswith("100."):
            try:
                ts_res = subprocess.run(["tailscale", "status"], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                for ts_line in ts_res.stdout.splitlines():
                    if LAST_KNOWN_PHONE_IP in ts_line and "direct" in ts_line:
                        match = re.search(r"direct\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", ts_line)
                        if match:
                            lan_ip = match.group(1)
                            print(f"[DEBUG] get_target_adb_device() -> Resolved Tailscale IP {LAST_KNOWN_PHONE_IP} to LAN IP {lan_ip}", flush=True)
                            for dev in devices:
                                if dev.startswith(f"{lan_ip}:") or dev == lan_ip:
                                    print(f"[DEBUG] get_target_adb_device() -> Tailscale LAN match found: {dev}", flush=True)
                                    return dev
            except Exception as e:
                print(f"[!] Error resolving Tailscale LAN IP: {e}", flush=True)

        # Try connecting if the device is not listed
        try:
            print(f"[*] Phone IP {LAST_KNOWN_PHONE_IP} not in active devices. Attempting adb connect...", flush=True)
            subprocess.run([ADB_EXECUTABLE_PATH, "connect", f"{LAST_KNOWN_PHONE_IP}:5555"], capture_output=True, timeout=3.0, creationflags=NO_WINDOW_FLAGS)
            
            res = subprocess.run([ADB_EXECUTABLE_PATH, "devices"], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
            lines = res.stdout.splitlines()
            devices = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith("List of devices"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            
            for dev in devices:
                if dev.startswith(f"{LAST_KNOWN_PHONE_IP}:") or dev == LAST_KNOWN_PHONE_IP:
                    print(f"[DEBUG] get_target_adb_device() -> Matched newly connected device: {dev}", flush=True)
                    return dev
        except Exception as e:
            print(f"[!] Error connecting adb to {LAST_KNOWN_PHONE_IP}: {e}", flush=True)

    if devices:
        print(f"[DEBUG] get_target_adb_device() -> Falling back to first device in list: {devices[0]}", flush=True)
        return devices[0]
    print(f"[DEBUG] get_target_adb_device() -> No devices connected.", flush=True)
    return None

def _extract_cookie_token(request_text):
    m = re.search(r'Cookie:[^\r\n]*gb_session=([a-f0-9]{64})', request_text, re.IGNORECASE)
    return m.group(1) if m else None

def _is_authenticated(request_text, client_ip):
    """Returns True if the request carries a valid, non-expired session token."""
    token = _extract_cookie_token(request_text)
    if not token:
        return False
    with SESSIONS_LOCK:
        s = SESSIONS.get(token)
        if not s:
            return False
        if time.time() - s['created_at'] > 86400:  # 24hr expiry
            del SESSIONS[token]
            return False
        s['last_seen'] = time.time()
        return True

def _make_session_cookie(token):
    return f"gb_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"

def _send_redirect(client_socket, location, extra_headers=""):
    resp = (
        f"HTTP/1.1 302 Found\r\n"
        f"Location: {location}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n\r\n"
    ).encode('utf-8')
    client_socket.sendall(resp)
    try: client_socket.shutdown(socket.SHUT_WR)
    except: pass
    client_socket.close()

def _send_html(client_socket, html, status="200 OK", extra_headers=""):
    body = html.encode('utf-8')
    resp = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n\r\n"
    ).encode('utf-8') + body
    client_socket.sendall(resp)
    try: client_socket.shutdown(socket.SHUT_WR)
    except: pass
    client_socket.close()

def _send_json(client_socket, data, extra_headers="", status="200 OK"):
    body = json.dumps(data).encode('utf-8')
    resp = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n\r\n"
    ).encode('utf-8') + body
    client_socket.sendall(resp)
    try: client_socket.shutdown(socket.SHUT_WR)
    except: pass
    client_socket.close()

def get_pids_by_name(name_query):
    pids = []
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors='ignore', creationflags=flags)
        for line in res.stdout.splitlines():
            parts = line.split(',')
            if len(parts) >= 2:
                img_name = parts[0].strip('"').lower()
                pid_str = parts[1].strip('"')
                if name_query.lower() in img_name and pid_str.isdigit():
                    pids.append(int(pid_str))
    except:
        pass
    return pids

def find_ports_by_pids(pids):
    ports = []
    if not pids:
        return ports
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        res = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors='ignore', creationflags=flags)
        for line in res.stdout.splitlines():
            if "LISTENING" in line:
                parts = line.split()
                if len(parts) >= 5:
                    pid_val = int(parts[-1])
                    if pid_val in pids:
                        local_addr = parts[1]
                        match = re.search(r':(\d+)$', local_addr)
                        if match:
                            ports.append(int(match.group(1)))
    except:
        pass
    return sorted(list(set(ports)))

def find_electron_debug_port():
    try:
        pids = get_pids_by_name("Antigravity")
        ports = find_ports_by_pids(pids)
        for port in sorted(ports, reverse=True):
            if port == PROXY_PORT:
                continue
            try:
                url = f"http://127.0.0.1:{port}/json"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=0.5) as response:
                    data = json.loads(response.read().decode('utf-8', errors='ignore'))
                    if isinstance(data, list) and len(data) > 0:
                        return port
            except:
                pass
    except Exception as e:
        print(f"[!] Error finding debug port: {e}", flush=True)
    return None

def find_language_server_port():
    debug_port = find_electron_debug_port()
    if debug_port:
        try:
            url = f"http://127.0.0.1:{debug_port}/json"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=1) as response:
                data = json.loads(response.read().decode('utf-8', errors='ignore'))
                for page in data:
                    page_url = page.get("url", "")
                    match = re.search(r'https?://127\.0\.0\.1:(\d+)', page_url)
                    if match:
                        port = int(match.group(1))
                        print(f"[+] Found active language server port from Electron: {port}", flush=True)
                        return port
        except:
            pass

    try:
        pids = get_pids_by_name("language_server")
        ports = find_ports_by_pids(pids)
        ports = [p for p in ports if p != PROXY_PORT]
        
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                ctx = ssl._create_unverified_context()
                ss = ctx.wrap_socket(s, server_hostname="127.0.0.1")
                ss.connect(("127.0.0.1", port))
                req = ("GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").encode('utf-8')
                ss.sendall(req)
                resp = ss.recv(256)
                ss.close()
                if resp and resp.startswith(b"HTTP/"):
                    resp_str = resp.decode('utf-8', errors='ignore')
                    if "200" in resp_str:
                        print(f"[+] Found active SSL port (200 OK): {port}", flush=True)
                        return port
            except:
                pass
                
        for port in sorted(ports, reverse=True):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                ctx = ssl._create_unverified_context()
                ss = ctx.wrap_socket(s, server_hostname="127.0.0.1")
                ss.connect(("127.0.0.1", port))
                req = ("GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").encode('utf-8')
                ss.sendall(req)
                resp = ss.recv(256)
                ss.close()
                if resp and resp.startswith(b"HTTP/"):
                    print(f"[+] Found fallback SSL port: {port}", flush=True)
                    return port
            except:
                pass
    except Exception as e:
        print(f"[!] Fallback port scan failed: {e}", flush=True)
    return None

def get_active_conversation_path():
    global LAST_ACTIVE_PATH
    try:
        pids = get_pids_by_name("Antigravity")
        ports = find_ports_by_pids(pids)
        for port in sorted(ports, reverse=True):
            if port == PROXY_PORT:
                continue
            try:
                url = f"http://127.0.0.1:{port}/json"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=0.5) as response:
                    data = json.loads(response.read().decode('utf-8', errors='ignore'))
                    if isinstance(data, list):
                        for page in data:
                            page_url = page.get("url", "")
                            if page.get("type") == "page" and "/c/" in page_url:
                                idx = page_url.find('/c/')
                                if idx != -1:
                                    path = page_url[idx:]
                                    LAST_ACTIVE_PATH = path
                                    return path
            except:
                pass
    except Exception as e:
        print(f"[!] Error in get_active_conversation_path: {e}", flush=True)
    return LAST_ACTIVE_PATH

def get_native_storage_js():
    """
    Returns the JavaScript nativeStorage localStorage shim code.
    """
    return (
        "console.log('[GravityBridge Shim] Injecting HTML5 localStorage shim');\n"
        "window.nativeStorage = {\n"
        "  getItems: function() {\n"
        "    return new Promise(function(resolve) {\n"
        "      var items = {};\n"
        "      for (var i = 0; i < localStorage.length; i++) {\n"
        "        var key = localStorage.key(i);\n"
        "        items[key] = localStorage.getItem(key);\n"
        "      }\n"
        "      resolve(items);\n"
        "    });\n"
        "  },\n"
        "  updateItems: function(changes) {\n"
        "    return new Promise(function(resolve) {\n"
        "      for (var key in changes) {\n"
        "        if (changes.hasOwnProperty(key)) {\n"
        "          var val = changes[key];\n"
        "          if (val === null) {\n"
        "            localStorage.removeItem(key);\n"
        "          } else {\n"
        "            localStorage.setItem(key, val);\n"
        "          }\n"
        "        }\n"
        "      }\n"
        "      resolve();\n"
        "    });\n"
        "  }\n"
        "};\n"
        "window.addEventListener('message', function(c) {\n"
        "  if (c.data && c.data.source === 'antigravity-iframe') {\n"
        "    var cmd = c.data.command;\n"
        "    if (cmd === 'storage:get-items') {\n"
        "      var items = {};\n"
        "      for (var i = 0; i < localStorage.length; i++) {\n"
        "        var key = localStorage.key(i);\n"
        "        items[key] = localStorage.getItem(key);\n"
        "      }\n"
        "      window.postMessage({source: 'antigravity-extension', type: 'storageResponse', id: c.data.id, data: items}, '*');\n"
        "    } else if (cmd === 'storage:update-items') {\n"
        "      var changes = c.data.changes;\n"
        "      for (var key in changes) {\n"
        "        if (changes.hasOwnProperty(key)) {\n"
        "          var val = changes[key];\n"
        "          if (val === null) {\n"
        "            localStorage.removeItem(key);\n"
        "          } else {\n"
        "            localStorage.setItem(key, val);\n"
        "          }\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "});\n"
    )

def get_floating_button_js():
    """
    Injects via /main.js prepend (safe -- no chunked body surgery):
    1. GravityBridge brand favicon + title
    2. Sticky top header bar (logo, IP badge, Logout)
    3. Native bottom nav bar (Chat | Phone Drive | OpenCode)
    4. SPA iframe overlay (#gb-overlay) for instant context switching
    """
    cfg = load_gravitybridge_config()
    tabs_cfg = cfg.get("tabs", {})
    show_chat = tabs_cfg.get("chat", True)
    show_drive = tabs_cfg.get("drive", True)
    show_opencode = tabs_cfg.get("opencode", True)

    js = '''
(function() {
  if (window.self !== window.top) return; // Prevent double injection inside iframes
  if (document.getElementById('gb-nav')) return; // already injected

  var ICON_B64 = "__ICON_B64__";
  var SHOW_CHAT = __SHOW_CHAT__;
  var SHOW_DRIVE = __SHOW_DRIVE__;
  var SHOW_OPENCODE = __SHOW_OPENCODE__;

  // Dynamically set title and favicon
  document.title = "GravityBridge";
  var link = document.querySelector("link[rel*='icon']") || document.createElement('link');
  link.type = 'image/svg+xml';
  link.rel = 'icon';
  link.href = 'data:image/svg+xml;base64,' + ICON_B64;
  document.getElementsByTagName('head')[0].appendChild(link);

  // --- CSS ---
  var style = document.createElement('style');
  style.id = 'gb-nav-css';
  style.textContent = [
    '@keyframes gbSlideUp{from{transform:translateY(100%);opacity:0}to{transform:translateY(0);opacity:1}}',
    '.gb-top-bar{position:fixed!important;top:0!important;left:0!important;right:0!important;height:56px!important;background:rgba(13,14,20,0.92)!important;backdrop-filter:blur(20px)!important;-webkit-backdrop-filter:blur(20px)!important;border-bottom:1px solid rgba(255,255,255,0.08)!important;display:flex!important;justify-content:space-between!important;align-items:center!important;padding:0 16px!important;z-index:2147483647!important;font-family:Outfit,system-ui,sans-serif!important;box-sizing:border-box!important;}',
    '.gb-top-logo{display:flex;align-items:center;gap:8px;}',
    '.gb-top-logo img{width:26px;height:26px;border-radius:50%;display:block;}',
    '.gb-top-logo-text{display:flex;flex-direction:column;line-height:1.15;}',
    '.gb-top-logo-name{font-weight:800;font-size:0.85rem;color:#fff;letter-spacing:-0.2px;}',
    '.gb-top-logo-sub{font-size:0.6rem;color:#fbbf24;font-weight:700;letter-spacing:0.5px;}',
    '.gb-top-right{display:flex;align-items:center;gap:8px;}',
    '@keyframes gbHeartBeat{0%,100%{transform:scale(1)}15%{transform:scale(1.25)}30%{transform:scale(1)}45%{transform:scale(1.15)}60%{transform:scale(1)}}',
    '.gb-ip-badge{background:rgba(251,191,36,0.08);color:#fbbf24;border:1px solid rgba(251,191,36,0.18);padding:2px 7px;border-radius:10px;font-size:0.62rem;font-weight:700;font-family:inherit;white-space:nowrap;}',
    '.gb-logout-btn{background:rgba(239,68,68,0.08);color:#ef4444;border:1px solid rgba(239,68,68,0.15);padding:4px 9px;font-size:0.68rem;font-weight:600;border-radius:6px;cursor:pointer;font-family:inherit;transition:all 0.2s;white-space:nowrap;}',
    '.gb-logout-btn:hover{background:#ef4444;color:#fff;}',
    '.gb-sponsor-btn{background:rgba(217,119,6,0.12);color:#fbbf24;border:1px solid rgba(217,119,6,0.25);padding:4px 9px;font-size:0.68rem;font-weight:600;border-radius:6px;cursor:pointer;font-family:inherit;transition:all 0.2s;white-space:nowrap;display:inline-flex;align-items:center;gap:3px;}',
    '.gb-sponsor-btn:hover{background:#d97706;color:#fff;border-color:#d97706;box-shadow:0 0 8px rgba(217,119,6,0.3);}',
    '.gb-sponsor-btn .gb-heart{display:inline-block;animation:gbHeartBeat 1.8s ease-in-out infinite;}',
    '.gb-top-right{display:flex!important;align-items:center!important;gap:6px!important;flex-shrink:0!important;}',
    '#gb-nav{position:fixed!important;bottom:0!important;left:0!important;right:0!important;z-index:2147483647!important;display:flex!important;height:52px!important;background:#0d0e14!important;border-top:1px solid rgba(255,255,255,0.09)!important;animation:gbSlideUp 0.25s ease-out both!important;}',
    '.gb-tab{flex:1!important;display:flex!important;flex-direction:column!important;align-items:center!important;justify-content:center!important;gap:2px!important;cursor:pointer!important;border:none!important;background:transparent!important;color:rgba(255,255,255,0.35)!important;font-size:0.6rem!important;font-weight:600!important;letter-spacing:0.04em!important;padding:5px 0!important;font-family:Outfit,system-ui,sans-serif!important;min-width:0!important;}',
    '.gb-tab .icon{font-size:1.1rem!important;line-height:1!important;display:block!important;}',
    '.gb-tab.active{color:#fff!important;background:rgba(217,119,6,0.2)!important;}',
    '.gb-tab.active .icon{filter:drop-shadow(0 0 5px rgba(217,119,6,0.9))!important;}',
    '#gb-overlay{position:fixed!important;top:55px!important;left:0!important;width:100%!important;height:calc(100% - 107px)!important;z-index:2147483646!important;display:none!important;background:#0d0e14!important;}',
    '#gb-overlay-opencode{position:fixed!important;top:55px!important;left:0!important;width:100%!important;height:calc(100% - 107px)!important;z-index:2147483646!important;display:none!important;background:#0d0e14!important;flex-direction:column!important;}',
    '#gb-overlay-opencode iframe{width:100%!important;flex:1!important;border:none!important;display:block!important;}',
    '#root{padding-top:55px!important;padding-bottom:52px!important;box-sizing:border-box!important;}',
    'body{background:#0d0e14!important;}'
  ].join('');
  document.head.appendChild(style);

  // --- Top Header Bar ---
  var topBar = document.createElement('header');
  topBar.className = 'gb-top-bar';
  topBar.innerHTML = [
    '<div class="gb-top-logo">',
      '<img src="data:image/svg+xml;base64,' + ICON_B64 + '" alt="GravityBridge" />',
      '<div class="gb-top-logo-text">',
        '<span class="gb-top-logo-name">GravityBridge</span>',
        '<span class="gb-top-logo-sub">WIRELESS DRIVE</span>',
      '</div>',
    '</div>',
    '<div class="gb-top-right">',
      '<button class="gb-sponsor-btn" id="gb-top-sponsor" title="Support the project"><span class="gb-heart">❤️</span> Support</button>',
      '<form method="POST" action="/auth/logout" style="margin:0;padding-right:6px;">',
        '<button type="submit" class="gb-logout-btn">&#128275; Logout</button>',
      '</form>',
    '</div>'
  ].join('');
  document.body.insertBefore(topBar, document.body.firstChild);

  // Set IP badge
  var ipEl = document.getElementById('gb-top-ip');
  if (ipEl) ipEl.textContent = 'IP: ' + window.location.hostname;

  // --- Sponsor Modal ---
  var sponsorModal = document.createElement('div');
  sponsorModal.id = 'gb-sponsor-modal';
  sponsorModal.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;z-index:2147483647;font-family:Outfit,system-ui,sans-serif;box-sizing:border-box;padding:16px;';
  sponsorModal.innerHTML = [
    '<div style="background:linear-gradient(160deg,#0e1318 0%,#131a22 100%);border:1px solid rgba(255,255,255,0.09);border-radius:24px;padding:28px 24px;width:100%;max-width:380px;box-shadow:0 30px 60px -12px rgba(0,0,0,0.9);text-align:center;box-sizing:border-box;animation:gbSlideUp 0.25s ease-out;">',
      '<div style="font-size:2.5rem;margin-bottom:10px;animation:gbHeartBeat 1.8s ease-in-out infinite;">❤️</div>',
      '<div style="font-weight:800;font-size:1.2rem;color:#fff;letter-spacing:-0.3px;margin-bottom:8px;">Support GravityBridge</div>',
      '<p style="font-size:0.78rem;color:rgba(255,255,255,0.55);line-height:1.5;margin-bottom:24px;">If this tool saves you time, consider buying me a coffee. One tap opens your payment app directly.</p>',
      '<div style="display:flex;gap:12px;justify-content:center;margin-bottom:20px;">',
        '<a id="gb-pay-upi" href="upi://pay?pa=mohit1998arora@yescred&pn=Mohit%20Arora&cu=INR&tn=GravityBridge" style="flex:1;text-decoration:none;background:linear-gradient(135deg,rgba(217,119,6,0.2),rgba(251,191,36,0.1));border:1px solid rgba(217,119,6,0.35);border-radius:14px;padding:16px 12px;display:flex;flex-direction:column;align-items:center;gap:6px;transition:all 0.2s;cursor:pointer;">',
          '<span style="font-size:1.6rem;">🇮🇳</span>',
          '<span style="font-weight:700;font-size:0.82rem;color:#fbbf24;">Pay via UPI</span>',
          '<span style="font-size:0.65rem;color:rgba(255,255,255,0.4);">GPay / PhonePe / Cred</span>',
        '</a>',
        '<a id="gb-pay-paypal" href="https://paypal.me/arorasir" target="_blank" rel="noopener" style="flex:1;text-decoration:none;background:linear-gradient(135deg,rgba(0,112,201,0.2),rgba(0,148,255,0.1));border:1px solid rgba(0,148,255,0.3);border-radius:14px;padding:16px 12px;display:flex;flex-direction:column;align-items:center;gap:6px;transition:all 0.2s;cursor:pointer;">',
          '<span style="font-size:1.6rem;">💳</span>',
          '<span style="font-weight:700;font-size:0.82rem;color:#60a5fa;">PayPal</span>',
          '<span style="font-size:0.65rem;color:rgba(255,255,255,0.4);">paypal.me/arorasir</span>',
        '</a>',
      '</div>',
      '<button id="gb-sponsor-close" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);color:rgba(255,255,255,0.5);border-radius:10px;padding:8px 20px;font-size:0.75rem;font-weight:600;cursor:pointer;font-family:inherit;transition:all 0.2s;">Maybe later</button>',
    '</div>'
  ].join('');
  document.body.appendChild(sponsorModal);

  // --- Phone Drive Overlay iframe ---
  if (SHOW_DRIVE) {
    var overlay = document.createElement('div');
    overlay.id = 'gb-overlay';
    overlay.innerHTML = '<iframe id="gb-iframe" src="/upload" style="width:100%;height:100%;border:none;display:block;"></iframe>';
    document.body.appendChild(overlay);
  }

  // --- OpenCode Overlay iframe ---
  if (SHOW_OPENCODE) {
    var ocUrl = 'http://' + window.location.hostname + ':__OPENCODE_PORT__';
    var overlayOC = document.createElement('div');
    overlayOC.id = 'gb-overlay-opencode';
    overlayOC.style.cssText = 'position:fixed;top:56px;left:0;width:100%;height:calc(100% - 108px);z-index:99999;display:none;flex-direction:column;background:#0d0e14;';
    overlayOC.innerHTML = '<div style="background:#161a23;padding:6px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,0.08);font-family:Outfit,sans-serif;font-size:0.8rem;color:#a1a1aa;"><span style="display:flex;align-items:center;gap:6px;"><span style="color:#10b981;">●</span> OpenCode Live Web IDE</span><a id="gb-iframe-opencode-popout" href="' + ocUrl + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();" style="color:#60a5fa;text-decoration:none;font-weight:600;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.3);border-radius:6px;padding:3px 10px;font-size:0.75rem;">Open in New Window ↗</a></div><iframe id="gb-iframe-opencode" src="' + ocUrl + '" style="width:100%;height:100%;border:none;display:block;background:#0d0e14;flex:1;"></iframe>';
    document.body.appendChild(overlayOC);
  }

  // --- Bottom Nav Bar ---
  var nav = document.createElement('nav');
  nav.id = 'gb-nav';
  nav.setAttribute('role', 'navigation');
  nav.setAttribute('aria-label', 'GravityBridge navigation');
  var tabsArr = [];
  if (SHOW_CHAT) tabsArr.push('<button class="gb-tab active" id="gb-tab-chat"><span class="icon">&#128172;</span><span>Chat</span></button>');
  if (SHOW_DRIVE) tabsArr.push('<button class="gb-tab" id="gb-tab-upload"><span class="icon">&#128241;</span><span>Phone Drive</span></button>');
  if (SHOW_OPENCODE) tabsArr.push('<button class="gb-tab" id="gb-tab-opencode"><span class="icon">&#128187;</span><span>OpenCode</span></button>');
  nav.innerHTML = tabsArr.join('');
  document.body.appendChild(nav);

  // --- Tab Switch Logic ---
  var tabChat = document.getElementById('gb-tab-chat');
  var tabUpload = document.getElementById('gb-tab-upload');
  var tabOpencode = document.getElementById('gb-tab-opencode');
  var subLabel = document.querySelector('.gb-top-logo-sub');
  var overlay = document.getElementById('gb-overlay');
  var overlayOC = document.getElementById('gb-overlay-opencode');

  function setActive(tab) {
    if (tabChat) tabChat.classList.toggle('active', tab === 'chat');
    if (tabUpload) tabUpload.classList.toggle('active', tab === 'upload');
    if (tabOpencode) tabOpencode.classList.toggle('active', tab === 'opencode');
    if (overlay) overlay.style.setProperty('display', tab === 'upload' ? 'block' : 'none', 'important');
    if (overlayOC) overlayOC.style.setProperty('display', tab === 'opencode' ? 'flex' : 'none', 'important');
    if (subLabel) {
      subLabel.textContent = tab === 'opencode' ? 'OPENCODE' : tab === 'upload' ? 'WIRELESS DRIVE' : 'AGENT MODE';
    }
  }

  // Event Listeners
  if (document.getElementById('gb-top-sponsor')) {
    document.getElementById('gb-top-sponsor').addEventListener('click', function() {
      sponsorModal.style.display = 'flex';
    });
  }
  if (document.getElementById('gb-sponsor-close')) {
    document.getElementById('gb-sponsor-close').addEventListener('click', function() {
      sponsorModal.style.display = 'none';
    });
  }
  sponsorModal.addEventListener('click', function(e) {
    if (e.target === sponsorModal) sponsorModal.style.display = 'none';
  });

  if (tabChat) tabChat.addEventListener('click', function() { setActive('chat'); });
  if (tabUpload) tabUpload.addEventListener('click', function() { setActive('upload'); });
  if (tabOpencode) {
    tabOpencode.addEventListener('click', function() {
      var iframe = document.getElementById('gb-iframe-opencode');
      var popout = document.getElementById('gb-iframe-opencode-popout');
      var targetUrl = 'http://' + window.location.hostname + ':__OPENCODE_PORT__';
      if (popout) popout.href = targetUrl;
      if (iframe && (!iframe.src || iframe.src.indexOf(':__OPENCODE_PORT__') === -1)) {
        iframe.src = targetUrl;
      }
      setActive('opencode');
    });
  }
  window.addEventListener('message', function(e) {
    if (e.data === 'gb:back-to-chat') { setActive('chat'); }
    else if (e.data === 'gb:switch-opencode') { setActive('opencode'); }
  });
})();
'''
    return (
        js.replace("__ICON_B64__", ICON_B64)
        .replace("__OPENCODE_PORT__", str(OPENCODE_PORT))
        .replace("__SHOW_CHAT__", "true" if show_chat else "false")
        .replace("__SHOW_DRIVE__", "true" if show_drive else "false")
        .replace("__SHOW_OPENCODE__", "true" if show_opencode else "false")
    )

def _read_post_body(initial_data, client_socket):
    """Reads full POST body using Content-Length header, buffering remaining bytes from socket."""
    try:
        header_end = initial_data.find(b'\r\n\r\n')
        if header_end == -1:
            return b''
        headers_raw = initial_data[:header_end].decode('utf-8', errors='ignore')
        body_so_far = initial_data[header_end + 4:]
        m = re.search(r'Content-Length:\s*(\d+)', headers_raw, re.IGNORECASE)
        content_length = int(m.group(1)) if m else 0
        while len(body_so_far) < content_length:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            body_so_far += chunk
        return body_so_far
    except:
        return b''


def serve_lock_screen(client_socket, client_ip, msg="", attempts_left=5, show_captcha=False, captcha_q=""):
    """Serves the PIN lock screen -- dark glassmorphism, phone-lock style."""
    warn = ""
    if msg:
        color = "#ef4444" if ("Too many" in msg or "locked" in msg) else "#f59e0b"
        warn = f'<div class="warn" style="color:{color}">{msg}</div>'
    captcha_html = ""
    if show_captcha:
        captcha_html = f"""
        <div class="captcha-row">
          <span class="captcha-label">Prove you're human: {captcha_q} = </span>
          <input type="number" name="captcha" id="captcha" class="captcha-input" required autocomplete="off" />
        </div>"""
    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>GravityBridge &mdash; Locked</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,__ICON_B64__"/>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Outfit', sans-serif;
    background: #060709;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background-image: radial-gradient(ellipse at 30% 20%, rgba(217,119,6,0.06) 0%, transparent 60%),
                      radial-gradient(ellipse at 80% 80%, rgba(99,102,241,0.05) 0%, transparent 60%);
  }}
  .card {{
    background: rgba(17,19,28,0.9);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 24px;
    padding: 44px 36px;
    width: 100%;
    max-width: 360px;
    text-align: center;
    box-shadow: 0 24px 60px rgba(0,0,0,0.6);
    animation: fadeUp 0.3s ease-out;
  }}
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(18px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .lock-icon {{ font-size: 2.8rem; margin-bottom: 12px; }}
  h1 {{ font-size: 1.4rem; font-weight: 800; color: #fff; margin-bottom: 6px; }}
  .sub {{ font-size: 0.82rem; color: rgba(255,255,255,0.4); margin-bottom: 28px; }}
  .pin-input {{
    width: 100%;
    background: rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    color: #fff;
    font-size: 1.1rem;
    font-family: 'Outfit', sans-serif;
    padding: 14px 16px;
    text-align: center;
    letter-spacing: 0.15em;
    outline: none;
    margin-bottom: 16px;
    transition: border-color 0.2s;
  }}
  .pin-input:focus {{ border-color: #d97706; }}
  .unlock-btn {{
    width: 100%;
    background: #d97706;
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 14px;
    font-size: 1rem;
    font-weight: 700;
    font-family: 'Outfit', sans-serif;
    cursor: pointer;
    transition: background 0.2s, transform 0.1s;
    margin-bottom: 14px;
  }}
  .unlock-btn:hover {{ background: #b45309; }}
  .unlock-btn:active {{ transform: scale(0.98); }}
  .warn {{ font-size: 0.82rem; margin-bottom: 12px; font-weight: 600; }}
  .attempts {{ font-size: 0.78rem; color: rgba(255,255,255,0.35); margin-bottom: 14px; }}
  .captcha-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 14px; justify-content: center; }}
  .captcha-label {{ font-size: 0.85rem; color: rgba(255,255,255,0.6); }}
  .captcha-input {{ width: 64px; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.15);
    border-radius: 8px; color: #fff; font-size: 0.95rem; padding: 8px; text-align: center; outline: none; }}
  .captcha-input:focus {{ border-color: #d97706; }}
</style>
</head><body>
<div class="card">
  <div class="lock-icon">&#128274;</div>
  <h1>GravityBridge</h1>
  <p class="sub">Secure access required</p>
  {warn}
  <form method="POST" action="/auth" autocomplete="on">
    <!-- Hidden username anchors the credential in the browser's password manager -->
    <input type="text" name="username" id="username"
           value="gravitybridge"
           autocomplete="username"
           style="display:none;position:absolute;opacity:0;height:0;width:0;pointer-events:none;"
           aria-hidden="true" tabindex="-1" />
    <input type="password" name="pin" id="pin" class="pin-input"
           placeholder="Enter passphrase"
           autofocus
           autocomplete="current-password" />
    {captcha_html}
    <button type="submit" class="unlock-btn">&#128275; Unlock</button>
  </form>
  {'<div class="attempts">' + str(attempts_left) + ' attempt(s) remaining before lockout</div>' if attempts_left < 5 else ''}
</div>
<script>document.getElementById('pin').focus();</script>
</body></html>"""
    html = html.replace("__ICON_B64__", ICON_B64)
    _send_html(client_socket, html)


def serve_dashboard(client_socket, client_ip):
    """Live security dashboard -- sessions, blocked IPs, event log."""
    now = time.time()
    with SESSIONS_LOCK:
        sessions_copy = dict(SESSIONS)
    with FAILED_LOCK:
        failed_copy = dict(FAILED_ATTEMPTS)
    with CONN_LOG_LOCK:
        log_copy = list(reversed(CONN_LOG[-50:]))

    sess_rows = ""
    for token, s in sessions_copy.items():
        age = int((now - s['created_at']) / 60)
        seen = int(now - s['last_seen'])
        seen_str = f"{seen}s ago" if seen < 120 else f"{int(seen/60)}m ago"
        age_str = f"{age}m ago" if age < 120 else f"{int(age/60)}h ago"
        short_token = token[:8] + "..."
        you = " (you)" if s['ip'] == client_ip else ""
        sess_rows += f"""
        <tr>
          <td>{s['ip']}{you}</td><td>{age_str}</td><td>{seen_str}</td>
          <td><span style="font-size:0.7rem;color:rgba(255,255,255,0.4)">{short_token}</span></td>
          <td><form method="POST" action="/auth/revoke" style="margin:0">
            <input type="hidden" name="token" value="{token}">
            <button class="kill-btn" type="submit">&#128683; Kill</button>
          </form></td>
        </tr>"""
    if not sess_rows:
        sess_rows = '<tr><td colspan="5" style="color:rgba(255,255,255,0.3);text-align:center">No active sessions</td></tr>'

    blocked_rows = ""
    blocked_count = 0
    for ip, entry in failed_copy.items():
        if entry.get('count', 0) >= 5:
            elapsed = now - entry.get('first_fail_time', now)
            remaining = max(0, int((900 - elapsed) / 60))
            if remaining > 0:
                blocked_count += 1
                blocked_rows += f"<tr><td>{ip}</td><td>{entry['count']} / 5</td><td>{remaining} min</td></tr>"
    if not blocked_rows:
        blocked_rows = '<tr><td colspan="3" style="color:rgba(255,255,255,0.3);text-align:center">No blocked IPs</td></tr>'

    log_rows = ""
    for e in log_copy:
        icon = "&#9989;" if "success" in e['event'].lower() else "&#10060;" if ("fail" in e['event'].lower() or "lock" in e['event'].lower() or "blocked" in e['event'].lower()) else "&#8505;&#65039;"
        log_rows += f"<tr><td>{e['ts']}</td><td>{e['ip']}</td><td>{icon} {e['event']}</td><td style='color:rgba(255,255,255,0.5)'>{e['detail']}</td></tr>"
    if not log_rows:
        log_rows = '<tr><td colspan="4" style="color:rgba(255,255,255,0.3);text-align:center">No events yet</td></tr>'

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GravityBridge &mdash; Security Dashboar<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,__ICON_B64__">c3ZnPgo=">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Outfit',sans-serif; background:#060709; color:#e2e8f0; padding:24px;
    background-image:radial-gradient(ellipse at 20% 10%,rgba(217,119,6,0.05) 0%,transparent 60%); }}
  h1 {{ font-size:1.5rem; font-weight:800; color:#fff; margin-bottom:4px; }}
  .sub {{ font-size:0.82rem; color:rgba(255,255,255,0.4); margin-bottom:28px; }}
  .section {{ background:rgba(17,19,28,0.8); border:1px solid rgba(255,255,255,0.07);
    border-radius:16px; padding:20px; margin-bottom:20px; overflow-x:auto; }}
  .section h2 {{ font-size:0.9rem; font-weight:700; color:rgba(255,255,255,0.7);
    text-transform:uppercase; letter-spacing:0.08em; margin-bottom:14px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
  th {{ text-align:left; color:rgba(255,255,255,0.4); font-weight:600;
    padding:6px 10px; border-bottom:1px solid rgba(255,255,255,0.06); }}
  td {{ padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.04); color:rgba(255,255,255,0.8); word-break:break-all; }}
  tr:last-child td {{ border-bottom:none; }}
  .kill-btn {{ background:#7f1d1d; color:#fca5a5; border:1px solid #991b1b;
    border-radius:6px; padding:4px 10px; font-size:0.75rem; cursor:pointer; font-family:'Outfit',sans-serif; }}
  .kill-btn:hover {{ background:#991b1b; }}
  .action-btn {{ display:inline-block; background:rgba(255,255,255,0.05);
    border:1px solid rgba(255,255,255,0.1); color:rgba(255,255,255,0.7);
    padding:8px 18px; border-radius:10px; font-size:0.82rem; cursor:pointer;
    text-decoration:none; font-family:'Outfit',sans-serif; margin-right:10px; }}
  .action-btn:hover {{ background:rgba(255,255,255,0.1); }}
  .meta {{ font-size:0.75rem; color:rgba(255,255,255,0.25); margin-top:20px; text-align:center; }}
</style>
</head><body>
<div style="max-width:900px;margin:0 auto">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px;flex-wrap:wrap;gap:12px">
    <div>
      <h1>&#128737;&#65039; Security Dashboard</h1>
      <p class="sub">GravityBridge &mdash; Viewing as {client_ip}</p>
    </div>
    <div>
      <a href="/upload" class="action-btn">&#128241; Portal</a>
      <form method="POST" action="/auth/logout" style="display:inline">
        <button class="action-btn" type="submit">&#128275; Logout</button>
      </form>
    </div>
  </div>

  <div class="section">
    <h2>&#127758; Active Sessions ({len(sessions_copy)})</h2>
    <table><thead><tr><th>IP</th><th>Logged In</th><th>Last Seen</th><th>Token</th><th>Action</th></tr></thead>
    <tbody>{sess_rows}</tbody></table>
  </div>

  <div class="section">
    <h2>&#128680; Blocked IPs ({blocked_count})</h2>
    <table><thead><tr><th>IP</th><th>Attempts</th><th>Unblocks In</th></tr></thead>
    <tbody>{blocked_rows}</tbody></table>
  </div>

  <div class="section">
    <h2>&#128196; Recent Events (last 50)</h2>
    <table><thead><tr><th>Time</th><th>IP</th><th>Event</th><th>Detail</th></tr></thead>
    <tbody>{log_rows}</tbody></table>
  </div>

  <p class="meta">Auto-refreshes every 10 seconds &bull; Sessions expire after 24h &bull; IPs unlock after 15 min</p>
</div>
<script>setTimeout(function(){{location.reload();}}, 10000);</script>
</body></html>"""
    html = html.replace("__ICON_B64__", ICON_B64)
    _send_html(client_socket, html)


def handle_auth_post(client_socket, client_ip, request_text, body_raw):
    """Handles POST /auth -- validates PIN, manages brute force, issues session cookie."""
    try:
        body_str = body_raw.decode('utf-8', errors='ignore')
        params = dict(urllib.parse.parse_qsl(body_str))
    except:
        params = {}

    submitted_pin = params.get('pin', '').strip()
    submitted_captcha = params.get('captcha', '').strip()

    with FAILED_LOCK:
        entry = FAILED_ATTEMPTS.get(client_ip, {'count': 0, 'first_fail_time': time.time()})
        count = entry.get('count', 0)

        # Check lockout (15 min)
        if count >= 5:
            elapsed = time.time() - entry.get('first_fail_time', time.time())
            if elapsed < 900:
                remaining = max(1, int((900 - elapsed) / 60))
                _log_event(client_ip, "Lockout rejected", f"{remaining}m remaining")
                serve_lock_screen(client_socket, client_ip,
                    msg=f"&#128274; Too many attempts. Locked for {remaining} more minute(s).",
                    attempts_left=0)
                return
            else:
                entry = {'count': 0, 'first_fail_time': time.time()}
                FAILED_ATTEMPTS[client_ip] = entry
                count = 0

        # Validate CAPTCHA if >= 3 fails
        if count >= 3:
            expected = entry.get('captcha_a', None)
            if expected is None or submitted_captcha != str(expected):
                _log_event(client_ip, "CAPTCHA failed", f"expected={expected} got={submitted_captcha}")
                a1 = random.randint(1, 15)
                a2 = random.randint(1, 15)
                entry['captcha_q'] = f"{a1} + {a2}"
                entry['captcha_a'] = a1 + a2
                FAILED_ATTEMPTS[client_ip] = entry
                serve_lock_screen(client_socket, client_ip,
                    msg="Wrong answer -- please try again.",
                    attempts_left=max(0, 5 - count),
                    show_captcha=True, captcha_q=entry['captcha_q'])
                return

        # Validate PIN -- constant-time compare (prevents timing attacks)
        submitted_hash = _hash_pin(submitted_pin)
        if not hmac.compare_digest(submitted_hash, HASHED_PIN):
            entry['count'] = count + 1
            if count == 0:
                entry['first_fail_time'] = time.time()
            remaining = max(0, 5 - entry['count'])
            _log_event(client_ip, "Wrong PIN", f"attempt {entry['count']}/5")

            show_cap = entry['count'] >= 3
            if show_cap and 'captcha_q' not in entry:
                a1 = random.randint(1, 15)
                a2 = random.randint(1, 15)
                entry['captcha_q'] = f"{a1} + {a2}"
                entry['captcha_a'] = a1 + a2
            FAILED_ATTEMPTS[client_ip] = entry

            if entry['count'] >= 5:
                _log_event(client_ip, "IP LOCKED OUT", "15 min ban")
                serve_lock_screen(client_socket, client_ip,
                    msg="&#128274; Too many failed attempts. Locked for 15 minutes.",
                    attempts_left=0)
            else:
                serve_lock_screen(client_socket, client_ip,
                    msg=f"Wrong passphrase. {remaining} attempt(s) remaining.",
                    attempts_left=remaining,
                    show_captcha=show_cap,
                    captcha_q=entry.get('captcha_q', ''))
            return

        # PIN correct -- issue session token
        token = secrets.token_hex(32)
        ua = re.search(r'User-Agent: ([^\r\n]+)', request_text)
        ua_str = ua.group(1)[:80] if ua else 'unknown'
        with SESSIONS_LOCK:
            SESSIONS[token] = {
                'ip': client_ip,
                'created_at': time.time(),
                'last_seen': time.time(),
                'user_agent': ua_str
            }
        if client_ip in FAILED_ATTEMPTS:
            del FAILED_ATTEMPTS[client_ip]
        _log_event(client_ip, "Login success", ua_str[:40])
        cookie = _make_session_cookie(token)
        _send_redirect(client_socket, "/upload", f"Set-Cookie: {cookie}\r\n")


def handle_auth_revoke(client_socket, client_ip, request_text, body_raw):
    """POST /auth/revoke -- kill a specific session by token or session_id."""
    if not _is_authenticated(request_text, client_ip):
        _send_redirect(client_socket, "/auth")
        return
    try:
        body_str = body_raw.decode('utf-8', errors='ignore')
        params = dict(urllib.parse.parse_qsl(body_str))
        target_id = params.get('session_id', '')
        target_token = params.get('token', '')
    except:
        target_id = ''
        target_token = ''
    
    if target_id or target_token:
        with SESSIONS_LOCK:
            to_delete = []
            for tok, s in SESSIONS.items():
                sid = hashlib.md5(tok.encode()).hexdigest()
                if sid == target_id or tok == target_token:
                    to_delete.append(tok)
            for tok in to_delete:
                killed_ip = SESSIONS[tok]['ip']
                del SESSIONS[tok]
                _log_event(client_ip, "Session killed", f"killed {killed_ip}")
    
    # Redirect dynamically based on Referer
    referer = ""
    for line in request_text.splitlines():
        if line.lower().startswith('referer:'):
            referer = line.partition(':')[2].strip()
            break
    if referer and "/upload" in referer:
        _send_redirect(client_socket, "/upload")
    else:
        _send_redirect(client_socket, "/auth/dashboard")


def handle_auth_logout(client_socket, client_ip, request_text):
    """POST /auth/logout -- invalidate current session."""
    token = _extract_cookie_token(request_text)
    if token:
        with SESSIONS_LOCK:
            SESSIONS.pop(token, None)
        _log_event(client_ip, "Logout", "")
    _send_redirect(client_socket, "/auth",
        "Set-Cookie: gb_session=; Path=/; Max-Age=0\r\n")


def get_upload_page_html():
    """
    Returns the premium dark-themed HTML/CSS/JS template for the custom upload portal.
    Fully responsive layout containing Phone Drive browser & unified queue.
    """
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#d97706">
    <title>GravityBridge Portal</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,__ICON_B64__">
    <link rel="apple-touch-icon" href="data:image/svg+xml;base64,__ICON_B64__">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090a0f;
            --panel-bg: rgba(17, 19, 28, 0.75);
            --border: rgba(255, 255, 255, 0.06);
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --amber: #d97706;
            --amber-hover: #b45309;
            --success: #10b981;
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: center;
            padding: 76px 16px 76px 16px;
            overflow-x: hidden;
        }

        .gb-top-bar {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 56px;
            background: rgba(13, 14, 20, 0.82);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0 16px;
            z-index: 10000;
        }

        .container {
            width: 100%;
            max-width: 650px;
            background: var(--panel-bg);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.5s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Connections Tab styling */
        .connection-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            gap: 12px;
            transition: background-color 0.15s ease;
        }
        .connection-item:hover {
            background: rgba(255, 255, 255, 0.02);
        }
        .connection-item:last-child {
            border-bottom: none;
        }
        .connections-section h2 {
            font-size: 1.15rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: 4px;
        }

        .sponsor-btn {
            background: rgba(217, 119, 6, 0.1);
            color: #fbbf24;
            border: 1px solid rgba(217, 119, 6, 0.25);
            padding: 5px 10px;
            font-size: 0.75rem;
            font-weight: 600;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 4px;
            font-family: inherit;
        }

        .sponsor-btn:hover {
            background: #d97706;
            color: #fff;
            border-color: #d97706;
            box-shadow: 0 0 8px rgba(217, 119, 6, 0.3);
        }

        .logout-btn {
            background: rgba(239, 68, 68, 0.08);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.15);
            padding: 5px 10px;
            font-size: 0.75rem;
            font-weight: 600;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 4px;
            font-family: inherit;
        }

        .logout-btn:hover {
            background: #ef4444;
            color: #fff;
            border-color: #ef4444;
            box-shadow: 0 0 8px rgba(239, 68, 68, 0.3);
        }

        /* GravityBridge bottom nav bar -- upload page, Phone Drive active */
        .gb-nav-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            z-index: 99999;
            display: flex;
            height: 52px;
            background: #0d0e14;
            border-top: 1px solid rgba(255,255,255,0.08);
            animation: gbNavIn 0.2s ease-out both;
        }
        @keyframes gbNavIn {
            from { transform: translateY(100%); opacity: 0; }
            to   { transform: translateY(0);    opacity: 1; }
        }
        .gb-nav-tab {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 2px;
            cursor: pointer;
            border: none;
            background: transparent;
            color: rgba(255,255,255,0.35);
            font-size: 0.62rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            padding: 5px 0;
            font-family: 'Outfit', system-ui, sans-serif;
        }
        .gb-nav-tab .icon { font-size: 1.15rem; line-height: 1; display: block; }
        .gb-nav-tab.active {
            color: #fff;
            background: rgba(217,119,6,0.2);
        }
        .gb-nav-tab.active .icon {
            filter: drop-shadow(0 0 5px rgba(217,119,6,0.9));
        }

        /* Tabs Interface */
        .tabs-header {
            display: flex;
            background: rgba(0, 0, 0, 0.25);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 4px;
            margin-bottom: 20px;
        }

        .tab-btn {
            flex: 1;
            padding: 12px;
            background: transparent;
            border: none;
            border-radius: 8px;
            color: var(--text-secondary);
            font-family: inherit;
            font-size: 0.9rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .tab-btn.active {
            background: var(--accent);
            color: white;
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.2);
        }

        .tab-panel {
            display: none;
        }

        .tab-panel.active {
            display: block;
        }

        /* Phone Browser Styling */
        .browser-section {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 16px;
        }

        .phone-ip-badge {
            font-size: 0.72rem;
            background: rgba(217, 119, 6, 0.1);
            color: var(--amber);
            padding: 3px 9px;
            border-radius: 20px;
            border: 1px solid rgba(217, 119, 6, 0.15);
            font-family: monospace;
            font-weight: 600;
        }

        .breadcrumb {
            font-size: 0.85rem;
            color: var(--text-secondary);
            word-break: break-all;
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }

        .breadcrumb span {
            color: var(--text-muted);
        }

        .breadcrumb a {
            color: var(--text-primary);
            text-decoration: none;
            font-weight: 500;
            cursor: pointer;
        }

        .breadcrumb a:hover {
            color: var(--amber);
        }

        .browser-list {
            max-height: 220px;
            overflow-y: auto;
            border: 1px solid var(--border);
            background: rgba(0,0,0,0.2);
            border-radius: 10px;
            padding: 6px;
        }

        .browser-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            border-radius: 8px;
            transition: all 0.2s;
            margin-bottom: 4px;
        }

        .browser-item:hover {
            background: rgba(255, 255, 255, 0.04);
        }

        .item-left {
            display: flex;
            align-items: center;
            gap: 10px;
            flex: 1;
            min-width: 0;
        }

        .item-checkbox {
            cursor: pointer;
            accent-color: var(--amber);
            width: 16px;
            height: 16px;
        }

        .item-icon {
            width: 18px;
            height: 18px;
            flex-shrink: 0;
        }

        .item-icon.folder {
            fill: var(--amber);
        }

        .item-icon.file {
            fill: var(--text-muted);
        }

        .item-name {
            font-size: 0.9rem;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .field-group {
            margin-bottom: 14px;
            margin-top: 14px;
        }

        label {
            display: block;
            font-size: 0.75rem;
            font-weight: 700;
            margin-bottom: 6px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        input[type="text"] {
            width: 100%;
            padding: 12px 16px;
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text-primary);
            font-family: inherit;
            font-size: 0.95rem;
            outline: none;
            transition: all 0.2s;
        }

        input[type="text"]:focus {
            border-color: var(--accent);
        }

        .dropzone {
            border: 1.5px dashed rgba(255, 255, 255, 0.15);
            border-radius: 12px;
            padding: 40px 20px;
            text-align: center;
            background: rgba(255, 255, 255, 0.01);
            cursor: pointer;
            transition: all 0.2s;
        }

        .dropzone.dragover {
            background: rgba(99, 102, 241, 0.05);
            border-color: var(--accent);
        }

        .dropzone svg {
            width: 38px;
            height: 38px;
            stroke: var(--text-secondary);
            stroke-width: 1.5;
            fill: none;
            margin-bottom: 10px;
        }

        .dropzone p {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 4px;
        }

        .dropzone span {
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        /* Unified Queue Section */
        .queue-section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            margin-top: 14px;
        }

        .queue-title {
            font-size: 0.8rem;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .file-list {
            margin-bottom: 20px;
            max-height: 200px;
            overflow-y: auto;
        }

        .file-item {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 12px 14px;
            margin-bottom: 8px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .file-info {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.85rem;
        }

        .file-name {
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 50%;
        }

        .badge-source {
            font-size: 0.7rem;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 700;
            color: white;
        }

        .progress-container {
            width: 100%;
            height: 4px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 2px;
            overflow: hidden;
        }

        .progress-bar {
            height: 100%;
            width: 0%;
            background: var(--accent);
            border-radius: 2px;
            transition: width 0.1s linear;
        }

        .file-status {
            font-size: 0.75rem;
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .status-text {
            color: var(--accent);
        }

        .retry-action-btn {
            background: var(--amber);
            border: none;
            border-radius: 6px;
            padding: 4px 10px;
            color: white;
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 600;
            transition: all 0.2s;
        }

        .retry-action-btn:hover {
            background: var(--amber-hover);
            box-shadow: 0 2px 8px rgba(217, 119, 6, 0.3);
        }

        .upload-btn {
            width: 100%;
            padding: 14px;
            background: var(--accent);
            border: none;
            border-radius: 10px;
            color: white;
            font-family: inherit;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .upload-btn:hover:not(:disabled) {
            background: var(--accent-hover);
        }

        .upload-btn:disabled {
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-muted);
            cursor: not-allowed;
        }

        .alert {
            padding: 12px 14px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 0.9rem;
            display: none;
            animation: fadeIn 0.3s;
            word-break: break-all;
            line-height: 1.4;
        }

        .alert-success {
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
            color: #34d399;
        }

        .alert-error {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.2);
            color: #f87171;
        }

        .debug-console {
            margin-top: 8px;
            background: #000000;
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 12px;
            max-height: 120px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.75rem;
            color: #10b981;
        }

        .debug-title {
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin-bottom: 4px;
            margin-top: 14px;
            font-weight: 600;
        }

        /* Responsive Layout Overrides */
        @media (max-width: 520px) {
            .header-row {
                flex-direction: column;
                align-items: flex-start;
                gap: 12px;
            }
            h1 {
                font-size: 1.4rem;
            }
        }

        ::-webkit-scrollbar {
            width: 5px;
        }
        ::-webkit-scrollbar-track {
            background: transparent;
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.08);
            border-radius: 3px;
        }
        /* Nested iframe visual adjustments */
        body.in-iframe {
            padding-top: 0 !important;
            padding-bottom: 0 !important;
            min-height: 100% !important;
            height: 100% !important;
            background: transparent !important;
        }
        body.in-iframe .gb-top-bar {
            display: none !important;
        }
        body.in-iframe #gb-nav {
            display: none !important;
        }
        body.in-iframe .container {
            padding-top: 20px !important;
            padding-bottom: 20px !important;
            margin: 0 auto !important;
            box-shadow: none !important;
            border: none !important;
            max-width: 100% !important;
            width: 100% !important;
            border-radius: 0 !important;
            background: transparent !important;
            backdrop-filter: none !important;
            animation: none !important;
        }

        /* Toast notification for new device connections */
        #gb-toast-container {
            position: fixed !important;
            bottom: 60px !important;
            left: 50% !important;
            transform: translateX(-50%) !important;
            z-index: 999999 !important;
            display: flex !important;
            flex-direction: column !important;
            gap: 8px !important;
            pointer-events: none !important;
            align-items: center !important;
        }
        .gb-toast {
            background: rgba(13, 14, 20, 0.95) !important;
            backdrop-filter: blur(16px) !important;
            -webkit-backdrop-filter: blur(16px) !important;
            border: 1px solid rgba(251, 191, 36, 0.3) !important;
            border-radius: 12px !important;
            padding: 12px 20px !important;
            color: #fbbf24 !important;
            font-family: Outfit, system-ui, sans-serif !important;
            font-size: 0.82rem !important;
            font-weight: 600 !important;
            display: flex !important;
            align-items: center !important;
            gap: 10px !important;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5) !important;
            animation: gbToastIn 0.35s ease-out !important;
            pointer-events: auto !important;
            white-space: nowrap !important;
        }
        .gb-toast.out {
            animation: gbToastOut 0.3s ease-in forwards !important;
        }
        @keyframes gbToastIn {
            from { transform: translateY(20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        @keyframes gbToastOut {
            from { transform: translateY(0); opacity: 1; }
            to { transform: translateY(20px); opacity: 0; }
        }
        .gb-toast-icon {
            font-size: 1.2rem !important;
        }
        .gb-toast-ip {
            color: #fff !important;
            font-weight: 700 !important;
        }
    </style>
</head>
<body>
    <!-- Hidden iframe preloads chat in background after auth -->
    <iframe id="gb-chat-preload" src="/" style="display:none; position:fixed; top:55px; left:0; width:100%; height:calc(100% - 107px); z-index:999998; border:none; background:#0d0e14;"></iframe>
    <!-- Top Sticky Header Bar -->
    <header class="gb-top-bar gb-upload-view">
        <div style="display: flex; align-items: center; gap: 10px;">
            <img src="data:image/svg+xml;base64,__ICON_B64__" style="width: 28px; height: 28px; border-radius: 50%; display: block;" alt="GravityBridge Logo" />
            <div style="display: flex; flex-direction: column; line-height: 1.15;">
                <span style="font-weight: 800; font-size: 0.9rem; color: #fff; letter-spacing: -0.2px;">GravityBridge</span>
                <span style="font-size: 0.65rem; color: var(--amber); font-weight: 700; letter-spacing: 0.5px;">WIRELESS DRIVE</span>
            </div>
        </div>
        <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0; padding-right: 8px;">
            <button class="sponsor-btn" id="gb-top-sponsor" title="Support the project" style="display:inline-flex;align-items:center;gap:3px;padding:5px 10px;font-size:0.72rem;"><span style="display:inline-block;animation:gbHeartBeat 1.8s ease-in-out infinite;">❤️</span> Support</button>
            <form method="POST" action="/auth/logout" style="margin: 0;">
                <button type="submit" class="logout-btn" title="Logout session" style="padding:5px 10px;font-size:0.72rem;">&#128275; Logout</button>
            </form>
        </div>
    </header>

    <div class="container gb-upload-view" style="padding-bottom:52px;">

        
        <div id="success-alert" class="alert alert-success"></div>
        <div id="error-alert" class="alert alert-error"></div>

        <!-- Tab Bar -->
        <div class="tabs-header">
            <button class="tab-btn active" id="tab-btn-phone">📱 Phone Drive</button>
            <button class="tab-btn" id="tab-btn-local">💻 Local Uploads</button>
            <button class="tab-btn" id="tab-btn-connections">🔗 Connections</button>
        </div>

        <!-- TAB PANEL 1: Phone Explorer -->
        <div class="tab-panel active" id="panel-phone">
            <div class="browser-section">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 10px;">
                    <div class="breadcrumb" id="breadcrumb" style="margin-bottom: 0;">
                        <span>Storage</span>
                    </div>
                    <span id="phone-ip-badge" class="phone-ip-badge" style="display: none;"></span>
                    <select id="sort-select" style="background: rgba(0, 0, 0, 0.2); color: var(--text-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 5px 10px; font-size: 0.72rem; cursor: pointer; outline: none; font-weight: 600;">
                        <option value="name-asc">Name (A-Z)</option>
                        <option value="name-desc">Name (Z-A)</option>
                        <option value="date-desc">Newest First</option>
                        <option value="date-asc">Oldest First</option>
                    </select>
                </div>

                <div class="browser-list" id="browser-list">
                    <div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">
                        Reading phone directories...
                    </div>
                </div>
            </div>
        </div>

        <!-- TAB PANEL 2: Local Dropzone -->
        <div class="tab-panel" id="panel-local">
            <div class="dropzone" id="dropzone">
                <svg viewBox="0 0 24 24">
                    <path d="M12 16V8M12 8L9 11M12 8L15 11M20 12C20 16.4183 16.4183 20 12 20C7.58172 20 4 16.4183 4 12C4 7.58172 7.58172 4 12 4C16.4183 4 20 7.58172 20 12Z" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <p>Drag & drop files or folders here</p>
                <span>or click to browse from device storage</span>
                <input type="file" id="file-input" multiple style="display: none;">
            </div>
        </div>

        <!-- TAB PANEL 3: Active Connections -->
        <div class="tab-panel" id="panel-connections">
            <div class="connections-section">
                <h2>Connected Devices</h2>
                <p class="subtitle" style="margin-bottom: 15px;">Other authorized devices currently accessing GravityBridge</p>
                <div id="connections-list" style="background: rgba(0, 0, 0, 0.2); border: 1px solid var(--border); border-radius: 12px; overflow: hidden;">
                    <!-- Loaded dynamically via JS -->
                </div>
            </div>
        </div>

        <div id="transfer-controls">
            <!-- Destination Folder Panel -->
            <div class="field-group">
                <label for="folder-input">Destination Laptop Folder</label>
                <input type="text" id="folder-input" placeholder="PhoneUploads">
            </div>

            <!-- Unified Staging Queue -->
            <div class="queue-section-header">
                <span class="queue-title" id="queue-title">Transfer Queue (0 items)</span>
            </div>
            <div class="file-list" id="file-list"></div>

            <button class="upload-btn" id="upload-btn" disabled>Start Transfer</button>

            <div class="debug-title">System Activity Log</div>
            <div class="debug-console" id="debug-console">
                <div>[System] GravityBridge web service active. Ready for drop.</div>
            </div>
        </div>
    </div>

    <script>
        if (window.parent && window.parent !== window) {
            document.body.classList.add('in-iframe');
        }
        const tabPhone = document.getElementById('tab-btn-phone');
        const tabLocal = document.getElementById('tab-btn-local');
        const tabConnections = document.getElementById('tab-btn-connections');
        const panelPhone = document.getElementById('panel-phone');
        const panelLocal = document.getElementById('panel-local');
        const panelConnections = document.getElementById('panel-connections');
        const transferControls = document.getElementById('transfer-controls');

        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file-input');
        const fileList = document.getElementById('file-list');
        const folderInput = document.getElementById('folder-input');
        const uploadBtn = document.getElementById('upload-btn');
        const successAlert = document.getElementById('success-alert');
        const errorAlert = document.getElementById('error-alert');
        const debugConsole = document.getElementById('debug-console');
        const phoneIpBadge = document.getElementById('phone-ip-badge');
        const breadcrumbContainer = document.getElementById('breadcrumb');
        const browserList = document.getElementById('browser-list');
        const queueTitle = document.getElementById('queue-title');
        const sortSelect = document.getElementById('sort-select');

        let stagedItems = [];
        let activeReads = 0;
        let droppedFilesFallback = [];
        let lastDroppedDirectory = null;

        let currentBrowserPath = "/sdcard";
        let currentBrowserItems = [];

        function logDebug(msg) {
            const row = document.createElement('div');
            row.innerText = `[${new Date().toLocaleTimeString()}] ${msg}`;
            debugConsole.appendChild(row);
            debugConsole.scrollTop = debugConsole.scrollHeight;
            console.log(msg);
        }

        window.addEventListener('error', function(e) {
            logDebug(`[EXCEPTION] ${e.message} at ${e.filename}:${e.lineno}`);
            errorAlert.innerText = `JS Exception: ${e.message}`;
            errorAlert.style.display = 'block';
        });

        // Tab Switching Logic
        tabPhone.addEventListener('click', () => {
            tabPhone.classList.add('active');
            tabLocal.classList.remove('active');
            tabConnections.classList.remove('active');
            panelPhone.classList.add('active');
            panelLocal.classList.remove('active');
            panelConnections.classList.remove('active');
            transferControls.style.display = 'block';
        });

        tabLocal.addEventListener('click', () => {
            tabLocal.classList.add('active');
            tabPhone.classList.remove('active');
            tabConnections.classList.remove('active');
            panelLocal.classList.add('active');
            panelPhone.classList.remove('active');
            panelConnections.classList.remove('active');
            transferControls.style.display = 'block';
        });

        tabConnections.addEventListener('click', () => {
            tabConnections.classList.add('active');
            tabPhone.classList.remove('active');
            tabLocal.classList.remove('active');
            panelConnections.classList.add('active');
            panelPhone.classList.remove('active');
            panelLocal.classList.remove('active');
            transferControls.style.display = 'none';
            loadConnections();
        });

        async function loadConnections() {
            const listEl = document.getElementById('connections-list');
            if (!listEl) return;
            listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:24px;font-size:0.85rem;">Loading active sessions...</div>';
            try {
                const res = await fetch('/auth/sessions');
                const data = await res.json();
                if (data.error) {
                    listEl.innerHTML = `<div style="text-align:center;color:#ef4444;padding:24px;font-size:0.85rem;">Error: ${data.error}</div>`;
                    return;
                }
                if (!data.sessions || data.sessions.length === 0) {
                    listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:24px;font-size:0.85rem;">No active connections found.</div>';
                    return;
                }
                let html = '';
                data.sessions.forEach(s => {
                    const date = new Date(s.created_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                    const lastSeen = new Date(s.last_seen * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                    const selfBadge = s.is_self ? '<span style="background:rgba(16,185,129,0.15);color:#10b981;padding:2px 6px;border-radius:4px;font-size:0.6rem;font-weight:800;border:1px solid rgba(16,185,129,0.25);">CURRENT DEVICE</span>' : '';
                    
                    let deviceIcon = '💻';
                    let deviceName = 'Desktop Browser';
                    const ua = s.user_agent.toLowerCase();
                    const uaRaw = s.user_agent;

                    // Detect device type + OS
                    if (ua.includes('android')) {
                        deviceIcon = '📱';
                        const modelMatch = uaRaw.match(/;[ \t]*([A-Z][^;)]+)[ \t]+Build/i);
                        deviceName = modelMatch ? modelMatch[1].trim() : 'Android Device';
                    } else if (ua.includes('iphone')) {
                        deviceIcon = '📱';
                        deviceName = 'iPhone';
                    } else if (ua.includes('ipad')) {
                        deviceIcon = '📱';
                        deviceName = 'iPad';
                    } else if (ua.includes('windows')) {
                        deviceIcon = '💻';
                        deviceName = 'Windows PC';
                    } else if (ua.includes('macintosh') || ua.includes('mac os')) {
                        deviceIcon = '💻';
                        deviceName = 'Mac';
                    } else if (ua.includes('linux')) {
                        deviceIcon = '💻';
                        deviceName = 'Linux PC';
                    } else if (ua.includes('curl') || ua.includes('python') || ua.includes('go-http')) {
                        deviceIcon = '🧩';
                        deviceName = 'Script / Bot';
                    }
                    
                    html += `
                    <div class="connection-item">
                        <div style="display:flex;align-items:center;gap:12px;">
                            <span style="font-size:1.5rem;background:rgba(255,255,255,0.03);padding:6px;border-radius:8px;">${deviceIcon}</span>
                            <div style="display:flex;flex-direction:column;gap:2px;">
                                <div style="display:flex;align-items:center;gap:8px;">
                                    <span style="font-weight:700;font-size:0.85rem;color:#fff;">${deviceName}</span>
                                    ${selfBadge}
                                </div>
                                <span style="font-size:0.75rem;color:var(--text-secondary);font-family:monospace;">IP: ${s.ip}</span>
                                <span style="font-size:0.65rem;color:var(--text-muted);" title="${s.user_agent}">${s.user_agent.length > 55 ? s.user_agent.slice(0,55)+'...' : s.user_agent}</span>
                                <span style="font-size:0.65rem;color:var(--text-muted);">Connected: ${date} | Active: ${lastSeen}</span>
                            </div>
                        </div>
                        ${!s.is_self ? `
                        <form method="POST" action="/auth/revoke" style="margin:0">
                            <input type="hidden" name="session_id" value="${s.session_id}" />
                            <button type="submit" class="logout-btn" style="background:rgba(239,68,68,0.08);color:#ef4444;border:1px solid rgba(239,68,68,0.2);padding:4px 8px;font-size:0.7rem;border-radius:6px;cursor:pointer;font-weight:600;">Disconnect</button>
                        </form>
                        ` : ''}
                    </div>
                    `;
                });
                listEl.innerHTML = html;
            } catch (err) {
                listEl.innerHTML = `<div style="text-align:center;color:#ef4444;padding:24px;font-size:0.85rem;">Failed to load sessions: ${err.message}</div>`;
            }
        }

        // Get and update Phone Directory List
        async function loadPhoneDirectory(path) {
            browserList.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">Loading phone storage...</div>';
            
            try {
                const response = await fetch(`/adb-ls?path=${encodeURIComponent(path)}`);
                const result = await response.json();
                
                if (result.success) {
                    currentBrowserPath = result.path;
                    currentBrowserItems = result.items;
                    renderBrowserBreadcrumb(result.path);
                    renderBrowserList(result.items);
                } else {
                    browserList.innerHTML = `<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">ADB connection inactive. Toggle Wireless Debugging.</div>`;
                    logDebug(`ADB Directory read failure: ${result.error}`);
                }
            } catch (err) {
                browserList.innerHTML = `<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">Failed to connect to local proxy helper.</div>`;
                logDebug(`ADB load error: ${err.message}`);
            }
        }

        // Listener for sorting select dropdown
        sortSelect.addEventListener('change', () => {
            renderBrowserList(currentBrowserItems);
        });

        // Render Browser Breadcrumbs
        function renderBrowserBreadcrumb(path) {
            breadcrumbContainer.innerHTML = '';
            
            const baseLink = document.createElement('a');
            baseLink.innerText = 'Internal Storage';
            baseLink.addEventListener('click', () => loadPhoneDirectory('/sdcard'));
            breadcrumbContainer.appendChild(baseLink);
            
            const relative = path.startsWith('/sdcard') ? path.substring(7) : path;
            if (relative && relative !== '/') {
                const parts = relative.split('/').filter(p => p);
                let currentBuildPath = '/sdcard';
                
                parts.forEach(part => {
                    const sep = document.createElement('span');
                    sep.innerText = ' > ';
                    breadcrumbContainer.appendChild(sep);
                    
                    currentBuildPath += '/' + part;
                    const pathLink = document.createElement('a');
                    pathLink.innerText = part;
                    const target = currentBuildPath;
                    pathLink.addEventListener('click', () => loadPhoneDirectory(target));
                    breadcrumbContainer.appendChild(pathLink);
                });
            }
        }

        // Render Directory Listing
        function renderBrowserList(items) {
            browserList.innerHTML = '';
            
            // Add Go Up Directory entry
            if (currentBrowserPath !== '/sdcard' && currentBrowserPath !== '/sdcard/') {
                const parentItem = document.createElement('div');
                parentItem.className = 'browser-item';
                parentItem.innerHTML = `
                    <div class="item-left">
                        <svg class="item-icon folder" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
                        <span class="item-name" style="font-weight: 600;">.. (Go Up)</span>
                    </div>
                `;
                parentItem.querySelector('.item-left').addEventListener('click', () => {
                    const idx = currentBrowserPath.lastIndexOf('/');
                    const parentPath = idx !== -1 ? currentBrowserPath.substring(0, idx) : '/sdcard';
                    loadPhoneDirectory(parentPath || '/sdcard');
                });
                browserList.appendChild(parentItem);
            }

            if (!items || items.length === 0) {
                browserList.innerHTML += '<div style="padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.85rem;">This directory is empty</div>';
                return;
            }

            const sortVal = sortSelect.value;
            const compareItems = (a, b) => {
                if (sortVal === 'name-asc') {
                    return a.name.localeCompare(b.name);
                } else if (sortVal === 'name-desc') {
                    return b.name.localeCompare(a.name);
                } else if (sortVal === 'date-desc') {
                    return (b.mtime || 0) - (a.mtime || 0);
                } else if (sortVal === 'date-asc') {
                    return (a.mtime || 0) - (b.mtime || 0);
                }
                return 0;
            };

            const folders = items.filter(i => i.type === 'd').sort(compareItems);
            const files = items.filter(i => i.type === 'f').sort(compareItems);

            // Render Folders
            folders.forEach(item => {
                const itemEl = document.createElement('div');
                itemEl.className = 'browser-item';
                
                const targetPath = currentBrowserPath.endsWith('/') ? (currentBrowserPath + item.name) : (currentBrowserPath + '/' + item.name);
                const isStaged = stagedItems.some(i => i.type === 'adb-folder' && i.sourcePath === targetPath);
                
                itemEl.innerHTML = `
                    <div class="item-left">
                        <input type="checkbox" class="item-checkbox" ${isStaged ? 'checked' : ''}>
                        <svg class="item-icon folder" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
                        <span class="item-name">${item.name}</span>
                    </div>
                `;
                
                const checkbox = itemEl.querySelector('.item-checkbox');
                checkbox.addEventListener('change', (e) => {
                    if (e.target.checked) {
                        stageItem({
                            id: `adb-folder-${targetPath}`,
                            type: 'adb-folder',
                            name: item.name,
                            sourcePath: targetPath,
                            status: 'Queued',
                            percent: 0
                        });
                    } else {
                        unstageItem(`adb-folder-${targetPath}`);
                    }
                });

                itemEl.querySelector('.item-name').addEventListener('click', () => loadPhoneDirectory(targetPath));
                itemEl.querySelector('.item-icon').addEventListener('click', () => loadPhoneDirectory(targetPath));

                browserList.appendChild(itemEl);
            });

            // Render Files
            files.forEach(item => {
                const itemEl = document.createElement('div');
                itemEl.className = 'browser-item';
                
                const targetPath = currentBrowserPath.endsWith('/') ? (currentBrowserPath + item.name) : (currentBrowserPath + '/' + item.name);
                const isStaged = stagedItems.some(i => i.type === 'adb-file' && i.sourcePath === targetPath);
                
                itemEl.innerHTML = `
                    <div class="item-left">
                        <input type="checkbox" class="item-checkbox" ${isStaged ? 'checked' : ''}>
                        <svg class="item-icon file" viewBox="0 0 20 20"><path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z"/></svg>
                        <span class="item-name">${item.name}</span>
                    </div>
                `;
                
                const checkbox = itemEl.querySelector('.item-checkbox');
                checkbox.addEventListener('change', (e) => {
                    if (e.target.checked) {
                        stageItem({
                            id: `adb-file-${targetPath}`,
                            type: 'adb-file',
                            name: item.name,
                            sourcePath: targetPath,
                            status: 'Queued',
                            percent: 0
                        });
                    } else {
                        unstageItem(`adb-file-${targetPath}`);
                    }
                });

                browserList.appendChild(itemEl);
            });
        }

        function stageItem(item) {
            if (!stagedItems.some(i => i.id === item.id)) {
                stagedItems.push(item);
                logDebug(`Staged item: ${item.name}`);
                renderFileList();
            }
        }

        function unstageItem(id) {
            stagedItems = stagedItems.filter(i => i.id !== id);
            logDebug(`Unstaged item ID: ${id}`);
            renderFileList();
        }

        async function initBrowser() {
            const host = window.location.hostname;
            phoneIpBadge.innerText = `IP: ${host}`;
            loadPhoneDirectory('/sdcard');
        }

        initBrowser();

        async function autoPullFolder(folderName) {
            successAlert.style.display = 'none';
            errorAlert.style.display = 'none';
            logDebug(`[Auto-Trigger] Searching and pulling phone folder '${folderName}' wirelessly...`);
            
            try {
                const response = await fetch('/adb-pull-auto', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folderName: folderName })
                });
                
                const result = await response.json();
                if (result.success) {
                    logDebug(`Auto ADB Pull Succeeded! Resolved phone path: ${result.resolvedPath}`);
                    successAlert.innerText = `Successfully auto-pulled folder from phone! Resolved path: ${result.resolvedPath}. Saved locally to: ${result.path}. Tell the AI in chat to analyze it.`;
                    successAlert.style.display = 'block';
                    loadPhoneDirectory(currentBrowserPath);
                } else {
                    logDebug(`Auto ADB Pull Failed: ${result.error}`);
                    errorAlert.innerText = `Auto ADB Pull Failed: ${result.error}`;
                    errorAlert.style.display = 'block';
                }
            } catch (err) {
                logDebug(`Auto ADB Pull Network Error: ${err.message}`);
                errorAlert.innerText = `Auto ADB Pull Network Error: ${err.message}`;
                errorAlert.style.display = 'block';
            }
        }

        dropzone.addEventListener('click', () => fileInput.click());

        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });

        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('dragover');
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            
            logDebug("Drop event caught");
            
            const items = e.dataTransfer.items;
            const files = e.dataTransfer.files;
            
            droppedFilesFallback = Array.from(files);
            
            let hasDirectory = false;
            let dirName = "";
            if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === 'function') {
                const item = items[0].webkitGetAsEntry();
                if (item && item.isDirectory) {
                    hasDirectory = true;
                    dirName = item.name;
                }
            }
            lastDroppedDirectory = hasDirectory ? dirName : null;
            
            logDebug("Processing local dropped items...");
            
            if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === 'function') {
                activeReads = 0;
                for (let i = 0; i < items.length; i++) {
                    const item = items[i].webkitGetAsEntry();
                    if (item) {
                        traverseFileTree(item);
                    }
                }
            } else {
                handleFiles(files);
            }
        });

        async function traverseFileTree(item, path = "") {
            activeReads++;
            if (item.isFile) {
                try {
                    const file = await getFileFromEntry(item);
                    stageLocalFile(file, path + file.name);
                } catch (err) {
                    logDebug(`File read error for ${item.name}: ${err}`);
                } finally {
                    activeReads--;
                    checkReadsComplete();
                }
            } else if (item.isDirectory) {
                try {
                    const entries = await readAllDirectoryEntries(item);
                    for (let i = 0; i < entries.length; i++) {
                        traverseFileTree(entries[i], path + item.name + "/");
                    }
                } catch (err) {
                    logDebug(`Dir read error for ${item.name}: ${err}`);
                } finally {
                    activeReads--;
                    checkReadsComplete();
                }
            }
        }

        function getFileFromEntry(fileEntry) {
            return new Promise((resolve, reject) => {
                fileEntry.file(resolve, reject);
            });
        }

        function readAllDirectoryEntries(dirEntry) {
            const dirReader = dirEntry.createReader();
            const allEntries = [];
            
            return new Promise((resolve, reject) => {
                const readBatch = () => {
                    dirReader.readEntries((entries) => {
                        if (entries.length > 0) {
                            allEntries.push(...entries);
                            readBatch();
                        } else {
                            resolve(allEntries);
                        }
                    }, reject);
                };
                readBatch();
            });
        }

        function checkReadsComplete() {
            if (activeReads === 0) {
                if (stagedItems.filter(i => i.type === 'local').length === 0 && droppedFilesFallback && droppedFilesFallback.length > 0) {
                    if (lastDroppedDirectory) {
                        logDebug(`Folder '${lastDroppedDirectory}' returned 0 files. Auto-triggering wireless ADB pull...`);
                        autoPullFolder(lastDroppedDirectory);
                        return;
                    }
                    logDebug("Directory scanning returned 0 files. Falling back to flat files.");
                    handleFiles(droppedFilesFallback);
                } else {
                    logDebug("Local directory scanning complete.");
                }
            }
        }

        fileInput.addEventListener('change', (e) => {
            handleFiles(e.target.files);
        });

        function handleFiles(files) {
            for (let file of files) {
                stageLocalFile(file, "");
            }
        }

        function stageLocalFile(file, relativePath) {
            const id = `local-${relativePath || file.name}-${file.size}`;
            stageItem({
                id: id,
                type: 'local',
                name: relativePath || file.name,
                file: file,
                relativePath: relativePath,
                status: 'Queued',
                percent: 0
            });
        }

        function formatBytes(bytes, decimals = 2) {
            if (bytes === 0) return '0 Bytes';
            const k = 1024;
            const dm = decimals < 0 ? 0 : decimals;
            const sizes = ['Bytes', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
        }

        function renderFileList() {
            fileList.innerHTML = '';
            stagedItems.forEach((item, index) => {
                const isLocal = item.type === 'local';
                const badgeText = isLocal ? 'Local File' : (item.type === 'adb-folder' ? 'Phone Folder' : 'Phone File');
                const badgeColor = isLocal ? 'rgba(99, 102, 241, 0.15)' : 'rgba(217, 119, 6, 0.15)';
                const badgeTextColor = isLocal ? 'var(--accent)' : 'var(--amber)';
                
                const fileElement = document.createElement('div');
                fileElement.className = 'file-item';
                fileElement.id = `file-${index}`;
                
                fileElement.innerHTML = `
                    <div class="file-info">
                        <span class="file-name" title="${item.name}">${item.name}</span>
                        <span class="badge-source" style="background: ${badgeColor}; color: ${badgeTextColor};">${badgeText}</span>
                    </div>
                    <div class="progress-container">
                        <div class="progress-bar" id="progress-${index}"></div>
                    </div>
                    <div class="file-status">
                        <span class="status-text" id="status-${index}">${item.status}</span>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span class="status-percent" id="percent-${index}">${item.percent}%</span>
                            <div class="retry-container" id="retry-container-${index}" style="display: none;"></div>
                        </div>
                    </div>
                `;

                if (item.status === 'Failed') {
                    const retryBtn = document.createElement('button');
                    retryBtn.className = 'retry-action-btn';
                    retryBtn.innerText = '🔄 Retry';
                    retryBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        retrySingleItem(index);
                    });
                    const container = fileElement.querySelector(`.retry-container`);
                    container.appendChild(retryBtn);
                    container.style.display = 'block';
                }

                fileList.appendChild(fileElement);
            });
            queueTitle.innerText = `Transfer Queue (${stagedItems.length} items)`;
            uploadBtn.disabled = stagedItems.length === 0;
        }

        async function retrySingleItem(index) {
            const item = stagedItems[index];
            const statusText = document.getElementById(`status-${index}`);
            const percentText = document.getElementById(`percent-${index}`);
            const progressBar = document.getElementById(`progress-${index}`);
            const retryContainer = document.getElementById(`retry-container-${index}`);
            
            retryContainer.style.display = 'none';
            retryContainer.innerHTML = '';
            
            item.status = 'Transferring...';
            statusText.innerText = 'Transferring...';
            statusText.style.color = 'var(--accent)';
            progressBar.style.backgroundColor = item.type === 'local' ? '#6366f1' : '#d97706';
            progressBar.style.width = '20%';
            percentText.innerText = '20%';
            
            const folder = folderInput.value.trim() || "PhoneUploads";
            
            try {
                if (item.type === 'local') {
                    await uploadFile(item.file, item.relativePath, folder, (percent) => {
                        progressBar.style.width = percent + '%';
                        percentText.innerText = percent + '%';
                    });
                } else {
                    const response = await fetch('/adb-pull', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: item.sourcePath, folder: folder })
                    });
                    const result = await response.json();
                    if (result.success) {
                        progressBar.style.width = '100%';
                        percentText.innerText = '100%';
                    } else {
                        throw new Error(result.error);
                    }
                }
                
                item.status = 'Completed';
                statusText.innerText = 'Completed';
                statusText.style.color = '#10b981';
                progressBar.style.backgroundColor = '#10b981';
            } catch (err) {
                item.status = 'Failed';
                statusText.innerText = 'Failed';
                statusText.style.color = '#ef4444';
                progressBar.style.backgroundColor = '#ef4444';
                logDebug(`Retry failed for item: ${item.name} - ${err.message || err}`);
                renderFileList();
            }
        }

        uploadBtn.addEventListener('click', async () => {
            uploadBtn.disabled = true;
            folderInput.disabled = true;
            successAlert.style.display = 'none';
            errorAlert.style.display = 'none';
            
            const folder = folderInput.value.trim() || "PhoneUploads";
            let successCount = 0;

            logDebug(`Starting transfer batch of ${stagedItems.length} items into folder: '${folder}'`);

            for (let i = 0; i < stagedItems.length; i++) {
                const item = stagedItems[i];
                const statusText = document.getElementById('status-' + i);
                const percentText = document.getElementById('percent-' + i);
                const progressBar = document.getElementById('progress-' + i);
                const retryContainer = document.getElementById('retry-container-' + i);
                
                if (retryContainer) {
                    retryContainer.style.display = 'none';
                    retryContainer.innerHTML = '';
                }

                item.status = 'Transferring...';
                statusText.innerText = 'Transferring...';
                progressBar.style.backgroundColor = item.type === 'local' ? '#6366f1' : '#d97706';
                progressBar.style.width = '20%';
                percentText.innerText = '20%';

                try {
                    if (item.type === 'local') {
                        await uploadFile(item.file, item.relativePath, folder, (percent) => {
                            progressBar.style.width = percent + '%';
                            percentText.innerText = percent + '%';
                        });
                    } else {
                        const response = await fetch('/adb-pull', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ path: item.sourcePath, folder: folder })
                        });
                        const result = await response.json();
                        if (result.success) {
                            progressBar.style.width = '100%';
                            percentText.innerText = '100%';
                        } else {
                            throw new Error(result.error);
                        }
                    }
                    
                    item.status = 'Completed';
                    statusText.innerText = 'Completed';
                    statusText.style.color = '#10b981';
                    progressBar.style.backgroundColor = '#10b981';
                    successCount++;
                } catch (err) {
                    item.status = 'Failed';
                    statusText.innerText = 'Failed';
                    statusText.style.color = '#ef4444';
                    progressBar.style.backgroundColor = '#ef4444';
                    logDebug(`Transfer failed for item: ${item.name} - ${err.message || err}`);
                    renderFileList();
                }
            }

            folderInput.disabled = false;
            uploadBtn.disabled = false;

            if (successCount === stagedItems.length) {
                successAlert.innerText = `Successfully transferred all ${successCount} items! Tell the AI in chat to analyze the folder: "DeviceUploads/${folder}"`;
                successAlert.style.display = 'block';
                logDebug(`All ${successCount} items successfully transferred.`);
                stagedItems = [];
                setTimeout(() => { renderFileList(); loadPhoneDirectory(currentBrowserPath); }, 5000);
            } else {
                errorAlert.innerText = `Transferred ${successCount} of ${stagedItems.length} items successfully. Some items failed.`;
                errorAlert.style.display = 'block';
                logDebug(`Batch transfer incomplete: ${successCount} of ${stagedItems.length} succeeded.`);
            }
        });

        function uploadFile(file, relativePath, folder, onProgress) {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                
                let destFolder = folder;
                let destFilename = file.name;
                
                if (relativePath) {
                    const idx = relativePath.lastIndexOf('/');
                    if (idx !== -1) {
                        const relDir = relativePath.substring(0, idx);
                        destFolder = folder ? (folder + '/' + relDir) : relDir;
                        destFilename = relativePath.substring(idx + 1);
                    }
                }
                
                const progressKey = destFolder ? (destFolder + '/' + destFilename) : destFilename;
                
                let pollInterval = setInterval(async () => {
                    try {
                        const res = await fetch(`/upload-progress?key=${encodeURIComponent(progressKey)}`);
                        const data = await res.json();
                        if (data.total > 0) {
                            const percent = Math.round((data.received / data.total) * 100);
                            onProgress(percent);
                        }
                    } catch (e) {}
                }, 300);
                
                const folderParam = destFolder ? encodeURIComponent(destFolder) : '';
                const filenameParam = encodeURIComponent(destFilename);
                const url = `/upload?folder=${folderParam}&filename=${filenameParam}`;
                
                xhr.open('POST', url, true);
                
                xhr.onload = () => {
                    clearInterval(pollInterval);
                    if (xhr.status === 200) {
                        onProgress(100);
                        resolve(JSON.parse(xhr.responseText));
                    } else {
                        reject(new Error('Upload failed with status ' + xhr.status));
                    }
                };
                
                xhr.onerror = () => {
                    clearInterval(pollInterval);
                    reject(new Error('Network error during upload'));
                };
                
                xhr.send(file);
            });
        }
    </script>
    <div id="gb-opencode-view" style="display:none;position:fixed;top:55px;left:0;width:100%;height:calc(100vh - 107px);z-index:10000;background:#0d0e14;flex-direction:column;">
        <div style="background:#161a23;padding:6px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,0.08);font-family:Outfit,sans-serif;font-size:0.8rem;color:#a1a1aa;">
            <span style="display:flex;align-items:center;gap:6px;"><span style="color:#10b981;">●</span> OpenCode Live Web IDE</span>
            <a id="gb-opencode-popout" href="#" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();" style="color:#60a5fa;text-decoration:none;font-weight:600;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.3);border-radius:6px;padding:3px 10px;font-size:0.75rem;">Open in New Window ↗</a>
        </div>
        <iframe id="gb-opencode-iframe" src="" style="width:100%;height:100%;border:none;display:block;flex:1;background:#0d0e14;"></iframe>
    </div>

    <script>
        function gbSwitchToChat() {
            // If we are inside an iframe (SPA overlay mode), signal the parent
            if (window.parent && window.parent !== window) {
                window.parent.postMessage('gb:back-to-chat', '*');
                window.top.postMessage('gb:back-to-chat', '*');
                return;
            }
            // Show preloaded chat iframe in middle section, keep top/bottom bar visible
            var chatFrame = document.getElementById('gb-chat-preload');
            var opencodeView = document.getElementById('gb-opencode-view');
            var chatTab = document.getElementById('gb-nav-chat');
            var uploadTab = document.getElementById('gb-nav-upload');
            var opencodeTab = document.getElementById('gb-nav-opencode');
            if (opencodeView) opencodeView.style.display = 'none';
            if (chatFrame) {
                chatFrame.style.display = 'block';
                chatFrame.style.top = '55px';
                chatFrame.style.height = 'calc(100% - 107px)';
            } else {
                window.location.href = '/';
                return;
            }
            chatTab.classList.add('active');
            uploadTab.classList.remove('active');
            if (opencodeTab) opencodeTab.classList.remove('active');
        }

        function gbSwitchToUpload() {
            var chatFrame = document.getElementById('gb-chat-preload');
            var opencodeView = document.getElementById('gb-opencode-view');
            var chatTab = document.getElementById('gb-nav-chat');
            var uploadTab = document.getElementById('gb-nav-upload');
            var opencodeTab = document.getElementById('gb-nav-opencode');
            if (opencodeView) opencodeView.style.display = 'none';
            if (chatFrame) chatFrame.style.display = 'none';
            uploadTab.classList.add('active');
            chatTab.classList.remove('active');
            if (opencodeTab) opencodeTab.classList.remove('active');
        }

        function gbSwitchToOpencode() {
            if (window.parent && window.parent !== window) {
                window.parent.postMessage('gb:switch-opencode', '*');
                window.top.postMessage('gb:switch-opencode', '*');
            }
            var chatFrame = document.getElementById('gb-chat-preload');
            var opencodeView = document.getElementById('gb-opencode-view');
            var opencodeFrame = document.getElementById('gb-opencode-iframe');
            var popout = document.getElementById('gb-opencode-popout');
            var chatTab = document.getElementById('gb-nav-chat');
            var uploadTab = document.getElementById('gb-nav-upload');
            var opencodeTab = document.getElementById('gb-nav-opencode');
            if (chatFrame) chatFrame.style.display = 'none';
            if (opencodeView) {
                opencodeView.style.display = 'flex';
                var ocUrl = 'http://' + window.location.hostname + ':__OPENCODE_PORT__';
                if (popout) popout.href = ocUrl;
                if (opencodeFrame && (!opencodeFrame.src || opencodeFrame.src.indexOf(':__OPENCODE_PORT__') === -1)) {
                    opencodeFrame.src = ocUrl;
                }
            }
            chatTab.classList.remove('active');
            uploadTab.classList.remove('active');
            if (opencodeTab) opencodeTab.classList.add('active');
        }

        function gbGoBack() { gbSwitchToChat(); }
    </script>
    <!-- Bottom nav bar (Phone Drive tab is active) -->
    <nav class="gb-nav-bar gb-upload-view" id="gb-nav" role="navigation" aria-label="GravityBridge navigation">
        <!-- __CHAT_TAB_HTML__ -->
        <!-- __DRIVE_TAB_HTML__ -->
        <!-- __OPENCODE_TAB_HTML__ -->
    </nav>

    <!-- Toast container for new device notifications -->
    <div id="gb-toast-container"></div>

    <!-- Sponsor Modal -->
    <div id="gb-sponsor-modal" style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;z-index:2147483647;font-family:Outfit,system-ui,sans-serif;box-sizing:border-box;padding:16px;">
        <div style="background:linear-gradient(160deg,#0e1318 0%,#131a22 100%);border:1px solid rgba(255,255,255,0.09);border-radius:24px;padding:28px 24px;width:100%;max-width:380px;box-shadow:0 30px 60px -12px rgba(0,0,0,0.9);text-align:center;box-sizing:border-box;animation:gbSlideUp 0.25s ease-out;">
            <div style="font-size:2.5rem;margin-bottom:10px;animation:gbHeartBeat 1.8s ease-in-out infinite;">❤️</div>
            <div style="font-weight:800;font-size:1.2rem;color:#fff;letter-spacing:-0.3px;margin-bottom:8px;">Support GravityBridge</div>
            <p style="font-size:0.78rem;color:rgba(255,255,255,0.55);line-height:1.5;margin-bottom:24px;">If this tool saves you time, consider buying me a coffee. One tap opens your payment app directly.</p>
            <div style="display:flex;gap:12px;justify-content:center;margin-bottom:20px;">
                <a href="upi://pay?pa=mohit1998arora@yescred&pn=Mohit%20Arora&cu=INR&tn=GravityBridge" style="flex:1;text-decoration:none;background:linear-gradient(135deg,rgba(217,119,6,0.2),rgba(251,191,36,0.1));border:1px solid rgba(217,119,6,0.35);border-radius:14px;padding:16px 12px;display:flex;flex-direction:column;align-items:center;gap:6px;transition:all 0.2s;cursor:pointer;">
                    <span style="font-size:1.6rem;">🇮🇳</span>
                    <span style="font-weight:700;font-size:0.82rem;color:#fbbf24;">Pay via UPI</span>
                    <span style="font-size:0.65rem;color:rgba(255,255,255,0.4);">GPay / PhonePe / Cred</span>
                </a>
                <a href="https://paypal.me/arorasir" target="_blank" rel="noopener" style="flex:1;text-decoration:none;background:linear-gradient(135deg,rgba(0,112,201,0.2),rgba(0,148,255,0.1));border:1px solid rgba(0,148,255,0.3);border-radius:14px;padding:16px 12px;display:flex;flex-direction:column;align-items:center;gap:6px;transition:all 0.2s;cursor:pointer;">
                    <span style="font-size:1.6rem;">💳</span>
                    <span style="font-weight:700;font-size:0.82rem;color:#60a5fa;">PayPal</span>
                    <span style="font-size:0.65rem;color:rgba(255,255,255,0.4);">paypal.me/arorasir</span>
                </a>
            </div>
            <button id="gb-sponsor-close" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);color:rgba(255,255,255,0.5);border-radius:10px;padding:8px 20px;font-size:0.75rem;font-weight:600;cursor:pointer;font-family:inherit;transition:all 0.2s;">Maybe later</button>
        </div>
    </div>

    <script>
        document.getElementById('gb-top-sponsor').addEventListener('click', function() {
            document.getElementById('gb-sponsor-modal').style.display = 'flex';
        });
        document.getElementById('gb-sponsor-close').addEventListener('click', function() {
            document.getElementById('gb-sponsor-modal').style.display = 'none';
        });
        document.getElementById('gb-sponsor-modal').addEventListener('click', function(e) {
            if (e.target === this) this.style.display = 'none';
        });
    </script>

    <script>
        (function() {
            var knownIPs = new Set();
            var toastContainer = document.getElementById('gb-toast-container');
            var POLL_INTERVAL = 3000;
            var firstPoll = true;

            function showToast(ip) {
                var toast = document.createElement('div');
                toast.className = 'gb-toast';
                toast.innerHTML = '<span class="gb-toast-icon">&#128241;</span> <span>New device connected: </span><span class="gb-toast-ip">' + ip + '</span>';
                toastContainer.appendChild(toast);

                // Auto-dismiss after 5 seconds
                setTimeout(function() {
                    toast.classList.add('out');
                    setTimeout(function() {
                        if (toast.parentNode) toast.parentNode.removeChild(toast);
                    }, 300);
                }, 5000);
            }

            function pollSessions() {
                fetch('/auth/sessions', { credentials: 'same-origin' })
                    .then(function(res) { return res.json(); })
                    .then(function(data) {
                        var sessions = data.sessions || [];
                        sessions.forEach(function(s) {
                            if (!s.is_self && !knownIPs.has(s.ip)) {
                                knownIPs.add(s.ip);
                                if (!firstPoll) {
                                    showToast(s.ip);
                                }
                            }
                        });
                        firstPoll = false;
                    })
                    .catch(function() { /* ignore errors */ });
            }

            // Initial poll after 2s delay (let page fully load)
            setTimeout(function() {
                pollSessions();
                setInterval(pollSessions, POLL_INTERVAL);
            }, 2000);

            // Hide nav and topbar inside iframe
            if (window.parent && window.parent !== window) {
                document.body.classList.add('in-iframe');
                var tb = document.querySelector('.gb-top-bar');
                if (tb) tb.style.setProperty('display', 'none', 'important');
                var nb = document.getElementById('gb-nav');
                if (nb) nb.style.setProperty('display', 'none', 'important');
            }
        })();
    </script>
</body>
</html>
"""
    cfg = load_gravitybridge_config()
    tabs_cfg = cfg.get("tabs", {})
    show_chat = tabs_cfg.get("chat", True)
    show_drive = tabs_cfg.get("drive", True)
    show_opencode = tabs_cfg.get("opencode", True)

    chat_tab_html = '<button class="gb-nav-tab" id="gb-nav-chat" aria-label="Chat" onclick="gbGoBack()"><span class="icon">&#128172;</span><span>Chat</span></button>'
    drive_tab_html = '<button class="gb-nav-tab active" id="gb-nav-upload" aria-label="Phone Drive" onclick="gbSwitchToUpload()"><span class="icon">&#128241;</span><span>Phone Drive</span></button>'
    opencode_tab_html = '<button class="gb-nav-tab" id="gb-nav-opencode" aria-label="OpenCode" onclick="gbSwitchToOpencode()"><span class="icon">&#128187;</span><span>OpenCode</span></button>'

    html = (
        html.replace("__ICON_B64__", ICON_B64)
        .replace("__OPENCODE_PORT__", str(OPENCODE_PORT))
        .replace("<!-- __CHAT_TAB_HTML__ -->", chat_tab_html if show_chat else "")
        .replace("<!-- __DRIVE_TAB_HTML__ -->", drive_tab_html if show_drive else "")
        .replace("<!-- __OPENCODE_TAB_HTML__ -->", opencode_tab_html if show_opencode else "")
    )
    return html

def rewrite_request_headers(raw_bytes, target_port, client_host, is_stream):
    try:
        text = raw_bytes.decode('utf-8', errors='surrogateescape')

        # If it is a request for main.js, strip caching validation headers
        # so the backend server is forced to return a full 200 OK with our injected JS shim
        if "GET /main.js" in text:
            text = re.sub(r'(?im)^If-None-Match:[^\r\n]*\r\n', '', text)
            text = re.sub(r'(?im)^If-Modified-Since:[^\r\n]*\r\n', '', text)

        # Rewrite Host header
        text = re.sub(
            r'(?im)^Host:\s*[^\r\n]+',
            f'Host: 127.0.0.1:{target_port}',
            text
        )

        # Rewrite Origin header
        text = re.sub(
            r'(?im)^Origin:\s*https?://[^\r\n]+',
            f'Origin: https://127.0.0.1:{target_port}',
            text
        )

        # Rewrite Referer header host portion ONLY
        text = re.sub(
            r'(?im)^Referer:\s*https?://[^/\r\n]+',
            f'Referer: https://127.0.0.1:{target_port}',
            text
        )

        # Force Accept-Encoding: identity to disable gzip compression from language server
        if re.search(r'(?im)^Accept-Encoding:', text):
            text = re.sub(
                r'(?im)^Accept-Encoding:\s*[^\r\n]+',
                'Accept-Encoding: identity',
                text
            )
        else:
            text = re.sub(
                r'(HTTP/\S+\r\n)',
                '\\1Accept-Encoding: identity\r\n',
                text,
                count=1
            )

        # Force Connection: close only if it is NOT an active stream request
        if not is_stream:
            if re.search(r'(?im)^Connection:', text):
                text = re.sub(
                    r'(?im)^Connection:\s*[^\r\n]+',
                    'Connection: close',
                    text
                )
            else:
                text = re.sub(
                    r'(HTTP/\S+\r\n)',
                    '\\1Connection: close\r\n',
                    text,
                    count=1
                )

        return text.encode('utf-8', errors='surrogateescape')
    except:
        return raw_bytes

def rewrite_response_headers(raw_bytes, client_origin, is_stream):
    try:
        if b'\r\n\r\n' not in raw_bytes:
            return raw_bytes

        header_raw, body_raw = raw_bytes.split(b'\r\n\r\n', 1)
        header_text = header_raw.decode('utf-8', errors='surrogateescape')

        # Rewrite CORS Allow-Origin
        if re.search(r'(?im)^Access-Control-Allow-Origin:', header_text):
            header_text = re.sub(
                r'(?im)^Access-Control-Allow-Origin:\s*[^\r\n]+',
                f'Access-Control-Allow-Origin: {client_origin}',
                header_text
            )
        else:
            header_text = re.sub(
                r'(HTTP/\S+ \d+ [^\r\n]*\r\n)',
                f'\\1Access-Control-Allow-Origin: {client_origin}\r\n',
                header_text,
                count=1
            )

        # Rewrite CORS Allow-Credentials
        if re.search(r'(?im)^Access-Control-Allow-Credentials:', header_text):
            header_text = re.sub(
                r'(?im)^Access-Control-Allow-Credentials:\s*[^\r\n]+',
                'Access-Control-Allow-Credentials: true',
                header_text
            )
        else:
            header_text = re.sub(
                r'(HTTP/\S+ \d+ [^\r\n]*\r\n)',
                '\\1Access-Control-Allow-Credentials: true\r\n',
                header_text,
                count=1
            )

        # Inject X-Accel-Buffering: no
        if not re.search(r'(?im)^X-Accel-Buffering:', header_text):
            header_text = re.sub(
                r'(HTTP/\S+ \d+ [^\r\n]*\r\n)',
                '\\1X-Accel-Buffering: no\r\n',
                header_text,
                count=1
            )

        # Force Connection: close in response only if it is NOT an active stream request
        if not is_stream:
            if re.search(r'(?im)^Connection:', header_text):
                header_text = re.sub(
                    r'(?im)^Connection:\s*[^\r\n]+',
                    'Connection: close',
                    header_text
                )
            else:
                header_text = re.sub(
                    r'(HTTP/\S+ \d+ [^\r\n]*\r\n)',
                    '\\1Connection: close\r\n',
                    header_text,
                    count=1
                )

        return header_text.encode('utf-8', errors='surrogateescape') + b'\r\n\r\n' + body_raw
    except:
        return raw_bytes

def raw_forward(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except:
        pass
    finally:
        try: src.shutdown(socket.SHUT_WR)
        except: pass
        try: src.close()
        except: pass
        try: dst.shutdown(socket.SHUT_WR)
        except: pass
        try: dst.close()
        except: pass

def handle_client(client_socket, target_port):
    global LAST_KNOWN_PHONE_IP
    target_ssl = None
    try:
        # Read FIRST request chunk
        request_data = client_socket.recv(16384)
        if not request_data:
            client_socket.close()
            return

        request_text = request_data.decode('utf-8', errors='ignore')

        # Dynamically record phone IP address from incoming network packets (same & different networks)
        client_ip = client_socket.getpeername()[0]
        if client_ip not in ("127.0.0.1", "::1"):
            LAST_KNOWN_PHONE_IP = client_ip

        # ============================================================
        # AUTH GATE -- Intercept all routes before processing
        # Public: GET /auth (lock screen) | POST /auth (login)
        # Everything else: must carry a valid session cookie
        # ============================================================

        # Serve lock screen
        if request_text.startswith("GET /auth/dashboard"):
            if not _is_authenticated(request_text, client_ip):
                _send_redirect(client_socket, "/auth")
                return
            serve_dashboard(client_socket, client_ip)
            return

        if request_text.startswith("GET /auth/sessions"):
            if not _is_authenticated(request_text, client_ip):
                _send_json(client_socket, {"error": "unauthorized"}, status="401 Unauthorized")
                return
            with SESSIONS_LOCK:
                sessions_list = []
                current_token = _extract_cookie_token(request_text)
                for tok, s in SESSIONS.items():
                    sessions_list.append({
                        'ip': s['ip'],
                        'created_at': s['created_at'],
                        'last_seen': s['last_seen'],
                        'user_agent': s['user_agent'],
                        'session_id': hashlib.md5(tok.encode()).hexdigest(),
                        'is_self': (tok == current_token)
                    })
            _send_json(client_socket, {"sessions": sessions_list})
            return

        if request_text.startswith("GET /auth"):
            # Already logged in? Redirect to portal
            if _is_authenticated(request_text, client_ip):
                _send_redirect(client_socket, "/upload")
                return
            serve_lock_screen(client_socket, client_ip)
            return

        if request_text.startswith("POST /auth/revoke"):
            body_raw = _read_post_body(request_data, client_socket)
            handle_auth_revoke(client_socket, client_ip, request_text, body_raw)
            return

        if request_text.startswith("POST /auth/logout"):
            handle_auth_logout(client_socket, client_ip, request_text)
            return

        if request_text.startswith("POST /auth"):
            body_raw = _read_post_body(request_data, client_socket)
            handle_auth_post(client_socket, client_ip, request_text, body_raw)
            return

        # All other routes require auth
        if not _is_authenticated(request_text, client_ip):
            _log_event(client_ip, "Blocked (no auth)", request_text.split('\n')[0][:60])
            _send_redirect(client_socket, "/auth")
            return

        # ----------------------------------------------------
        # Route 1: GET /upload (Serve custom upload portal HTML)
        # ----------------------------------------------------
        if request_text.startswith("GET /upload"):
            html = get_upload_page_html()
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
                "Pragma: no-cache\r\n"
                f"Content-Length: {len(html.encode('utf-8'))}\r\n"
                "Connection: close\r\n\r\n"
                + html
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 2: OPTIONS CORS handlers
        # ----------------------------------------------------
        if request_text.startswith("OPTIONS /upload") or request_text.startswith("OPTIONS /adb-pull") or request_text.startswith("OPTIONS /adb-pull-auto") or request_text.startswith("OPTIONS /adb-ls") or request_text.startswith("OPTIONS /upload-progress"):
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type, Accept-Encoding\r\n"
                "Connection: close\r\n\r\n"
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 3: GET /upload-progress (True server-side received bytes status)
        # ----------------------------------------------------
        if request_text.startswith("GET /upload-progress"):
            first_line = request_text.split('\r\n')[0]
            path_part = first_line.split()[1]
            parsed_url = urllib.parse.urlparse(path_part)
            params = urllib.parse.parse_qs(parsed_url.query)
            
            key = params.get('key', [''])[0].strip()
            
            with PROGRESS_LOCK:
                progress = UPLOAD_PROGRESS.get(key, {"received": 0, "total": 0})
                
            resp_body = json.dumps(progress)
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
                + resp_body
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 4: GET /adb-ls (Phone Real-time Directory Listing)
        # ----------------------------------------------------
        if request_text.startswith("GET /adb-ls"):
            first_line = request_text.split('\r\n')[0]
            path_part = first_line.split()[1]
            parsed_url = urllib.parse.urlparse(path_part)
            params = urllib.parse.parse_qs(parsed_url.query)
            
            raw_path = params.get('path', ['/sdcard'])[0].strip()
            cleaned_path = raw_path.replace("'", "").replace('"', '').replace(';', '')
            if not cleaned_path.startswith('/'):
                cleaned_path = '/sdcard/' + cleaned_path
            
            print(f"[DEBUG] GET /adb-ls -> Path: {cleaned_path}, client IP: {client_ip}", flush=True)
            
            device_id = get_target_adb_device()
            
            items = []
            if not device_id:
                print(f"[DEBUG] GET /adb-ls -> No ADB device found.", flush=True)
                resp_body = json.dumps({"success": False, "error": "No ADB device connected. Please connect your phone."})
            else:
                print(f"[*] Browsing directory {cleaned_path} on device {device_id}...", flush=True)
                
                # Query type, modification time (seconds since epoch), and name using stat
                res = subprocess.run([
                    ADB_EXECUTABLE_PATH,
                    "-s", device_id,
                    "shell",
                    f"cd '{cleaned_path}' && stat -c '%F|%Y|%n' * 2>/dev/null"
                ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                
                print(f"[DEBUG] GET /adb-ls -> stat returncode: {res.returncode}", flush=True)
                
                # If stat succeeded and returned output, parse it
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.splitlines():
                        line = line.strip()
                        if '|' in line:
                            parts = line.split('|', 2)
                            if len(parts) == 3:
                                f_type, mtime, name = parts
                                t = 'd' if 'directory' in f_type else 'f'
                                try:
                                    mtime_val = int(mtime)
                                except:
                                    mtime_val = 0
                                items.append({"type": t, "mtime": mtime_val, "name": name})
                else:
                    # Fallback if stat is not available or directory is empty / failed
                    print(f"[DEBUG] GET /adb-ls -> stat command was empty or failed, attempting loop fallback", flush=True)
                    res = subprocess.run([
                        ADB_EXECUTABLE_PATH,
                        "-s", device_id,
                        "shell",
                        f"cd '{cleaned_path}' && for f in *; do [ -e \"$f\" ] || continue; [ -d \"$f\" ] && echo \"d|$f\" || echo \"f|$f\"; done"
                    ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                    
                    if res.returncode == 0:
                        for line in res.stdout.splitlines():
                            line = line.strip()
                            if '|' in line:
                                t, name = line.split('|', 1)
                                items.append({"type": t, "mtime": 0, "name": name})
                
                print(f"[DEBUG] GET /adb-ls -> Found {len(items)} items", flush=True)
                resp_body = json.dumps({"success": True, "path": cleaned_path, "items": items})
                
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
                + resp_body
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 5: POST /adb-pull-auto (ADB auto-search & pull)
        # ----------------------------------------------------
        if request_text.startswith("POST /adb-pull-auto"):
            parts = request_data.split(b'\r\n\r\n', 1)
            body = parts[1].decode('utf-8', errors='ignore') if len(parts) > 1 else ""
            
            try:
                data = json.loads(body)
                folder_name = data.get("folderName", "").strip()
            except:
                folder_name = ""
                
            if not folder_name:
                resp_body = json.dumps({"success": False, "error": "Empty folder name"})
            else:
                device_id = get_target_adb_device()
                if not device_id:
                    resp_body = json.dumps({"success": False, "error": "No ADB device connected. Please connect your phone."})
                else:
                    print(f"[*] Auto-locating dropped folder '{folder_name}' on device {device_id}...", flush=True)
                    
                    typical_paths = [
                        f"/sdcard/Documents/{folder_name}",
                        f"/sdcard/Download/{folder_name}",
                        f"/sdcard/{folder_name}"
                    ]
                    
                    found_path = None
                    for path in typical_paths:
                        check = subprocess.run([
                            ADB_EXECUTABLE_PATH,
                            "-s", device_id,
                            "shell",
                            f"[ -d '{path}' ] && echo 'exists'"
                        ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                        if "exists" in check.stdout:
                            found_path = path
                            break
                    
                    if not found_path:
                        find_res = subprocess.run([
                            ADB_EXECUTABLE_PATH,
                            "-s", device_id,
                            "shell",
                            f"find /sdcard -maxdepth 4 -type d -name '{folder_name}' 2>/dev/null"
                        ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                        
                        paths = [p.strip() for p in find_res.stdout.splitlines() if p.strip()]
                        if paths:
                            found_path = paths[0]
                    
                    if not found_path:
                        resp_body = json.dumps({"success": False, "error": f"Could not find folder '{folder_name}' on phone."})
                    else:
                        local_path = os.path.join(os.getcwd(), "DeviceUploads", folder_name)
                        print(f"[*] Auto-pulling: {found_path} -> {local_path} on device {device_id}...", flush=True)
                        res = subprocess.run([
                            ADB_EXECUTABLE_PATH,
                            "-s", device_id,
                            "pull",
                            found_path,
                            local_path
                        ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                        
                        if res.returncode == 0:
                            resp_body = json.dumps({"success": True, "path": f"DeviceUploads/{folder_name}", "resolvedPath": found_path})
                        else:
                            resp_body = json.dumps({"success": False, "error": res.stderr or res.stdout or "ADB Pull Error"})
                        
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
                + resp_body
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 6: POST /adb-pull (ADB manual path pull with dest folder)
        # ----------------------------------------------------
        if request_text.startswith("POST /adb-pull"):
            parts = request_data.split(b'\r\n\r\n', 1)
            body = parts[1].decode('utf-8', errors='ignore') if len(parts) > 1 else ""
            
            try:
                data = json.loads(body)
                phone_path = data.get("path", "").strip()
                folder = data.get("folder", "").strip()
            except:
                phone_path = ""
                folder = ""
                
            if not phone_path:
                resp_body = json.dumps({"success": False, "error": "Empty folder path entered"})
            else:
                android_path = phone_path
                if not android_path.startswith("/"):
                    if android_path.startswith("sdcard/"):
                        android_path = "/" + android_path
                    else:
                        android_path = "/sdcard/" + android_path
                
                folder_name = os.path.basename(android_path.rstrip("/"))
                if not folder_name:
                    folder_name = "pulled_folder"
                
                # Format custom destination local folder structure securely under DeviceUploads/
                clean_parts = []
                for part in folder.replace('\\', '/').split('/'):
                    part = part.strip()
                    if part and part != '.' and part != '..':
                        clean_parts.append(os.path.basename(part))
                
                if clean_parts:
                    local_path = os.path.join(os.getcwd(), "DeviceUploads", *clean_parts, folder_name)
                else:
                    local_path = os.path.join(os.getcwd(), "DeviceUploads", folder_name)
                
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                
                device_id = get_target_adb_device()
                if not device_id:
                    resp_body = json.dumps({"success": False, "error": "No ADB device connected. Please connect your phone."})
                else:
                    print(f"[*] Wirelessly pulling {android_path} -> {local_path} on device {device_id}...", flush=True)
                    res = subprocess.run([
                        ADB_EXECUTABLE_PATH,
                        "-s", device_id,
                        "pull",
                        android_path,
                        local_path
                    ], capture_output=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
                    
                    if res.returncode == 0:
                        rel_local_path = os.path.relpath(local_path, os.path.join(os.getcwd(), "DeviceUploads")).replace('\\', '/')
                        resp_body = json.dumps({"success": True, "path": f"DeviceUploads/{rel_local_path}"})
                    else:
                        resp_body = json.dumps({"success": False, "error": res.stderr or res.stdout or "ADB Pull Error"})
                    
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
                + resp_body
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # ----------------------------------------------------
        # Route 7: POST /upload (Disk-streaming file writer)
        # ----------------------------------------------------
        if request_text.startswith("POST /upload"):
            content_length = 0
            for line in request_text.split('\r\n'):
                if line.lower().startswith('content-length:'):
                    content_length = int(line.split(':')[1].strip())
                    break
            
            first_line = request_text.split('\r\n')[0]
            path_part = first_line.split()[1]
            parsed_url = urllib.parse.urlparse(path_part)
            params = urllib.parse.parse_qs(parsed_url.query)
            folder = params.get('folder', [''])[0]
            filename = params.get('filename', [''])[0]
            
            parts = request_data.split(b'\r\n\r\n', 1)
            initial_body = parts[1] if len(parts) > 1 else b""
            bytes_written = len(initial_body)
            
            clean_parts = []
            for part in folder.replace('\\', '/').split('/'):
                part = part.strip()
                if part and part != '.' and part != '..':
                    clean_parts.append(os.path.basename(part))
            
            if not clean_parts:
                folder_path = "upload_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            else:
                folder_path = os.path.join(*clean_parts)
                
            filename = os.path.basename(filename)
            
            storage_dir = os.path.join(os.getcwd(), "DeviceUploads", folder_path)
            os.makedirs(storage_dir, exist_ok=True)
            filepath = os.path.join(storage_dir, filename)
            
            # Setup track key
            progress_key = f"{folder}/{filename}" if folder else filename
            with PROGRESS_LOCK:
                UPLOAD_PROGRESS[progress_key] = {
                    "received": bytes_written,
                    "total": content_length,
                    "done": False
                }
            
            print(f"[*] Disk streaming upload: {filepath} ({content_length} bytes)", flush=True)
            
            with open(filepath, 'wb') as f:
                f.write(initial_body)
                remaining = content_length - bytes_written
                while remaining > 0:
                    chunk = client_socket.recv(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    bytes_written += len(chunk)
                    
                    with PROGRESS_LOCK:
                        UPLOAD_PROGRESS[progress_key] = {
                            "received": bytes_written,
                            "total": content_length,
                            "done": False
                        }

            # Finalize progress status
            with PROGRESS_LOCK:
                UPLOAD_PROGRESS[progress_key] = {
                    "received": content_length,
                    "total": content_length,
                    "done": True
                }

            # Auto-extract zip file if filename ends with .zip
            if filename.lower().endswith('.zip'):
                try:
                    extract_dir = os.path.join(storage_dir, filename[:-4])
                    os.makedirs(extract_dir, exist_ok=True)
                    print(f"[*] Auto-extracting ZIP: {filepath} -> {extract_dir}", flush=True)
                    with zipfile.ZipFile(filepath, 'r') as zip_ref:
                        zip_ref.extractall(extract_dir)
                    # Clean up the uploaded zip file after extraction
                    os.remove(filepath)
                    print(f"[+] ZIP extraction complete. Cleaned up zip file.", flush=True)
                except Exception as ex:
                    print(f"[!] ZIP extraction failed: {ex}", flush=True)
                    
            resp_body = json.dumps({"success": True, "path": filepath})
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
                + resp_body
            ).encode('utf-8')
            client_socket.sendall(resp)
            try: client_socket.shutdown(socket.SHUT_WR)
            except: pass
            client_socket.close()
            return

        # Redirect root GET /
        if (request_text.startswith("GET / HTTP/1.1") or request_text.startswith("GET /c HTTP/1.1") or request_text.startswith("GET /?")) and not request_text.startswith("GET /c/"):
            conv_path = get_active_conversation_path()
            if conv_path:
                print(f"[*] Redirecting root request to active conversation path: {conv_path}", flush=True)
                redirect_resp = (
                    "HTTP/1.1 307 Temporary Redirect\r\n"
                    f"Location: {conv_path}\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: close\r\n\r\n"
                ).encode('utf-8')
                client_socket.sendall(redirect_resp)
                try: client_socket.shutdown(socket.SHUT_WR)
                except: pass
                client_socket.close()
                return
            if request_text.startswith("GET / HTTP/1.1") or request_text.startswith("GET /?"):
                # No active conversation found -- generate a random UUID for instant chat
                fake_id = str(uuid.uuid4())
                fake_path = f"/c/{fake_id}"
                print(f"[*] No active conversation, redirecting to random chat path: {fake_path}", flush=True)
                redirect_resp = (
                    "HTTP/1.1 307 Temporary Redirect\r\n"
                    f"Location: {fake_path}\r\n"
                    "Content-Length: 0\r\n"
                    "Connection: close\r\n\r\n"
                ).encode('utf-8')
                client_socket.sendall(redirect_resp)
                try: client_socket.shutdown(socket.SHUT_WR)
                except: pass
                client_socket.close()
                return

        # Detect active streaming requests (SSE, WebSockets, gRPC streams)
        is_websocket = "upgrade: websocket" in request_text.lower()
        is_sse = "text/event-stream" in request_text.lower()
        is_stream = ("Stream" in request_text) or ("Subscribe" in request_text) or is_websocket or is_sse
        is_main_js = "GET /main.js" in request_text

        # Extract CORS origins
        origin_match = re.search(r'(?im)^Origin:\s*([^\r\n]+)', request_text)
        client_origin = origin_match.group(1).strip() if origin_match else 'http://localhost'

        host_match = re.search(r'(?im)^Host:\s*([^\r\n]+)', request_text)
        client_host = host_match.group(1).strip() if host_match else f'localhost:{PROXY_PORT}'

        first_line = request_text.split('\r\n')[0]
        stream_suffix = " [STREAM]" if is_stream else ""
        print(f"[*] {first_line}{stream_suffix} (Host: {client_host})", flush=True)

        rewritten_request = rewrite_request_headers(request_data, target_port, client_host, is_stream)

        backend_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend_socket.connect((LOCAL_HOST, target_port))
        ctx = ssl._create_unverified_context()
        target_ssl = ctx.wrap_socket(backend_socket, server_hostname=LOCAL_HOST)

        target_ssl.sendall(rewritten_request)

        def forward_response(src, dst, origin, is_str, is_js):
            try:
                first = True
                overlap = b""
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    
                    if is_js:
                        combined = overlap + data
                        target_bytes = b'onChange:a,accept:b,multiple:c},d)=>F.createElement("input",{type:"file",ref:d,onChange:a,accept:b'
                        replacement_bytes = b'onChange:a,accept:b,multiple:c},d)=>F.createElement("input",{type:"file",ref:d,onChange:a,accept:"*/*"'
                        if target_bytes in combined:
                            combined = combined.replace(target_bytes, replacement_bytes)
                            data = combined[len(overlap):]
                        overlap = data[-200:] if len(data) >= 200 else data

                    if first:
                        first = False
                        if data.startswith(b'HTTP/') and b'\r\n\r\n' in data:
                            data = rewrite_response_headers(data, origin, is_str)
                            if is_js:
                                parts = data.split(b'\r\n\r\n', 1)
                                if len(parts) > 1:
                                    headers = parts[0]
                                    body = parts[1]
                                    
                                    headers_text = headers.decode('utf-8', errors='surrogateescape')
                                    is_chunked = "transfer-encoding: chunked" in headers_text.lower()
                                    
                                    combined_js = get_native_storage_js() + "\n" + get_floating_button_js()
                                    shim_js = combined_js.encode('utf-8', errors='surrogateescape')
                                    
                                    if is_chunked:
                                        chunk_header = f"{hex(len(shim_js))[2:]}\r\n".encode('utf-8')
                                        body = chunk_header + shim_js + b"\r\n" + body
                                    else:
                                        body = shim_js + body
                                    
                                    headers_text = re.sub(r'(?im)^Content-Length:\s*[^\r\n]+\r\n', '', headers_text)
                                    headers = headers_text.encode('utf-8', errors='surrogateescape')
                                    data = headers + b'\r\n\r\n' + body
                                    overlap = data[-200:] if len(data) >= 200 else data
                    dst.sendall(data)
            except:
                pass
            finally:
                try: src.shutdown(socket.SHUT_WR)
                except: pass
                try: src.close()
                except: pass
                try: dst.shutdown(socket.SHUT_WR)
                except: pass
                try: dst.close()
                except: pass

        t_req = threading.Thread(
            target=raw_forward,
            args=(client_socket, target_ssl),
            daemon=True
        )

        t_resp = threading.Thread(
            target=forward_response,
            args=(target_ssl, client_socket, client_origin, is_stream, is_main_js),
            daemon=True
        )

        t_req.start()
        t_resp.start()

    except Exception as e:
        print(f"[!] Error: {e}", flush=True)
        try: client_socket.shutdown(socket.SHUT_WR)
        except: pass
        try: client_socket.close()
        except: pass
        if target_ssl:
            try: target_ssl.shutdown(socket.SHUT_WR)
            except: pass
            try: target_ssl.close()
            except: pass

def load_gravitybridge_config():
    """Load config options from gravitybridge.json if present."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gravitybridge.json")
    default_cfg = {
        "version": "2.0.0",
        "tabs": {"chat": True, "drive": True, "opencode": True},
        "opencode": {"auto_start": True, "open_pc_browser": False}
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except Exception as e:
            print(f"[!] Error reading gravitybridge.json: {e}", flush=True)
    return default_cfg

def stop_opencode_server():
    """Terminates any active OpenCode process listening on OPENCODE_PORT from Task Manager."""
    port = OPENCODE_PORT
    if os.name == 'nt':
        try:
            cmd = f'netstat -ano | findstr :{port}'
            out = subprocess.check_output(cmd, shell=True, text=True, errors='ignore', creationflags=NO_WINDOW_FLAGS)
            pids = set()
            for line in out.splitlines():
                if "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        pid = parts[-1]
                        if pid.isdigit() and pid != "0":
                            pids.add(pid)
            for pid in pids:
                print(f"[+] Terminating active OpenCode process on port {port} (PID {pid})...", flush=True)
                subprocess.run(f'taskkill /F /PID {pid}', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW_FLAGS)
        except Exception:
            pass
    else:
        try:
            cmd = f'lsof -t -i:{port}'
            out = subprocess.check_output(cmd, shell=True, text=True, errors='ignore')
            for pid in out.splitlines():
                pid = pid.strip()
                if pid.isdigit():
                    subprocess.run(f'kill -9 {pid}', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

def ensure_opencode_server(config):
    """Ensure OpenCode web server is running on configured OPENCODE_PORT. If auto_start is False, terminate active session."""
    oc_cfg = config.get("opencode", {})
    tabs_cfg = config.get("tabs", {})
    auto_start = oc_cfg.get("auto_start", True)
    tab_enabled = tabs_cfg.get("opencode", True)

    if not auto_start or not tab_enabled:
        print(f"[*] OpenCode auto_start={auto_start}, tab_enabled={tab_enabled}. Terminating active OpenCode server on port {OPENCODE_PORT}...", flush=True)
        stop_opencode_server()
        return

    port = OPENCODE_PORT
    already_running = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        s.close()
        already_running = True
        print(f"[+] OpenCode server already running on port {port}.", flush=True)
    except Exception:
        pass

    if not already_running:
        print(f"[*] Starting OpenCode server on port {port}...", flush=True)
        npm_path = os.path.expanduser(r"~\AppData\Roaming\npm\opencode.cmd")
        opencode_bin = npm_path if os.path.exists(npm_path) else "opencode"

        try:
            if os.name == 'nt':
                cmd = f'"{opencode_bin}" serve --port {port} --hostname 0.0.0.0'
                subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=True
                )
            else:
                subprocess.Popen(
                    [opencode_bin, "serve", "--port", str(port), "--hostname", "0.0.0.0"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            print(f"[+] OpenCode server launched in background on port {port}.", flush=True)
        except Exception as e:
            print(f"[!] Failed to auto-launch OpenCode server: {e}", flush=True)

    if oc_cfg.get("open_pc_browser", False):
        try:
            print(f"[+] Opening http://localhost:{port} in PC browser...", flush=True)
            webbrowser.open(f"http://localhost:{port}")
        except Exception as e:
            print(f"[!] Failed to open browser on PC: {e}", flush=True)

def start_proxy():
    print("====================================================")
    print("     GravityBridge 5.38 - Dynamic HTTP Proxy        ")
    print("====================================================")

    config = load_gravitybridge_config()
    ensure_opencode_server(config)

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    target_port = None
    while target_port is None:
        print("[*] Searching for active port...", flush=True)
        target_port = find_language_server_port()
        if target_port:
            print(f"[+] Dynamic Routing Active -> Forwarding to https://127.0.0.1:{target_port}", flush=True)
            break
        print("[!] Antigravity app not running. Start the desktop app.", flush=True)
        time.sleep(4)

    bound = False
    for attempt in range(10):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((LISTEN_HOST, PROXY_PORT))
            server.listen(200)
            bound = True
            print(f"[+] Proxy listening on http://0.0.0.0:{PROXY_PORT}", flush=True)
            print("====================================================", flush=True)
            break
        except Exception as e:
            server.close()
            print(f"[!] Bind attempt {attempt + 1}/10 failed on port {PROXY_PORT}: {e}. Retrying in 1s...", flush=True)
            time.sleep(1)

    if not bound:
        print(f"[!] Could not bind to port {PROXY_PORT} after 10 attempts.", flush=True)
        sys.exit(1)

    try:
        while True:
            client_sock, addr = server.accept()
            t = threading.Thread(
                target=handle_client,
                args=(client_sock, target_port),
                daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        print("\n[-] Stopping proxy.", flush=True)
    finally:
        server.close()

if __name__ == "__main__":
    start_proxy()
