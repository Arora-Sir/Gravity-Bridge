# GravityBridge -- Complete Feature Reference & Architecture Deep-Dive

> **Single Source of Truth** -- Everything about the GravityBridge project: every feature, every design decision, how we built it, what we backtracked on, and why.

---

## Table of Contents

1. [Project Overview & Origin](#1-project-overview--origin)
2. [Architecture Overview](#2-architecture-overview)
3. [How the Proxy Works (Under the Hood)](#3-how-the-proxy-works-under-the-hood)
4. [Antigravity Integration -- Running Chat on Phone Browser](#4-antigravity-integration--running-chat-on-phone-browser)
5. [Upload Portal -- Frontend Deep-Dive](#5-upload-portal--frontend-deep-dive)
6. [Phone Drive Explorer (ADB Browser)](#6-phone-drive-explorer-adb-browser)
7. [File Transfer Engine -- All Upload Modes](#7-file-transfer-engine--all-upload-modes)
8. [Server-Side Progress Tracking (VPN Bypass)](#8-server-side-progress-tracking-vpn-bypass)
9. [ZIP Auto-Extraction](#9-zip-auto-extraction)
10. [Network Architecture -- Wi-Fi, Tailscale & Mobile Data](#10-network-architecture--wi-fi-tailscale--mobile-data)
11. [SPA-Style Instant Page Switching](#11-spa-style-instant-page-switching)
12. [Retry Mechanism for Failed Transfers](#12-retry-mechanism-for-failed-transfers)
13. [main.js Injection -- Shim + Floating Widget](#13-mainjs-injection--shim--floating-widget)
14. [Storage Naming -- DeviceUploads vs Phone sdcard](#14-storage-naming--deviceuploads-vs-phone-sdcard)
15. [Backtracking & Problem History](#15-backtracking--problem-history)
16. [Version History](#16-version-history)

---

## 1. Project Overview & Origin

**Goal:** Transfer files and folders from an Android phone to a Windows laptop wirelessly -- without USB, without Google Drive, without any 3rd party cloud.

**Starting point:** The Antigravity AI desktop app (Electron-based) runs a local language server on a dynamic HTTPS port (e.g., `65286`). It is only accessible from `localhost`, meaning a phone browser can't open it directly.

**The GravityBridge proxy** runs on port `15842` and does two things simultaneously:
1. Acts as an HTTP-to-HTTPS reverse proxy, forwarding all Antigravity API traffic so the phone browser can use the full chat assistant.
2. Serves its own custom `/upload` portal page for phone-to-laptop file transfer.

---

## 2. Architecture Overview

```
Phone Browser (Chrome/Samsung/Firefox)
     |
     | HTTP (port 15842 via Wi-Fi / Tailscale / Mobile Data)
     v
[GravityBridge proxy.py :15842]
     |
     |-- GET /upload          --> Serves custom Phone Drive portal HTML
     |-- GET /adb-ls          --> ADB shell: list phone directory
     |-- POST /adb-pull       --> ADB pull: copy phone path to laptop
     |-- POST /adb-pull-auto  --> ADB auto-find + pull by folder name
     |-- POST /upload         --> Streaming file write to disk
     |-- GET /upload-progress --> Real-time bytes-received counter
     |
     |-- All other routes --> HTTP-to-HTTPS reverse proxy
              |
              | SSL (self-signed, unverified context)
              v
     [Antigravity Language Server :65286]
              |
              v
     Antigravity AI Chat (React SPA)
```

---

## 3. How the Proxy Works (Under the Hood)

### Port Discovery (Dynamic)

The Antigravity language server starts on a **random port** every time. The proxy discovers it automatically:

1. Uses `tasklist` + `netstat -ano` to find PIDs of `Antigravity.exe` and `language_server.exe`
2. Scans their listening ports
3. Tries to connect each port with SSL and sends `GET /` -- if it gets `200 OK`, that's the active port
4. Falls back to scanning Electron's DevTools `/json` endpoint to extract the URL

### Request Rewriting

For every proxied request, the proxy modifies headers before forwarding:
- **Host** → `127.0.0.1:{target_port}` (required for SSL SNI)
- **Origin** → `https://127.0.0.1:{target_port}` (avoids CORS rejection)
- **Referer** → rewrites host portion only
- **Accept-Encoding** → forced to `identity` (disables gzip, so we can read/modify responses)
- **Connection** → `close` for unary calls, kept alive for gRPC streams

### gRPC Stream Detection

The language server uses gRPC-over-HTTP/2 for streaming calls (e.g., `JetboxSubscribeToState`). These must NOT have `Connection: close` injected, or they will terminate prematurely. The proxy detects streams by checking if the path contains `/JetboxSubscribeTo` and skips the `Connection: close` rewrite.

### Response CORS Injection

For all responses, the proxy injects:
- `Access-Control-Allow-Origin: {client_origin}`
- `Access-Control-Allow-Credentials: true`
- `X-Accel-Buffering: no`

---

## 4. Antigravity Integration -- Running Chat on Phone Browser

### The Problem

The Antigravity React app uses `window.nativeStorage` (an Electron-native API) for persistent storage. When opened in a regular browser, this API is `undefined` and the app crashes silently.

### The Solution -- main.js Shim Injection

The proxy intercepts `GET /main.js` responses and **prepends a JavaScript shim** that polyfills `window.nativeStorage` using `localStorage`:

```js
window.nativeStorage = {
  getItems: () => Promise.resolve(/* all localStorage items */),
  updateItems: (changes) => Promise.resolve(/* apply changes to localStorage */)
};
```

This is injected before any app code runs, so the React app boots cleanly in any mobile browser.

### Chunked Transfer Handling

The language server returns `main.js` with `Transfer-Encoding: chunked`. The shim is injected as the **first valid HTTP chunk** (with a correct hex size prefix), so the browser's chunked parser doesn't break.

### File Input Unlock -- `accept="*/*"`

The Antigravity app restricts file inputs to specific MIME types, blocking mobile file pickers from showing all files. The proxy patches this in `main.js` by replacing:

```js
accept:b  // original (variable, restricted)
```
with:
```js
accept:"*/*"  // patched (all files allowed)
```

---

## 5. Upload Portal -- Frontend Deep-Dive

The portal is a fully self-contained HTML page served by `GET /upload`. All CSS and JS is inline -- no external dependencies except Google Fonts.

### Design System

```css
--bg: #090a0f;                          /* Near-black background */
--panel-bg: rgba(17, 19, 28, 0.75);    /* Glassmorphism panel */
--accent: #6366f1;                      /* Indigo (local uploads) */
--amber: #d97706;                       /* Amber (phone ADB transfers) */
--success: #10b981;                     /* Green (completed) */
```

Font: **Outfit** (Google Fonts) -- clean, modern sans-serif.

### Tab System

Two tabs share a **unified staging queue**:
- **📱 Phone Drive** -- ADB-powered phone browser with checkboxes
- **💻 Local Uploads (Drop)** -- Drag & drop zone for laptop files

Items from both tabs get staged into the same queue and transferred together on **Start Transfer**.

### Responsive Layout

- On screens > 520px: header is `flex-row` with title left and back-tab on the right edge
- On screens ≤ 520px: header stacks vertically (`flex-direction: column`)
- The back-to-chat amber tab is `position:fixed` so it never participates in document flow

---

## 6. Phone Drive Explorer (ADB Browser)

### How It Works

1. Phone opens portal → browser calls `GET /adb-ls?path=/sdcard`
2. Proxy runs: `adb shell "cd '/sdcard' && for f in *; do [...]; done"`
3. Returns JSON: `[{type: "d", name: "DCIM"}, {type: "f", name: "note.txt"}, ...]`
4. Browser renders sorted folders first, then files, each with a checkbox

### Auto-Connect

Before every ADB command, the proxy runs:
```bash
adb connect {LAST_KNOWN_PHONE_IP}:5555
```
`LAST_KNOWN_PHONE_IP` is updated dynamically from `client_socket.getpeername()[0]` on every incoming request -- so the phone's IP is always tracked regardless of which network it's on.

### Breadcrumb Navigation

The explorer renders a clickable breadcrumb trail:
```
Internal Storage > Download > MyFolder
```
Each breadcrumb segment is a clickable link that calls `loadPhoneDirectory()` for that path.

### Go Up

A special `.. (Go Up)` entry at the top of each directory listing navigates to the parent directory.

### Checkbox Staging

- Checking a folder/file calls `stageItem()` which adds it to `stagedItems[]`
- Unchecking calls `unstageItem()` which removes it by ID
- Staged items are immediately visible in the Transfer Queue below

---

## 7. File Transfer Engine -- All Upload Modes

### Mode 1 -- ADB Folder/File Pull (Phone Drive checkboxes)

For ADB-staged items (`type: 'adb-folder'` or `'adb-file'`):

```
POST /adb-pull
Body: { path: "/sdcard/DCIM/Camera", folder: "PhoneUploads" }
```

Proxy runs:
```bash
adb pull /sdcard/DCIM/Camera C:\...\DeviceUploads\PhoneUploads\Camera
```

### Mode 2 -- HTTP File Upload (Local Drop Zone)

For locally dropped files (`type: 'local'`):

```
POST /upload?folder=PhoneUploads&filename=photo.jpg
Body: [raw file bytes]
```

Proxy streams bytes directly to disk:
```python
with open(filepath, 'wb') as f:
    f.write(initial_body)
    while remaining > 0:
        chunk = client_socket.recv(65536)
        f.write(chunk)
```

### Mode 3 -- Auto-Pull by Folder Name (Drag & Drop fallback)

When a folder is **dragged onto the drop zone** on mobile, the browser sandbox returns 0 files (mobile security restriction). The proxy detects this and automatically:
1. Captures the dropped folder name from `webkitGetAsEntry()`
2. Calls `POST /adb-pull-auto` with the folder name
3. Proxy searches phone at common paths (`/sdcard/Documents/`, `/sdcard/Download/`, `/sdcard/`)
4. Falls back to `adb shell find /sdcard -maxdepth 4 -type d -name 'FolderName'`
5. Pulls the found path to `DeviceUploads/`

---

## 8. Server-Side Progress Tracking (VPN Bypass)

### The Problem

When uploading over **Tailscale VPN** (or any local VPN tunnel), the phone OS sends data into the local VPN tunnel at full local-network speed. The VPN then forwards it slowly over cellular/internet. This causes:
- `xhr.upload.onprogress` to fire at 100% almost instantly (tunnel is fast)
- But actual disk write on laptop happens much later (cellular is slow)
- Result: progress bar jumps to 100% but file isn't done yet

### The Solution -- `UPLOAD_PROGRESS` dict + polling

1. When `POST /upload` starts, proxy registers:
   ```python
   UPLOAD_PROGRESS[progress_key] = {"received": 0, "total": content_length}
   ```
2. As each chunk arrives and is written to disk, the counter updates:
   ```python
   UPLOAD_PROGRESS[progress_key]["received"] += len(chunk)
   ```
3. The browser polls `GET /upload-progress?key={filename}` every 300ms
4. Gets back `{received: 4194304, total: 10485760}` → renders true progress %

This bypasses the VPN buffering entirely -- you see actual disk I/O speed, not tunnel speed.

---

## 9. ZIP Auto-Extraction

Mobile browsers can't upload directory structures directly. A workaround is to ZIP a folder and upload the `.zip`.

When `POST /upload` receives a file ending in `.zip`:
1. File is written to `DeviceUploads/{folder}/{filename}.zip`
2. Proxy auto-extracts: `ZipFile.extractall(extract_dir)`
3. Extracted folder lands at `DeviceUploads/{folder}/{filename_without_zip}/`
4. The original `.zip` is deleted (cleaned up)

Path components are sanitized during extraction to prevent directory traversal (`..` stripped).

---

## 10. Network Architecture -- Wi-Fi, Tailscale & Mobile Data

### Same Wi-Fi (192.168.x.x)

Simplest setup. Both devices on same router. Latency ~1-5ms. Full ADB works.

### Tailscale (100.x.x.x)

Tailscale creates a WireGuard mesh VPN between any devices signed into the same account. The phone and laptop both get stable `100.x.x.x` IPs that work from anywhere.

- **Proxy detects phone IP** from socket `getpeername()`, not from any header -- so even when the phone switches from Wi-Fi to cellular, ADB auto-reconnects to the new Tailscale IP.
- ADB works over Tailscale because `adb connect {tailscale_ip}:5555` goes through the WireGuard tunnel.

### Mobile Data Only (Hotspot scenario)

If you're away from Wi-Fi:
1. Phone uses mobile data
2. Tailscale connects phone to laptop via relay nodes
3. Upload speed limited by cellular upload bandwidth (typically 5-50 Mbps on 5G)
4. ADB still works if Tailscale is active

---

## 11. SPA-Style Instant Page Switching

### The Problem

Tapping the orange "📱 Phone Drive" tab on the Antigravity chat page navigated to `/upload` as a full page load. On mobile, this took 30-40 seconds because:
- Antigravity's React app has a large `main.js` (injected shim + app code)
- Mobile browser clears all connections and re-initiates from scratch
- gRPC streams have to reconnect

### The Solution -- Hidden iframe Overlay

Instead of navigating, the switcher now works like a React SPA:

```
[Chat Page]
  └── hidden <div id="gb-overlay" style="display:none">
        └── <iframe src="/upload"> (preloaded silently after 1 second)

[Click 📱 tab]
  → overlay.style.display = 'block'   // instant, <100ms
  → chat page stays loaded underneath

[Click 💬 tab inside upload portal]
  → window.parent.postMessage('gb:back-to-chat', '*')
  → parent chat page receives message
  → overlay.style.display = 'none'    // instant, <100ms
  → chat tab reappears
```

The iframe preloads `/upload` 1 second after the chat page loads, so by the time you tap the tab, the portal is already initialized.

### Symmetric Design

Both pages have a matching **amber right-edge side-tab** (`position: fixed, right: 0, top: 140px`):
- **Chat page:** `📱` tab (injected via `main.js` shim) → shows iframe overlay
- **Upload portal:** `💬` tab → sends `postMessage` to parent

---

## 12. Retry Mechanism for Failed Transfers

If any item fails in the queue (network drop, ADB disconnect, etc.):

1. Item status turns red "Failed"
2. A `🔄 Retry` amber button appears next to that item only
3. Clicking retry:
   - Resets that item's progress bar to `20%`
   - Re-runs exactly the same transfer logic (HTTP upload or ADB pull)
   - On success → turns green "Completed"
   - On failure → shows Retry button again

Other items in the queue are not affected.

---

## 13. main.js Injection -- Shim + Floating Widget

The proxy intercepts `GET /main.js` and prepends two scripts:

### Script 1 -- nativeStorage Shim

Polyfills `window.nativeStorage` using `localStorage` so Antigravity's React app boots in mobile browsers.

### Script 2 -- SPA Switcher

Injects the iframe overlay + amber side-tab code described in §11. Key behaviors:
- `createOverlay()` -- creates the hidden iframe div
- `injectBtn()` -- creates the amber side-tab button
- `setTimeout(createOverlay, 1000)` -- silently preloads iframe 1s after page load
- `window.addEventListener('message', ...)` -- listens for `'gb:back-to-chat'` from iframe

`setInterval(injectBtn, 3000)` periodically re-injects the button in case Antigravity's React SPA removes it during client-side navigation.

### Chunked Encoding Compatibility

If the language server returns `main.js` with `Transfer-Encoding: chunked`, the shim is wrapped as a proper HTTP chunk:
```
<hex_length>\r\n
<shim_bytes>\r\n
<rest of original chunks...>
```
The `Content-Length` header is stripped (incompatible with prepended content).

---

## 14. Storage Naming -- DeviceUploads vs Phone sdcard

### Naming Confusion Problem

The phone's internal storage is referred to as `/sdcard` in ADB shell and sometimes described as "storage". The laptop also originally had a folder called `storage/` for received files. This caused confusion when referring to "storage" in conversation.

### Resolution

- **Phone storage** → always referred to as `/sdcard` or "Internal Storage"
- **Laptop destination** → renamed from `storage/` to **`DeviceUploads/`**
- Default subfolder for phone transfers → **`DeviceUploads/PhoneUploads/`**

All file writes in `proxy.py` use:
```python
storage_dir = os.path.join(os.getcwd(), "DeviceUploads", folder_path)
```

---

## 15. Backtracking & Problem History

### Problem 1 -- Mobile browser can't upload directories

**What we tried first:** Standard `<input type="file" webkitdirectory>` -- browsers on Android showed folders but uploaded 0 bytes.

**Root cause:** Android browser sandbox blocks `FileSystemDirectoryEntry.createReader()` from actually reading directory contents. `DataTransfer.items[0].webkitGetAsEntry()` returns an entry with `isDirectory=true` but `readEntries()` returns empty array.

**Solution:** When directory scan returns 0 files, auto-trigger `POST /adb-pull-auto` with the folder name. ADB on the laptop side does the actual copy.

### Problem 2 -- Progress bar jumping to 100% instantly over VPN

**Root cause:** Tailscale VPN tunnel accepts data at local-network speed. `xhr.upload.onprogress` fires based on bytes handed to the OS socket buffer, not bytes received on the other end.

**Solution:** Server-side `UPLOAD_PROGRESS` dict + 300ms polling. Progress now tracks actual disk writes.

### Problem 3 -- 30-40 second black screen when switching pages

**Root cause:** Full `window.location.href` navigation from chat page to `/upload` caused the browser to tear down all connections, clear the React app, re-download `main.js` (with shim), re-initialize gRPC streams. Very slow on mobile.

**Solution:** Hidden iframe overlay -- keeps chat page alive underneath, shows/hides upload portal via `display:none/block`.

### Problem 4 -- Multiple Python processes running

**Cause:** Previous version was killed and new one started without verifying the old one died, leaving two proxies competing on port 15842 (with port conflict errors). **Fix:** Always `kill` old task before starting new one.

### Problem 5 -- Duplicate "Storage Directory Browser" heading

**Cause:** Previous version had a redundant header label ("Storage Directory Browser") above the breadcrumbs, which duplicated the tab button's label ("Phone Drive").

**Fix:** Removed the header row entirely. The breadcrumb (`Internal Storage > ...`) and IP badge are now on a single clean row inside the explorer section.

---

## 16. Version History

| Version | Key Changes |
|---|---|
| v5.26 | Initial drag-and-drop file upload with flat file support |
| v5.27 | ADB wireless panel with manual phone path input |
| v5.28 | Auto-detection of phone IP from socket headers |
| v5.29 | Graceful fallback: directory drop → ADB auto-pull |
| v5.30 | Tailscale cross-network support via dynamic IP tracking |
| v5.31 | ZIP auto-extraction with safe path sanitization |
| v5.32 | Dual-tab interface: Phone Drive + Local Drop unified queue |
| v5.33 | Renamed "Storage Directory Browser" header → "Phone Drive" |
| v5.34 | Renamed laptop `storage/` → `DeviceUploads/`; default to `PhoneUploads/` |
| v5.35 | Removed redundant explorer header; IP badge moved to breadcrumb row |
| v5.36 | Server-side progress tracking; single-item retry buttons; floating nav widgets |
| v5.37 | Responsive mobile layout; amber right-edge side-tab on chat page |
| v5.38 | SPA iframe overlay switching (instant <300ms); matching amber tab on upload portal |
