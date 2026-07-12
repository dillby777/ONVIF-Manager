# ONVIF Inspector — Dahua NVR Real-time Monitor

A terminal-based ONVIF explorer with live event streaming. Built for Dahua NVRs
but works with any ONVIF-compliant device.

## Install

```bash
pip install onvif-zeep wsdiscovery rich textual
```

## Run

```bash
python onvif_inspector.py --host 192.168.1.100 --password yourpassword
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | required | NVR IP address |
| `--port` | 80 | ONVIF HTTP port (try 8080 if 80 fails) |
| `--user` | admin | Username |
| `--password` | required | Password |

## What you'll see

The UI has four live-updating panels:

1. **Device** — Manufacturer, model, firmware, serial, hostname
2. **Capabilities** — Which ONVIF services the NVR exposes and their endpoints
3. **Media Profiles** — All video channels: codec, resolution, FPS, bitrate
4. **Stream URIs** — RTSP URLs with credentials injected (paste into VLC to test)
5. **Live Events** — Real-time scrolling feed of all ONVIF events:
   - Motion detection (per channel)
   - Video loss / tampering
   - I/O alarm inputs
   - Line crossing / intrusion (if configured)
   - Recording state changes

Press `Ctrl+C` to exit.

## Dahua-specific notes

- Default ONVIF port is **80** (not 8080 like some brands)
- ONVIF must be enabled in: **Setup → Network → Advanced → Integration Protocol**
- Make sure the ONVIF user has sufficient permissions (or use the admin account)
- If events don't appear, check **Setup → Event** and ensure rules are enabled

## Troubleshooting

**"Connection failed"** — Check IP, port, and that ONVIF is enabled on the NVR.

**"Events unavailable"** — The NVR may not support PullPoint subscriptions on the
  ONVIF port. Try enabling "ONVIF Protocol" in the NVR web UI under Network settings.

**Clock skew errors** — ONVIF auth is time-sensitive. Make sure your Mac's clock
  is synced (it almost always is).

**Port 80 blocked** — Some Dahua NVRs use port 8080 for ONVIF. Try `--port 8080`.
