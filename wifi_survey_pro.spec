# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NetInspect Pro
import os, sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# ── Collect packages that need full collection ─────────────────────────────
webview_datas, webview_binaries, webview_hiddens = collect_all("webview")
flask_datas,   flask_binaries,   flask_hiddens   = collect_all("flask")
wz_datas,      wz_binaries,      wz_hiddens      = collect_all("werkzeug")
jinja_datas,   jinja_binaries,   jinja_hiddens   = collect_all("jinja2")
rl_datas,      rl_binaries,      rl_hiddens      = collect_all("reportlab")
mac_datas,     mac_binaries,     mac_hiddens      = collect_all("mac_vendor_lookup")

# ── Source files to bundle ─────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=webview_binaries + wz_binaries,
    datas=[
        # Frontend static files
        ("frontend", "frontend"),
        # Collected package data
        *webview_datas,
        *flask_datas,
        *wz_datas,
        *jinja_datas,
        *rl_datas,
        *mac_datas,
    ],
    hiddenimports=[
        # PyObjC / CoreWLAN
        "objc", "Foundation", "CoreWLAN",
        "PyObjCTools", "PyObjCTools.Conversion",
        # pywebview
        "webview", "webview.platforms", "webview.platforms.cocoa",
        *webview_hiddens,
        # Flask stack
        "flask", "flask.json", "flask.templating",
        *flask_hiddens,
        *wz_hiddens,
        *jinja_hiddens,
        # ReportLab
        *rl_hiddens,
        # Vendor lookup
        *mac_hiddens,
        # Network tools
        "ping3", "dns", "dns.resolver", "dns.name", "dns.rdataclass",
        # Scipy / numpy
        "scipy", "scipy.spatial", "scipy.interpolate",
        "numpy", "numpy.core",
        # PIL
        "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
        # Local app modules
        "scanner", "analyzer", "heatmap_gen", "reporter",
        "recommender", "iperf3_mgr",
        # stdlib misc
        "encodings.utf_8", "encodings.ascii",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "test"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NetInspect Pro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NetInspect Pro",
)

app = BUNDLE(
    coll,
    name="NetInspect Pro.app",
    icon=None,                  # add .icns path here if you have one
    bundle_identifier="tw.zonetech.wifi-survey-pro",
    version="2.1.0",
    info_plist={
        "CFBundleDisplayName":       "NetInspect Pro",
        "CFBundleShortVersionString": "2.1",
        "NSHighResolutionCapable":   True,
        "NSRequiresAquaSystemAppearance": False,   # supports dark mode
        # Location permission for CoreWLAN BSSID/SSID access
        "NSLocationWhenInUseUsageDescription":
            "NetInspect Pro needs Location Services to display SSID and BSSID information.",
        "NSLocationAlwaysUsageDescription":
            "NetInspect Pro needs Location Services to display SSID and BSSID information.",
    },
)
