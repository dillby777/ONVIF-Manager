# ONVIF Device Manager

A cross-platform desktop app for inspecting ONVIF cameras and NVRs in real time. Built as a modern replacement for the old Windows-only ONVIF Device Manager.

Tested with Dahua NVRs. Should work with any ONVIF-compliant device.

---

## Requirements

- Python 3.9+
- Homebrew (macOS)

---

## Install

**1. Install Tcl/Tk (macOS only)**

```bash
brew install python-tk
```

**2. Install Python dependencies**

```bash
pip3 install customtkinter requests
```

---

## Run

```bash
python3 onvif_manager.py
```

---

## Adding a Device

**Option A — Discover**
Click **Discover** in the sidebar. The app sends a WS-Discovery broadcast to the local network and lists any ONVIF devices that respond. Click a discovered device to connect.

**Option B — Add manually**
Click **+ Add**, enter the device IP, port, username, and password, then click Connect.

> **Dahua NVR notes:**
> - Default ONVIF port is **80** (not 8080)
> - ONVIF must be enabled at: `Setup → Network → Advanced → Integration Protocol`
> - The ONVIF username/password is set separately from the main NVR login on that same page

---

## Tabs

Once connected, six tabs populate with live data.

### Info
- Device details: manufacturer, model, firmware version, serial number, hostname
- System date and time (from the device clock)
- All ONVIF service endpoints (Device, Media, Events, PTZ, Imaging, Analytics)
- ONVIF scopes

### Media
- All media profiles with codec, resolution, FPS, bitrate, and quality settings
- Video sources with resolution and framerate

### Streams
- RTSP URIs for every profile, with credentials already injected
- Double-click any row to copy the URI to your clipboard
- Paste into VLC via **Media → Open Network Stream** to verify the feed

### Events
- Live scrolling event log, newest events at the top
- Color coded: **amber** for motion, **red** for alarms and tampering, **green** for connection events
- Filter box to search by topic name
- Clear button to reset the log
- Event counter showing total events received this session

### Network
- Network interfaces with IP address, MAC address, and subnet prefix
- NTP configuration (source and server addresses)

### Users
- ONVIF user accounts and their permission levels

---

## Troubleshooting

**`No module named '_tkinter'`**
Tkinter isn't bundled with Homebrew Python by default.
```bash
brew install python-tk
```
If you're on Python 3.14 specifically:
```bash
brew install python-tk@3.14
```

**"Sender not Authorized"**
This is an auth issue, not a credentials issue — `onvif-zeep` adds WS-Security headers that many Dahua devices reject. This app uses raw HTTP digest auth (the same method as VLC, Blue Iris, and Home Assistant) which avoids that problem entirely.

If you still see it, double-check that the username matches exactly what's shown in the NVR's Integration Protocol page — it's case-sensitive and separate from the main admin account.

**Events tab shows "Events unavailable"**
Some NVRs only expose the event service on a specific port or require PullPoint subscriptions to be explicitly enabled. Check that event rules are configured in `Setup → Event` on the NVR web UI.

**Device not found via Discover**
WS-Discovery uses UDP multicast, which can be blocked by managed switches or VLANs. Use **+ Add** to enter the IP manually if discovery doesn't find your device.
