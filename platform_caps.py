"""
platform_caps.py — 跨平台能力與權限探測（NetInspect Pro）
統一 macOS / Windows / Linux 的平台判斷，供 LAN 掃描、流量監測等模組查詢
目前執行環境能做到什麼（是否有系統管理權限、是否可抓封包）。
"""
import os
import sys
import shutil
import subprocess


def os_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def is_windows() -> bool:
    return os_name() == "windows"


def is_macos() -> bool:
    return os_name() == "macos"


def is_admin() -> bool:
    """是否以系統管理員 / root 身分執行。"""
    try:
        if is_windows():
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def has_pcap() -> bool:
    """是否具備封包擷取後端（Windows: Npcap；macOS/Linux: BPF/libpcap）。
    只做保守偵測，實際抓包能力仍受權限影響。"""
    try:
        if is_windows():
            # Npcap 會安裝 wpcap.dll / npcap 服務
            paths = [
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "Npcap", "wpcap.dll"),
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "wpcap.dll"),
            ]
            return any(os.path.exists(p) for p in paths)
        if is_macos():
            # macOS 內建 BPF 裝置節點
            return os.path.exists("/dev/bpf0")
        return os.path.exists("/usr/sbin/tcpdump") or shutil.which("tcpdump") is not None
    except Exception:
        return False


def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def capabilities() -> dict:
    """回傳目前環境能力總覽，供前端顯示與功能降級判斷。"""
    admin = is_admin()
    pcap  = has_pcap()
    osn   = os_name()

    # LAN ARP 掃描：ping sweep + arp 表法不需權限（degraded 完整可用）；
    # 原生 ARP 送包（未來 scapy）才需要 admin。
    lan_scan = "full"          # ping-sweep + arp-table 免權限即可用
    # 流量監測：需要 admin，Windows 另需 Npcap
    if is_windows():
        traffic = "full" if (admin and pcap) else ("needs_npcap" if not pcap else "needs_admin")
    else:
        traffic = "full" if admin else "needs_admin"

    return {
        "os":            osn,
        "os_label":      {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(osn, osn),
        "is_admin":      admin,
        "has_pcap":      pcap,
        "lan_scan":      lan_scan,
        "traffic":       traffic,
        "hints": {
            "traffic": (
                "流量監測需系統管理員權限"
                + ("，且需安裝 Npcap" if is_windows() and not pcap else "")
                if traffic != "full" else ""
            ),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(capabilities(), ensure_ascii=False, indent=2))
