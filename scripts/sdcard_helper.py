import os
import sys
import json
import subprocess
import socket

# Helper to read environment variables from .env
def load_env():
    env = {}
    # .env is located one directory up from scripts/
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                val = v.strip()
                if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                    val = val[1:-1]
                env[k.strip()] = val
    return env

ENV = load_env()
ADB_PATH = ENV.get('ADB_EXECUTABLE_PATH', 'adb')
PROXY_PORT = int(ENV.get('PROXY_PORT', '15842'))
TAILSCALE_PHONE_NAME = ENV.get('TAILSCALE_PHONE_NAME', 'android').strip().lower()

def get_tailscale_phone_ip():
    """Run tailscale status and extract phone details. Returns (ip, name, is_active, laptop_active)."""
    try:
        res = subprocess.run(['tailscale', 'status'], capture_output=True, text=True, errors='ignore', timeout=4.0)
        if res.returncode != 0:
            return None, None, False, False
        
        phone_ip = None
        phone_name = None
        phone_active = False
        
        for line in res.stdout.splitlines():
            line_lower = line.lower()
            if 'android' in line_lower or (TAILSCALE_PHONE_NAME and TAILSCALE_PHONE_NAME in line_lower):
                parts = line.split()
                if len(parts) >= 4:
                    phone_ip = parts[0]
                    phone_name = parts[1]
                    phone_active = 'active' in line_lower
                    break
        return phone_ip, phone_name, phone_active, True
    except subprocess.TimeoutExpired:
        return None, None, False, False
    except Exception:
        return None, None, False, False

def check_proxy_running(port):
    """Check if the local proxy port is open."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        result = s.connect_ex(('127.0.0.1', port))
        s.close()
        return result == 0
    except Exception:
        return False

def get_adb_device(phone_ip):
    """Attempt connection to phone and verify ADB devices."""
    if phone_ip:
        try:
            subprocess.run([ADB_PATH, 'connect', f'{phone_ip}:5555'], capture_output=True, timeout=3.0)
        except Exception:
            pass
            
    try:
        res = subprocess.run([ADB_PATH, 'devices'], capture_output=True, text=True, errors='ignore', timeout=4.0)
        lines = res.stdout.splitlines()
        devices = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith('List of devices'):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == 'device':
                devices.append(parts[0])
        
        if not devices:
            return None
            
        # Prioritize matching phone_ip
        if phone_ip:
            for dev in devices:
                if dev.startswith(phone_ip) or phone_ip in dev:
                    return dev
        return devices[0]
    except Exception:
        return None

def cmd_status():
    """Print the connectivity status report."""
    phone_ip, phone_name, ts_active, ts_laptop = get_tailscale_phone_ip()
    device_id = get_adb_device(phone_ip) if phone_ip else None
    
    device_model = None
    if device_id:
        try:
            res = subprocess.run([ADB_PATH, '-s', device_id, 'shell', 'getprop ro.product.model'], capture_output=True, text=True, errors='ignore', timeout=4.0)
            if res.returncode == 0:
                device_model = res.stdout.strip()
        except Exception:
            pass
            
    proxy_ok = check_proxy_running(PROXY_PORT)
    
    report = {
        "tailscale": {
            "laptop_active": ts_laptop,
            "phone_connected": phone_ip is not None,
            "phone_ip": phone_ip,
            "phone_name": phone_name,
            "phone_active": ts_active
        },
        "adb": {
            "device_connected": device_id is not None,
            "device_id": device_id,
            "device_model": device_model
        },
        "proxy": {
            "running": proxy_ok,
            "port": PROXY_PORT
        }
    }
    print(json.dumps(report, indent=2))

def cmd_search(query):
    """Search for matching files/folders on phone's sdcard."""
    phone_ip, phone_name, ts_active, ts_laptop = get_tailscale_phone_ip()
    device_id = get_adb_device(phone_ip) if phone_ip else None
    if not device_id:
        print(json.dumps({"success": False, "error": "No ADB device connected. Please connect your phone."}))
        return

    cleaned_query = query.strip().strip("/")
    path_to_check = f"/sdcard/{cleaned_query}"
    
    # Check if query points to a directory
    is_dir = False
    try:
        check = subprocess.run([
            ADB_PATH, '-s', device_id, 'shell', f"[ -d '{path_to_check}' ] && echo 'dir' || echo 'not'"
        ], capture_output=True, text=True, errors='ignore', timeout=3.0)
        if "dir" in check.stdout:
            is_dir = True
    except Exception:
        pass

    items = []
    try:
        if is_dir:
            # List files in this directory sorted by time (newest first)
            cmd = f"ls -lt '{path_to_check}'"
            res = subprocess.run([
                ADB_PATH, '-s', device_id, 'shell', cmd
            ], capture_output=True, text=True, errors='ignore', timeout=8.0)
            
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("total"):
                    continue
                parts = line.split(None, 7)
                if len(parts) >= 8:
                    perms = parts[0]
                    try:
                        size = int(parts[4])
                    except ValueError:
                        size = 0
                    name = parts[7]
                    t = 'directory' if perms.startswith('d') else 'file'
                    items.append({
                        "path": f"{path_to_check}/{name}",
                        "type": t,
                        "size_bytes": size
                    })
        else:
            # Do a find search on /sdcard
            search_cmd = f"find -L /sdcard -path '/sdcard/Android' -prune -o -maxdepth 3 -iname '*{cleaned_query}*' -exec stat -c '%n|%F|%s' {{}} \\;"
            res = subprocess.run([
                ADB_PATH, '-s', device_id, 'shell', search_cmd
            ], capture_output=True, text=True, errors='ignore', timeout=8.0)
            
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if '|' in line:
                        parts = line.split('|', 2)
                        if len(parts) == 3:
                            path, f_type, size_str = parts
                            if '/sdcard/Android' in path:
                                continue
                            try:
                                size = int(size_str)
                            except ValueError:
                                size = 0
                            
                            t = 'directory' if 'directory' in f_type.lower() else 'file'
                            items.append({
                                "path": path,
                                "type": t,
                                "size_bytes": size
                            })
        print(json.dumps({"success": True, "items": items}, indent=2))
    except subprocess.TimeoutExpired:
        print(json.dumps({"success": False, "error": "ADB search timed out"}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))

def cmd_pull(phone_path):
    """Pull the specified phone path to the AutomaticUploads local folder."""
    phone_ip, phone_name, ts_active, ts_laptop = get_tailscale_phone_ip()
    device_id = get_adb_device(phone_ip) if phone_ip else None
    if not device_id:
        print(json.dumps({"success": False, "error": "No ADB device connected"}))
        return
        
    dest_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'DeviceUploads', 'AutomaticUploads'))
    os.makedirs(dest_dir, exist_ok=True)
    
    try:
        # Run adb pull
        res = subprocess.run([
            ADB_PATH, '-s', device_id, 'pull', phone_path, dest_dir
        ], capture_output=True, text=True, errors='ignore', timeout=60.0)
        
        if res.returncode == 0:
            basename = os.path.basename(phone_path.rstrip('/'))
            local_path = os.path.join(dest_dir, basename)
            # Make path display look nice
            rel_path = os.path.relpath(local_path, os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))).replace('\\', '/')
            print(json.dumps({"success": True, "local_path": rel_path}))
        else:
            print(json.dumps({"success": False, "error": res.stderr or res.stdout or "ADB pull failed"}))
    except subprocess.TimeoutExpired:
        print(json.dumps({"success": False, "error": "ADB pull timed out"}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))

def main():
    if len(sys.argv) < 2:
        print("Usage: python sdcard_helper.py [status|search|pull] [args...]")
        sys.exit(1)
        
    action = sys.argv[1]
    if action == 'status':
        cmd_status()
    elif action == 'search':
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "No query provided for search"}))
            sys.exit(1)
        query = sys.argv[2]
        cmd_search(query)
    elif action == 'pull':
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "No path provided for pull"}))
            sys.exit(1)
        phone_path = sys.argv[2]
        cmd_pull(phone_path)
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)

if __name__ == '__main__':
    main()
