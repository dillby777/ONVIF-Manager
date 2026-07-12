#!/usr/bin/env python3
"""
ONVIF Device Manager
A desktop GUI for inspecting ONVIF cameras and NVRs.
Modelled after ONVIF Device Manager (Windows) but cross-platform.

Requirements:
    pip install customtkinter requests
"""

import threading
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from queue import Queue, Empty
import tkinter as tk
import tkinter.ttk as ttk
import customtkinter as ctk
import requests
from requests.auth import HTTPDigestAuth
import json
import socket

# ── Theme ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG         = "#1a1d23"
PANEL      = "#21252e"
SIDEBAR    = "#181b21"
ACCENT     = "#3b8ef0"
ACCENT2    = "#2563c7"
SUCCESS    = "#22c55e"
WARNING    = "#f59e0b"
DANGER     = "#ef4444"
TEXT       = "#e8eaf0"
TEXT_DIM   = "#7a7f8e"
BORDER     = "#2e3340"
ROW_ALT    = "#252930"
ROW_EVEN   = "#21252e"
HEADER_BG  = "#1e2229"

FONT_MONO  = ("JetBrains Mono", 11) if True else ("Courier New", 11)
FONT_BODY  = ("Inter", 12)
FONT_SMALL = ("Inter", 10)
FONT_TITLE = ("Inter", 13, "bold")
FONT_HEAD  = ("Inter", 11, "bold")

# ── ONVIF / SOAP helpers ───────────────────────────────────────────────────────
NS_MAP = {
    's':    'http://www.w3.org/2003/05/soap-envelope',
    'tds':  'http://www.onvif.org/ver10/device/wsdl',
    'trt':  'http://www.onvif.org/ver10/media/wsdl',
    'tev':  'http://www.onvif.org/ver10/events/wsdl',
    'tt':   'http://www.onvif.org/ver10/schema',
    'wsnt': 'http://docs.oasis-open.org/wsn/b-2',
    'wsa':  'http://www.w3.org/2005/08/addressing',
    'tptz': 'http://www.onvif.org/ver20/ptz/wsdl',
    'timg': 'http://www.onvif.org/ver20/imaging/wsdl',
}

def soap_envelope(body_xml, header_xml=""):
    return f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  {"<s:Header>" + header_xml + "</s:Header>" if header_xml else ""}
  <s:Body>{body_xml}</s:Body>
</s:Envelope>'''

def soap_post(url, body_xml, auth, timeout=12, header_xml=""):
    payload = soap_envelope(body_xml, header_xml).encode()
    r = requests.post(url, data=payload, auth=auth,
                      headers={"Content-Type": "application/soap+xml"},
                      timeout=timeout, verify=False)
    r.raise_for_status()
    return ET.fromstring(r.content)

def findall_ns(root, tag):
    results = []
    for ns in NS_MAP.values():
        results.extend(root.findall(f".//{{{ns}}}{tag}"))
    return results

def find_ns(root, tag):
    for ns in NS_MAP.values():
        el = root.find(f".//{{{ns}}}{tag}")
        if el is not None:
            return el
    return None

def el_text(el):
    return el.text.strip() if el is not None and el.text else ""

def attr(el, name, default=""):
    if el is None: return default
    return el.get(name, default)

# ── Discovery ──────────────────────────────────────────────────────────────────
WS_DISCOVERY_MSG = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:onvif-discover-001</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""

def discover_devices(timeout=3):
    """WS-Discovery broadcast, returns list of {address, types}"""
    found = []
    seen = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(WS_DISCOVERY_MSG.encode(),
                    ("239.255.255.250", 3702))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                try:
                    root = ET.fromstring(data)
                    xaddrs = findall_ns(root, "XAddrs")
                    addrs = []
                    for x in xaddrs:
                        if x.text:
                            addrs.extend(x.text.strip().split())
                    # Prefer http addresses
                    http_addrs = [a for a in addrs if a.startswith("http")]
                    best = http_addrs[0] if http_addrs else (addrs[0] if addrs else f"http://{ip}/onvif/device_service")
                    types_els = findall_ns(root, "Types")
                    types = el_text(types_els[0]) if types_els else ""
                    found.append({"ip": ip, "address": best, "types": types})
                except Exception:
                    found.append({"ip": ip, "address": f"http://{ip}/onvif/device_service", "types": ""})
            except socket.timeout:
                break
    except Exception:
        pass
    finally:
        try: sock.close()
        except: pass
    return found

# ── ONVIF Client ───────────────────────────────────────────────────────────────
class ONVIFClient:
    def __init__(self, host, port, user, password):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.auth = HTTPDigestAuth(user, password)
        self.base_url = f"http://{host}:{port}"
        self.device_url = f"{self.base_url}/onvif/device_service"
        self.media_url = None
        self.events_url = None
        self.ptz_url = None
        self.imaging_url = None

    def get_device_info(self):
        root = soap_post(self.device_url, '''
            <tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
        ''', self.auth)
        info = {}
        for field in ['Manufacturer','Model','FirmwareVersion','SerialNumber','HardwareId']:
            el = find_ns(root, field)
            if el is not None:
                info[field] = el_text(el)
        return info

    def get_hostname(self):
        root = soap_post(self.device_url, '''
            <tds:GetHostname xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
        ''', self.auth)
        el = find_ns(root, 'Name')
        return el_text(el) if el is not None else ""

    def get_network_interfaces(self):
        root = soap_post(self.device_url, '''
            <tds:GetNetworkInterfaces xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
        ''', self.auth)
        ifaces = []
        for ni in findall_ns(root, 'NetworkInterfaces'):
            iface = {}
            name_el = find_ns(ni, 'Name')
            mac_el  = find_ns(ni, 'HwAddress')
            ip_el   = find_ns(ni, 'IPv4Address')
            pfx_el  = find_ns(ni, 'PrefixLength')
            if name_el is not None: iface['Name'] = el_text(name_el)
            if mac_el  is not None: iface['MAC']  = el_text(mac_el)
            if ip_el   is not None: iface['IP']   = el_text(ip_el)
            if pfx_el  is not None: iface['Prefix'] = el_text(pfx_el)
            if iface: ifaces.append(iface)
        return ifaces

    def get_ntp(self):
        try:
            root = soap_post(self.device_url, '''
                <tds:GetNTP xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
            ''', self.auth)
            from_dhcp_el = find_ns(root, 'FromDHCP')
            ntp_manual = findall_ns(root, 'NTPManual')
            ntp_dhcp   = findall_ns(root, 'NTPFromDHCP')
            result = {}
            if from_dhcp_el is not None:
                result['FromDHCP'] = el_text(from_dhcp_el)
            servers = []
            for n in (ntp_manual + ntp_dhcp):
                ip4 = find_ns(n, 'IPv4Address')
                dns = find_ns(n, 'DNSname')
                v = el_text(ip4 or dns)
                if v: servers.append(v)
            result['Servers'] = servers
            return result
        except Exception:
            return {}

    def get_system_date_time(self):
        root = soap_post(self.device_url, '''
            <tds:GetSystemDateAndTime xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
        ''', self.auth)
        result = {}
        tz = find_ns(root, 'TZ')
        if tz is not None: result['Timezone'] = el_text(tz)
        h = find_ns(root, 'Hour'); m = find_ns(root, 'Minute'); s = find_ns(root, 'Second')
        y = find_ns(root, 'Year'); mo = find_ns(root, 'Month'); d = find_ns(root, 'Day')
        if all(x is not None for x in [h,m,s,y,mo,d]):
            result['UTC'] = f"{el_text(y)}-{el_text(mo).zfill(2)}-{el_text(d).zfill(2)} {el_text(h).zfill(2)}:{el_text(m).zfill(2)}:{el_text(s).zfill(2)}"
        return result

    def get_capabilities(self):
        root = soap_post(self.device_url, '''
            <tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
                <tds:Category>All</tds:Category>
            </tds:GetCapabilities>
        ''', self.auth)
        caps = {}
        for svc in ['Device','Events','Media','PTZ','Imaging','Analytics','Recording','Search']:
            els = findall_ns(root, svc)
            if els:
                xaddr = find_ns(els[0], 'XAddr')
                if xaddr is not None:
                    caps[svc] = el_text(xaddr)
        self.media_url   = caps.get('Media',   f"{self.base_url}/onvif/media_service")
        self.events_url  = caps.get('Events',  f"{self.base_url}/onvif/event_service")
        self.ptz_url     = caps.get('PTZ',     None)
        self.imaging_url = caps.get('Imaging', None)
        return caps

    def get_profiles(self):
        url = self.media_url or f"{self.base_url}/onvif/media_service"
        root = soap_post(url, '''
            <trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>
        ''', self.auth)
        profiles = []
        for p in findall_ns(root, 'Profiles'):
            token = p.get('token','')
            name_el = find_ns(p,'Name')
            entry = {'Token': token, 'Name': el_text(name_el) if name_el else token}
            # Video encoder
            vec = findall_ns(p, 'VideoEncoderConfiguration')
            if vec:
                v = vec[0]
                for field, key in [('Encoding','Codec'),('Width','Width'),('Height','Height'),
                                   ('FrameRateLimit','FPS'),('BitrateLimit','Bitrate'),
                                   ('Quality','Quality'),('GovLength','GovLength')]:
                    el = find_ns(v, field)
                    if el is not None: entry[key] = el_text(el)
                if 'Width' in entry and 'Height' in entry:
                    entry['Resolution'] = f"{entry.pop('Width')}×{entry.pop('Height')}"
                if 'Bitrate' in entry:
                    entry['Bitrate'] = entry['Bitrate'] + ' kbps'
            # Video source
            vsc = findall_ns(p, 'VideoSourceConfiguration')
            if vsc:
                src_token = find_ns(vsc[0], 'SourceToken')
                if src_token is not None: entry['SourceToken'] = el_text(src_token)
            profiles.append(entry)
        return profiles

    def get_stream_uri(self, profile_token):
        url = self.media_url or f"{self.base_url}/onvif/media_service"
        body = f'''
            <trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
                <trt:StreamSetup>
                    <tt:Stream xmlns:tt="http://www.onvif.org/ver10/schema">RTP-Unicast</tt:Stream>
                    <tt:Transport xmlns:tt="http://www.onvif.org/ver10/schema">
                        <tt:Protocol>RTSP</tt:Protocol>
                    </tt:Transport>
                </trt:StreamSetup>
                <trt:ProfileToken>{profile_token}</trt:ProfileToken>
            </trt:GetStreamUri>'''
        root = soap_post(url, body, self.auth)
        uri_el = find_ns(root, 'Uri')
        return el_text(uri_el) if uri_el is not None else ""

    def get_video_sources(self):
        url = self.media_url or f"{self.base_url}/onvif/media_service"
        root = soap_post(url, '''
            <trt:GetVideoSources xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>
        ''', self.auth)
        sources = []
        for vs in findall_ns(root, 'VideoSources'):
            token = vs.get('token','')
            w = find_ns(vs,'Width'); h = find_ns(vs,'Height')
            fr = find_ns(vs,'Framerate')
            entry = {'Token': token}
            if w and h: entry['Resolution'] = f"{el_text(w)}×{el_text(h)}"
            if fr: entry['Framerate'] = el_text(fr)
            sources.append(entry)
        return sources

    def get_event_properties(self):
        url = self.events_url or f"{self.base_url}/onvif/event_service"
        try:
            root = soap_post(url, '''
                <tev:GetEventProperties xmlns:tev="http://www.onvif.org/ver10/events/wsdl"/>
            ''', self.auth, timeout=10)
            topics = []
            for t in findall_ns(root, 'TopicSet'):
                topics.append(ET.tostring(t, encoding='unicode'))
            return topics
        except Exception:
            return []

    def create_pullpoint_subscription(self):
        url = self.events_url or f"{self.base_url}/onvif/event_service"
        body = '''<tev:CreatePullPointSubscription xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
            <tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime>
        </tev:CreatePullPointSubscription>'''
        root = soap_post(url, body, self.auth, timeout=15)
        addr_els = findall_ns(root, 'Address')
        if addr_els:
            return el_text(addr_els[0])
        # Fallback
        return url.replace('/event_service', '/subscription')

    def pull_messages(self, sub_url):
        body = '''<tev:PullMessages xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
            <tev:Timeout>PT8S</tev:Timeout>
            <tev:MessageLimit>100</tev:MessageLimit>
        </tev:PullMessages>'''
        r = requests.post(sub_url, data=soap_envelope(body).encode(), auth=self.auth,
                          headers={"Content-Type": "application/soap+xml"},
                          timeout=15, verify=False)
        if r.status_code in (404, 410):
            return None, "expired"
        r.raise_for_status()
        root = ET.fromstring(r.content)
        msgs = findall_ns(root, 'NotificationMessage')
        return msgs, None

    def get_users(self):
        try:
            root = soap_post(self.device_url, '''
                <tds:GetUsers xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
            ''', self.auth)
            users = []
            for u in findall_ns(root, 'User'):
                name = find_ns(u,'Username'); level = find_ns(u,'UserLevel')
                users.append({
                    'Username': el_text(name) if name else '?',
                    'Level': el_text(level) if level else '?'
                })
            return users
        except Exception:
            return []

    def get_scopes(self):
        try:
            root = soap_post(self.device_url, '''
                <tds:GetScopes xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>
            ''', self.auth)
            scopes = []
            for s in findall_ns(root, 'ScopeItem'):
                sc = find_ns(s,'ScopeItem')
                if sc is not None: scopes.append(el_text(sc))
            # Also try direct ScopeItem text
            for s in findall_ns(root, 'Scopes'):
                scope_def = find_ns(s, 'ScopeDef')
                scope_item = find_ns(s, 'ScopeItem')
                if scope_item is not None: scopes.append(el_text(scope_item))
            return list(dict.fromkeys(scopes))  # dedupe
        except Exception:
            return []

def parse_event(msg):
    try:
        utc_time = datetime.utcnow().strftime("%H:%M:%S")
        topic = ""
        source = {}
        data = {}

        topic_els = findall_ns(msg, 'Topic')
        if topic_els:
            raw = el_text(topic_els[0])
            topic = '/'.join(p.split(':')[-1] for p in raw.split('/'))

        for message in findall_ns(msg, 'Message'):
            ut = message.get('UtcTime','')
            if ut: utc_time = ut[11:19] if len(ut) >= 19 else utc_time

            # Walk children to find Source and Data
            for child in message:
                tag_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                target = source if tag_local == 'Source' else data if tag_local == 'Data' else None
                if target is not None:
                    for si in child:
                        si_tag = si.tag.split('}')[-1] if '}' in si.tag else si.tag
                        if si_tag == 'SimpleItem':
                            name = si.get('Name','')
                            val  = si.get('Value','')
                            if name: target[name] = val

        if not topic: return None
        return {'time': utc_time, 'topic': topic, 'source': source, 'data': data,
                'ts': datetime.utcnow()}
    except Exception:
        return None

# ── Main App Window ────────────────────────────────────────────────────────────
class ONVIFManagerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ONVIF Device Manager")
        self.geometry("1280x820")
        self.minsize(900, 600)
        self.configure(fg_color=BG)

        self.devices = {}         # ip -> {client, info, status, ...}
        self.selected_ip = None
        self.event_thread = None
        self.stop_events = threading.Event()
        self.event_queue = Queue()

        self._build_ui()
        self._start_event_drain()

    # ── UI Construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = ctk.CTkFrame(self, width=260, fg_color=SIDEBAR, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(2, weight=1)

        # Sidebar header
        hdr = ctk.CTkFrame(self.sidebar, fg_color="#0f1117", corner_radius=0, height=52)
        hdr.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        hdr.grid_propagate(False)
        ctk.CTkLabel(hdr, text="ONVIF Manager", font=("Inter", 14, "bold"),
                     text_color=TEXT).place(x=16, y=14)

        # Sidebar buttons
        btn_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(10,4))
        btn_frame.grid_columnconfigure((0,1), weight=1)

        self._btn(btn_frame, "Discover", self._discover, ACCENT, 0, 0)
        self._btn(btn_frame, "+ Add", self._show_add_dialog, "#374151", 0, 1)

        # Device list
        list_lbl = ctk.CTkLabel(self.sidebar, text="DEVICES", font=("Inter", 9, "bold"),
                                 text_color=TEXT_DIM)
        list_lbl.grid(row=1, column=0, sticky="w", padx=16, pady=(44,0))

        self.device_list_frame = ctk.CTkScrollableFrame(
            self.sidebar, fg_color="transparent", corner_radius=0)
        self.device_list_frame.grid(row=2, column=0, sticky="nsew", padx=0, pady=(4,0))
        self.device_list_frame.grid_columnconfigure(0, weight=1)

        # Status bar at bottom of sidebar
        self.sidebar_status = ctk.CTkLabel(
            self.sidebar, text="Ready", font=("Inter", 10),
            text_color=TEXT_DIM, anchor="w")
        self.sidebar_status.grid(row=3, column=0, sticky="ew", padx=12, pady=8)

        # Main content
        self.main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)

        # Top bar
        self.topbar = ctk.CTkFrame(self.main, height=52, fg_color=PANEL, corner_radius=0)
        self.topbar.grid(row=0, column=0, sticky="ew")
        self.topbar.grid_propagate(False)
        self.topbar.grid_columnconfigure(1, weight=1)

        self.device_label = ctk.CTkLabel(
            self.topbar, text="No device selected",
            font=("Inter", 13, "bold"), text_color=TEXT)
        self.device_label.grid(row=0, column=0, padx=20, pady=14, sticky="w")

        self.status_dot = ctk.CTkLabel(self.topbar, text="●", font=("Inter", 14),
                                        text_color=TEXT_DIM)
        self.status_dot.grid(row=0, column=1, padx=(0,8), sticky="e")
        self.status_label = ctk.CTkLabel(self.topbar, text="", font=("Inter", 11),
                                          text_color=TEXT_DIM)
        self.status_label.grid(row=0, column=2, padx=(0,20), sticky="e")

        # Tabs
        self.tabview = ctk.CTkTabview(self.main, fg_color=PANEL,
                                       segmented_button_fg_color=PANEL,
                                       segmented_button_selected_color=ACCENT,
                                       segmented_button_selected_hover_color=ACCENT2,
                                       segmented_button_unselected_color=PANEL,
                                       segmented_button_unselected_hover_color="#2d3240",
                                       text_color=TEXT, border_width=0,
                                       corner_radius=0)
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

        for tab_name in ["Info", "Media", "Streams", "Events", "Network", "Users"]:
            self.tabview.add(tab_name)
            self.tabview.tab(tab_name).configure(fg_color=BG)

        self._build_info_tab()
        self._build_media_tab()
        self._build_streams_tab()
        self._build_events_tab()
        self._build_network_tab()
        self._build_users_tab()

        self._show_welcome()

    def _btn(self, parent, text, cmd, color, row, col, **kwargs):
        b = ctk.CTkButton(parent, text=text, command=cmd, fg_color=color,
                           hover_color=ACCENT2 if color==ACCENT else "#4b5563",
                           font=("Inter", 12), height=32, corner_radius=6, **kwargs)
        b.grid(row=row, column=col, padx=3, pady=2, sticky="ew")
        return b

    def _build_info_tab(self):
        tab = self.tabview.tab("Info")
        tab.grid_columnconfigure((0,1), weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Device info card
        self.info_card = self._make_card(tab, "Device Information", 0, 0, colspan=1)
        self.info_tree = self._make_tree(self.info_card,
            columns=("Field", "Value"), widths=(180, 320))

        # Date/time card
        self.dt_card = self._make_card(tab, "System Date & Time", 0, 1, colspan=1)
        self.dt_tree = self._make_tree(self.dt_card,
            columns=("Field", "Value"), widths=(180, 200))

        # Capabilities card
        self.caps_card = self._make_card(tab, "Services & Capabilities", 1, 0, colspan=2)
        self.caps_tree = self._make_tree(self.caps_card,
            columns=("Service", "Endpoint"), widths=(120, 500))

        # Scopes card
        self.scopes_card = self._make_card(tab, "ONVIF Scopes", 2, 0, colspan=2)
        self.scopes_tree = self._make_tree(self.scopes_card,
            columns=("Scope",), widths=(700,))

    def _build_media_tab(self):
        tab = self.tabview.tab("Media")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.profiles_card = self._make_card(tab, "Media Profiles", 0, 0, colspan=1)
        self.profiles_tree = self._make_tree(self.profiles_card,
            columns=("Name","Token","Codec","Resolution","FPS","Bitrate","Quality"),
            widths=(140,120,70,110,50,90,60))

        self.sources_card = self._make_card(tab, "Video Sources", 1, 0, colspan=1)
        self.sources_tree = self._make_tree(self.sources_card,
            columns=("Token","Resolution","Framerate"), widths=(140,120,90))

    def _build_streams_tab(self):
        tab = self.tabview.tab("Streams")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        self.streams_card = self._make_card(tab, "RTSP Stream URIs", 0, 0)
        self.streams_tree = self._make_tree(self.streams_card,
            columns=("Profile","RTSP URI (with credentials)"),
            widths=(160, 700))

        note = ctk.CTkLabel(self.streams_card,
            text="💡  Double-click a URI to copy it  ·  Open in VLC → Media → Open Network Stream",
            font=("Inter", 11), text_color=TEXT_DIM, anchor="w")
        note.grid(row=2, column=0, sticky="ew", padx=12, pady=(0,8))

    def _build_events_tab(self):
        tab = self.tabview.tab("Events")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(tab, fg_color=PANEL, corner_radius=8)
        card.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        # Toolbar
        toolbar = ctk.CTkFrame(card, fg_color="transparent", height=40)
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8,0))
        toolbar.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(toolbar, text="Live Events", font=FONT_TITLE,
                     text_color=TEXT).grid(row=0, column=0, padx=(4,12))

        self.event_count_label = ctk.CTkLabel(toolbar, text="0 events",
            font=("Inter",10), text_color=TEXT_DIM)
        self.event_count_label.grid(row=0, column=1, padx=4)

        self.event_status_label = ctk.CTkLabel(toolbar, text="●  Not connected",
            font=("Inter",10), text_color=TEXT_DIM)
        self.event_status_label.grid(row=0, column=2, padx=12)

        clear_btn = ctk.CTkButton(toolbar, text="Clear", width=70, height=26,
                                   font=("Inter",11), fg_color="#374151",
                                   hover_color="#4b5563", corner_radius=5,
                                   command=self._clear_events)
        clear_btn.grid(row=0, column=4, padx=4)

        # Filter
        self.event_filter = ctk.CTkEntry(toolbar, placeholder_text="Filter by topic...",
                                          width=200, height=28, font=("Inter",11),
                                          fg_color="#1a1d23", border_color=BORDER,
                                          text_color=TEXT)
        self.event_filter.grid(row=0, column=5, padx=4)
        self.event_filter.bind("<KeyRelease>", lambda e: self._apply_event_filter())

        # Event tree
        tree_frame = ctk.CTkFrame(card, fg_color="transparent")
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Events.Treeview",
            background=PANEL, foreground=TEXT, fieldbackground=PANEL,
            rowheight=24, font=("Inter", 11), borderwidth=0)
        style.configure("Events.Treeview.Heading",
            background=HEADER_BG, foreground=TEXT_DIM, relief="flat",
            font=("Inter", 10, "bold"), borderwidth=0)
        style.map("Events.Treeview", background=[("selected", ACCENT2)])
        style.layout("Events.Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])

        self.event_tree = ttk.Treeview(tree_frame, style="Events.Treeview",
            columns=("time","topic","source","data"), show="headings",
            selectmode="browse")
        self.event_tree.heading("time",   text="Time (UTC)")
        self.event_tree.heading("topic",  text="Topic")
        self.event_tree.heading("source", text="Source")
        self.event_tree.heading("data",   text="Data")
        self.event_tree.column("time",   width=90,  stretch=False)
        self.event_tree.column("topic",  width=260, stretch=True)
        self.event_tree.column("source", width=200, stretch=True)
        self.event_tree.column("data",   width=200, stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.event_tree.yview)
        self.event_tree.configure(yscrollcommand=vsb.set)
        self.event_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.event_tree.tag_configure("motion",  foreground="#f59e0b")
        self.event_tree.tag_configure("alarm",   foreground="#ef4444")
        self.event_tree.tag_configure("connect", foreground="#22c55e")
        self.event_tree.tag_configure("normal",  foreground=TEXT)
        self.event_tree.tag_configure("alt",     background=ROW_ALT)

        self._all_events = []
        self._event_row_count = 0

    def _build_network_tab(self):
        tab = self.tabview.tab("Network")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        self.net_card = self._make_card(tab, "Network Interfaces", 0, 0)
        self.net_tree = self._make_tree(self.net_card,
            columns=("Name","IP","MAC","Prefix"), widths=(80,150,160,80))

        self.ntp_card = self._make_card(tab, "NTP", 1, 0)
        self.ntp_tree = self._make_tree(self.ntp_card,
            columns=("Field","Value"), widths=(120,300))

    def _build_users_tab(self):
        tab = self.tabview.tab("Users")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        self.users_card = self._make_card(tab, "ONVIF Users", 0, 0)
        self.users_tree = self._make_tree(self.users_card,
            columns=("Username","Level"), widths=(200,200))

    def _make_card(self, parent, title, row, col, colspan=1):
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=8)
        card.grid(row=row, column=col, columnspan=colspan,
                  sticky="nsew", padx=8, pady=8)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(card, text=title, font=FONT_TITLE,
                     text_color=TEXT, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10,4))
        return card

    def _make_tree(self, parent, columns, widths):
        style = ttk.Style()
        style.theme_use("clam")
        sid = "T" + str(id(parent))
        style.configure(f"{sid}.Treeview",
            background=PANEL, foreground=TEXT, fieldbackground=PANEL,
            rowheight=26, font=("Inter", 11), borderwidth=0)
        style.configure(f"{sid}.Treeview.Heading",
            background=HEADER_BG, foreground=TEXT_DIM, relief="flat",
            font=("Inter", 10, "bold"))
        style.map(f"{sid}.Treeview", background=[("selected", ACCENT2)])
        style.layout(f"{sid}.Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])

        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0,8))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        tree = ttk.Treeview(frame, style=f"{sid}.Treeview",
                             columns=columns, show="headings", selectmode="browse")
        for col, w in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=w, stretch=(w > 200))

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        tree.tag_configure("alt", background=ROW_ALT)
        return tree

    # ── Device list UI ─────────────────────────────────────────────────────────

    def _refresh_device_list(self):
        for w in self.device_list_frame.winfo_children():
            w.destroy()
        for i, (ip, dev) in enumerate(self.devices.items()):
            self._make_device_row(ip, dev, i)

    def _make_device_row(self, ip, dev, idx):
        is_sel = (ip == self.selected_ip)
        bg = ACCENT2 if is_sel else "transparent"
        row = ctk.CTkFrame(self.device_list_frame, fg_color=bg, corner_radius=6, height=58)
        row.grid(row=idx, column=0, sticky="ew", padx=6, pady=2)
        row.grid_propagate(False)
        row.grid_columnconfigure(1, weight=1)

        status = dev.get("status", "disconnected")
        dot_color = SUCCESS if status == "connected" else (WARNING if status == "connecting" else DANGER)

        dot = ctk.CTkLabel(row, text="●", font=("Inter",12), text_color=dot_color, width=20)
        dot.grid(row=0, column=0, rowspan=2, padx=(10,4), pady=4)

        name = dev.get("name", ip)
        ctk.CTkLabel(row, text=name, font=("Inter",12,"bold"),
                     text_color=TEXT, anchor="w").grid(row=0, column=1, sticky="w", pady=(6,0))
        ctk.CTkLabel(row, text=f"{ip}  ·  {status}", font=("Inter",10),
                     text_color=TEXT_DIM, anchor="w").grid(row=1, column=1, sticky="w")

        for widget in [row, dot]:
            widget.bind("<Button-1>", lambda e, i=ip: self._select_device(i))

        # Buttons appear on hover
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=4)
        if status != "connected":
            ctk.CTkButton(btns, text="Connect", width=70, height=24, font=("Inter",10),
                           fg_color=ACCENT, hover_color=ACCENT2, corner_radius=4,
                           command=lambda i=ip: self._connect_device(i)).pack(pady=2)
        ctk.CTkButton(btns, text="Remove", width=70, height=24, font=("Inter",10),
                       fg_color="#374151", hover_color=DANGER, corner_radius=4,
                       command=lambda i=ip: self._remove_device(i)).pack(pady=2)

    # ── Device actions ─────────────────────────────────────────────────────────

    def _discover(self):
        self.sidebar_status.configure(text="Discovering...", text_color=WARNING)
        def run():
            found = discover_devices(timeout=3)
            self.after(0, lambda: self._on_discovered(found))
        threading.Thread(target=run, daemon=True).start()

    def _on_discovered(self, found):
        added = 0
        for dev in found:
            ip = dev['ip']
            if ip not in self.devices:
                self.devices[ip] = {
                    "ip": ip, "port": 80, "user": "admin", "password": "",
                    "name": ip, "status": "disconnected",
                    "address": dev.get("address", f"http://{ip}/onvif/device_service")
                }
                added += 1
        self.sidebar_status.configure(
            text=f"Found {len(found)} device(s), {added} new",
            text_color=SUCCESS if found else TEXT_DIM)
        self._refresh_device_list()

    def _show_add_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Add Device")
        dialog.geometry("400x320")
        dialog.configure(fg_color=PANEL)
        dialog.grab_set()

        fields = {}
        defaults = [("Host / IP", "192.168.1."), ("Port", "80"),
                    ("Username", "admin"), ("Password", "")]
        for i, (label, default) in enumerate(defaults):
            ctk.CTkLabel(dialog, text=label, font=("Inter",12),
                         text_color=TEXT_DIM).grid(row=i, column=0, padx=20, pady=8, sticky="w")
            show = "*" if label == "Password" else ""
            e = ctk.CTkEntry(dialog, width=220, font=("Inter",12),
                              fg_color=BG, border_color=BORDER, text_color=TEXT,
                              show=show)
            e.insert(0, default)
            e.grid(row=i, column=1, padx=12, pady=8)
            fields[label] = e

        def add():
            host = fields["Host / IP"].get().strip()
            port = fields["Port"].get().strip() or "80"
            user = fields["Username"].get().strip()
            pwd  = fields["Password"].get()
            if not host: return
            self.devices[host] = {
                "ip": host, "port": int(port), "user": user, "password": pwd,
                "name": host, "status": "disconnected"
            }
            self._refresh_device_list()
            dialog.destroy()
            self._connect_device(host)

        ctk.CTkButton(dialog, text="Connect", command=add, fg_color=ACCENT,
                       hover_color=ACCENT2, font=("Inter",12), height=36).grid(
            row=len(defaults), column=0, columnspan=2, padx=20, pady=16, sticky="ew")

    def _remove_device(self, ip):
        if self.selected_ip == ip:
            self.selected_ip = None
            self._show_welcome()
        self.devices.pop(ip, None)
        self._refresh_device_list()

    def _select_device(self, ip):
        self.selected_ip = ip
        self._refresh_device_list()
        dev = self.devices.get(ip, {})
        if dev.get("status") == "connected":
            self._populate_all_tabs(ip)
        else:
            self._connect_device(ip)

    def _connect_device(self, ip):
        dev = self.devices.get(ip)
        if not dev: return

        # If password missing, prompt
        if not dev.get("password"):
            self._prompt_password(ip)
            return

        dev["status"] = "connecting"
        self.selected_ip = ip
        self._set_top_status(dev.get("name", ip), "Connecting...", WARNING)
        self._refresh_device_list()

        def run():
            try:
                client = ONVIFClient(dev["ip"], dev.get("port",80),
                                      dev["user"], dev["password"])
                info = client.get_device_info()
                name = f"{info.get('Manufacturer','')} {info.get('Model','')}".strip() or ip
                dev.update({"client": client, "info": info, "status": "connected", "name": name})
                self.after(0, lambda: self._on_connected(ip))
            except Exception as e:
                dev["status"] = "error"
                dev["error"] = str(e)
                self.after(0, lambda err=str(e): self._on_connect_error(ip, err))
        threading.Thread(target=run, daemon=True).start()

    def _prompt_password(self, ip):
        dev = self.devices[ip]
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Connect to {ip}")
        dialog.geometry("360x240")
        dialog.configure(fg_color=PANEL)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text=f"Credentials for {ip}",
                     font=FONT_TITLE, text_color=TEXT).pack(pady=(20,12))
        user_e = ctk.CTkEntry(dialog, placeholder_text="Username", width=260,
                               fg_color=BG, border_color=BORDER, text_color=TEXT)
        user_e.insert(0, dev.get("user","admin"))
        user_e.pack(pady=6)
        pwd_e = ctk.CTkEntry(dialog, placeholder_text="Password", show="*", width=260,
                              fg_color=BG, border_color=BORDER, text_color=TEXT)
        pwd_e.pack(pady=6)

        def connect():
            dev["user"] = user_e.get().strip()
            dev["password"] = pwd_e.get()
            dialog.destroy()
            self._connect_device(ip)

        ctk.CTkButton(dialog, text="Connect", command=connect, fg_color=ACCENT,
                       hover_color=ACCENT2, font=("Inter",12), height=36, width=260).pack(pady=14)
        pwd_e.bind("<Return>", lambda e: connect())

    def _on_connected(self, ip):
        dev = self.devices[ip]
        self._set_top_status(dev["name"], "Connected", SUCCESS)
        self._refresh_device_list()
        if self.selected_ip == ip:
            self._populate_all_tabs(ip)
        self._start_event_stream(ip)

    def _on_connect_error(self, ip, err):
        self._set_top_status(self.devices[ip].get("name", ip), f"Error: {err}", DANGER)
        self._refresh_device_list()

    def _set_top_status(self, name, status, color):
        self.device_label.configure(text=name)
        self.status_dot.configure(text_color=color)
        self.status_label.configure(text=status)

    # ── Tab population ─────────────────────────────────────────────────────────

    def _populate_all_tabs(self, ip):
        dev = self.devices.get(ip, {})
        client: ONVIFClient = dev.get("client")
        if not client: return

        def run():
            results = {}
            try: results['caps'] = client.get_capabilities()
            except Exception as e: results['caps_err'] = str(e)

            try: results['dt'] = client.get_system_date_time()
            except Exception as e: results['dt_err'] = str(e)

            try: results['hostname'] = client.get_hostname()
            except Exception: results['hostname'] = ""

            try: results['net'] = client.get_network_interfaces()
            except Exception as e: results['net'] = []

            try: results['ntp'] = client.get_ntp()
            except Exception: results['ntp'] = {}

            try: results['profiles'] = client.get_profiles()
            except Exception as e: results['profiles'] = []

            try: results['sources'] = client.get_video_sources()
            except Exception: results['sources'] = []

            try: results['users'] = client.get_users()
            except Exception: results['users'] = []

            try: results['scopes'] = client.get_scopes()
            except Exception: results['scopes'] = []

            # Stream URIs per profile
            uris = []
            for p in results.get('profiles', []):
                try:
                    uri = client.get_stream_uri(p['Token'])
                    uri_creds = uri.replace("rtsp://", f"rtsp://{client.user}:{client.password}@")
                    uris.append({'Profile': p['Name'], 'URI': uri_creds})
                except Exception as e:
                    uris.append({'Profile': p.get('Name','?'), 'URI': f'(error: {e})'})
            results['uris'] = uris

            self.after(0, lambda: self._render_all_tabs(ip, results))

        threading.Thread(target=run, daemon=True).start()

    def _render_all_tabs(self, ip, r):
        dev = self.devices.get(ip, {})
        info = dev.get('info', {})

        # Info tab
        self._clear_tree(self.info_tree)
        rows = [(k, v) for k, v in info.items()]
        if r.get('hostname'): rows.append(("Hostname", r['hostname']))
        rows.append(("ONVIF Port", str(dev.get('port', 80))))
        for i, (k, v) in enumerate(rows):
            tags = ("alt",) if i % 2 else ()
            self.info_tree.insert("", "end", values=(k, v), tags=tags)

        self._clear_tree(self.dt_tree)
        for i, (k, v) in enumerate(r.get('dt', {}).items()):
            self.dt_tree.insert("", "end", values=(k, v), tags=(("alt",) if i%2 else ()))

        self._clear_tree(self.caps_tree)
        for i, (k, v) in enumerate(r.get('caps', {}).items()):
            self.caps_tree.insert("", "end", values=(k, v), tags=(("alt",) if i%2 else ()))

        self._clear_tree(self.scopes_tree)
        for i, s in enumerate(r.get('scopes', [])):
            self.scopes_tree.insert("", "end", values=(s,), tags=(("alt",) if i%2 else ()))

        # Media tab
        self._clear_tree(self.profiles_tree)
        for i, p in enumerate(r.get('profiles', [])):
            self.profiles_tree.insert("", "end", values=(
                p.get('Name',''), p.get('Token',''), p.get('Codec',''),
                p.get('Resolution',''), p.get('FPS',''),
                p.get('Bitrate',''), p.get('Quality','')
            ), tags=(("alt",) if i%2 else ()))

        self._clear_tree(self.sources_tree)
        for i, s in enumerate(r.get('sources', [])):
            self.sources_tree.insert("", "end", values=(
                s.get('Token',''), s.get('Resolution',''), s.get('Framerate','')
            ), tags=(("alt",) if i%2 else ()))

        # Streams tab
        self._clear_tree(self.streams_tree)
        for i, u in enumerate(r.get('uris', [])):
            self.streams_tree.insert("", "end", values=(u['Profile'], u['URI']),
                                      tags=(("alt",) if i%2 else ()))
        self.streams_tree.bind("<Double-1>", self._copy_stream_uri)

        # Network tab
        self._clear_tree(self.net_tree)
        for i, iface in enumerate(r.get('net', [])):
            self.net_tree.insert("", "end", values=(
                iface.get('Name',''), iface.get('IP',''),
                iface.get('MAC',''), iface.get('Prefix','')
            ), tags=(("alt",) if i%2 else ()))

        self._clear_tree(self.ntp_tree)
        ntp = r.get('ntp', {})
        if ntp:
            self.ntp_tree.insert("", "end", values=("From DHCP", ntp.get('FromDHCP','')))
            for i, s in enumerate(ntp.get('Servers', [])):
                self.ntp_tree.insert("", "end", values=(f"Server {i+1}", s),
                                      tags=(("alt",) if i%2 else ()))

        # Users tab
        self._clear_tree(self.users_tree)
        for i, u in enumerate(r.get('users', [])):
            self.users_tree.insert("", "end", values=(u.get('Username',''), u.get('Level','')),
                                   tags=(("alt",) if i%2 else ()))

    def _copy_stream_uri(self, event):
        tree = self.streams_tree
        sel = tree.selection()
        if not sel: return
        uri = tree.item(sel[0])['values'][1]
        self.clipboard_clear()
        self.clipboard_append(uri)
        self._set_top_status(self.device_label.cget("text"), "URI copied to clipboard ✓", SUCCESS)
        self.after(2000, lambda: self._set_top_status(
            self.device_label.cget("text"), "Connected", SUCCESS))

    def _clear_tree(self, tree):
        for item in tree.get_children():
            tree.delete(item)

    # ── Events ─────────────────────────────────────────────────────────────────

    def _start_event_stream(self, ip):
        self.stop_events.set()
        time.sleep(0.1)
        self.stop_events.clear()

        dev = self.devices.get(ip, {})
        client: ONVIFClient = dev.get("client")
        if not client: return

        def run():
            self.after(0, lambda: self.event_status_label.configure(
                text="●  Subscribing...", text_color=WARNING))
            try:
                sub_url = client.create_pullpoint_subscription()
                self.after(0, lambda: self.event_status_label.configure(
                    text="●  Live", text_color=SUCCESS))
                while not self.stop_events.is_set():
                    try:
                        msgs, err = client.pull_messages(sub_url)
                        if err == "expired":
                            sub_url = client.create_pullpoint_subscription()
                            continue
                        for msg in (msgs or []):
                            parsed = parse_event(msg)
                            if parsed:
                                self.event_queue.put(parsed)
                    except requests.exceptions.Timeout:
                        continue
                    except Exception as e:
                        if not self.stop_events.is_set():
                            self.after(0, lambda e=e: self.event_status_label.configure(
                                text=f"●  Reconnecting...", text_color=WARNING))
                            time.sleep(3)
                            try: sub_url = client.create_pullpoint_subscription()
                            except Exception: time.sleep(5)
            except Exception as e:
                self.after(0, lambda: self.event_status_label.configure(
                    text=f"●  Events unavailable", text_color=DANGER))

        self.event_thread = threading.Thread(target=run, daemon=True)
        self.event_thread.start()

    def _start_event_drain(self):
        def drain():
            batch = []
            while True:
                try:
                    ev = self.event_queue.get_nowait()
                    batch.append(ev)
                except Empty:
                    break
            if batch:
                self.after(0, lambda b=batch: self._add_events(b))
            self.after(200, drain)
        self.after(200, drain)

    def _add_events(self, batch):
        filter_text = self.event_filter.get().lower().strip()
        for ev in batch:
            self._all_events.append(ev)
            if len(self._all_events) > 2000:
                self._all_events.pop(0)

            if filter_text and filter_text not in ev['topic'].lower():
                continue

            src_str  = "  ".join(f"{k}={v}" for k,v in ev['source'].items())
            data_str = "  ".join(f"{k}={v}" for k,v in ev['data'].items())

            # Color coding
            topic_lower = ev['topic'].lower()
            if any(w in topic_lower for w in ['motion','alarm','tamper','intrusion','line']):
                tag = "alarm" if 'alarm' in topic_lower or 'tamper' in topic_lower else "motion"
            elif any(w in topic_lower for w in ['connect','disconnect','login']):
                tag = "connect"
            else:
                tag = "alt" if self._event_row_count % 2 else "normal"

            self._event_row_count += 1
            self.event_tree.insert("", 0,  # insert at top
                values=(ev['time'], ev['topic'], src_str, data_str), tags=(tag,))

            # Cap tree at 500 rows
            children = self.event_tree.get_children()
            if len(children) > 500:
                self.event_tree.delete(children[-1])

        self.event_count_label.configure(text=f"{len(self._all_events)} events")

    def _apply_event_filter(self):
        for item in self.event_tree.get_children():
            self.event_tree.delete(item)
        filter_text = self.event_filter.get().lower().strip()
        shown = [e for e in reversed(self._all_events)
                 if not filter_text or filter_text in e['topic'].lower()]
        for ev in shown[:500]:
            src_str  = "  ".join(f"{k}={v}" for k,v in ev['source'].items())
            data_str = "  ".join(f"{k}={v}" for k,v in ev['data'].items())
            self.event_tree.insert("", "end", values=(ev['time'], ev['topic'], src_str, data_str))

    def _clear_events(self):
        self._all_events.clear()
        self._event_row_count = 0
        self._clear_tree(self.event_tree)
        self.event_count_label.configure(text="0 events")

    # ── Welcome screen ─────────────────────────────────────────────────────────

    def _show_welcome(self):
        self.device_label.configure(text="No device selected")
        self.status_dot.configure(text_color=TEXT_DIM)
        self.status_label.configure(text="")

    def _show_welcome(self):
        self.device_label.configure(text="ONVIF Device Manager")
        self.status_dot.configure(text="", text_color=TEXT_DIM)
        self.status_label.configure(text="Discover or add a device to begin")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    app = ONVIFManagerApp()
    app.mainloop()
