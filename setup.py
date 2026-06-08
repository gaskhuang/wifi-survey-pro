"""
py2app setup for Wi-Fi Survey Pro
Build: python setup.py py2app
"""
from setuptools import setup

APP        = ["main.py"]
APP_NAME   = "Wi-Fi Survey Pro"
IDENTIFIER = "tw.zonetech.wifi-survey-pro"
VERSION    = "2.1.0"

OPTIONS = {
    "iconfile": None,           # 替換成 .icns 路徑（有的話）
    "plist": {
        "CFBundleDisplayName":           APP_NAME,
        "CFBundleName":                  APP_NAME,
        "CFBundleIdentifier":            IDENTIFIER,
        "CFBundleShortVersionString":    VERSION,
        "CFBundleVersion":               VERSION,
        "NSHighResolutionCapable":       True,
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion":        "12.0",
        # Location Services permission for CoreWLAN BSSID/SSID
        "NSLocationWhenInUseUsageDescription":
            "Wi-Fi Survey Pro 需要定位服務以顯示 SSID 和 BSSID 資訊。",
        "NSLocationAlwaysUsageDescription":
            "Wi-Fi Survey Pro 需要定位服務以顯示 SSID 和 BSSID 資訊。",
    },
    "packages": [
        "flask", "werkzeug", "jinja2", "click", "itsdangerous",
        "PIL", "numpy", "scipy", "reportlab",
        "dns", "ping3", "mac_vendor_lookup",
        "webview",
        "objc", "Foundation", "CoreWLAN",
    ],
    "includes": [
        "scanner", "analyzer", "heatmap_gen", "reporter",
        "recommender", "iperf3_mgr",
        "webview.platforms.cocoa",
        "CoreWLAN",
        "dns.resolver", "dns.name",
        "scipy.spatial", "scipy.interpolate",
        "encodings.utf_8",
    ],
    "excludes": ["tkinter", "matplotlib", "IPython", "test"],
    # Bundle the frontend static files
    "resources": ["frontend"],
    "semi_standalone": False,
    "site_packages": True,
    "strip": False,
    "argv_emulation": False,   # must be False for pywebview
    "emulate_shell_environment": True,
}

setup(
    name=APP_NAME,
    version=VERSION,
    app=APP,
    data_files=[],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
