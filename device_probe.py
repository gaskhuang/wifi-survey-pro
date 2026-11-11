"""
device_probe.py — 設備辨識引擎（NetInspect Pro）

ping / ARP / 埠掃描只能知道「有一台設備、開了哪些埠」。這個模組再用設備自己會回答的
協定，問出它的名稱、型號、作業系統：

  · mDNS / Bonjour ：Apple 裝置、印表機、電視、喇叭、NAS、IoT 的名稱、服務、型號（TXT）
  · NetBIOS        ：Windows 電腦 / NAS 的電腦名稱與工作群組
  · SSDP / UPnP    ：路由器、電視、NAS、IP Cam 的廠牌、型號、序號
  · SNMP (public)  ：交換器、AP、印表機的 sysName / sysDescr / sysLocation
  · HTTP(S) 首頁   ：管理介面的標題與 Server 標頭
  · SSH / FTP 橫幅 ：作業系統與韌體線索

全部只用標準庫。每種 UDP 探測共用一個 socket「全部送出、統一收回」，
整個網段多花約 3～4 秒。只探測呼叫端給的 IP（掃描網段內的主機），
UPnP 描述檔只向回應者本身的位址抓取，並限制大小與時間。
"""
import ipaddress
import re
import select
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ════════════════════════════════════════════════════════════════════════════
#  DNS 封包（mDNS 用）— 自行解析：mDNS 的 class 帶 cache-flush 位元，一般 DNS 函式庫會誤判
# ════════════════════════════════════════════════════════════════════════════
T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33
_MDNS_ADDR = ("224.0.0.251", 5353)
_SVC_ENUM = "_services._dns-sd._udp.local"

# 主動詢問的常見服務（設備不一定回應 _services 列舉，直接問命中率較高）
_COMMON_SERVICES = [
    "_device-info._tcp", "_companion-link._tcp", "_airplay._tcp", "_raop._tcp",
    "_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp", "_scanner._tcp",
    "_uscan._tcp", "_smb._tcp", "_afpovertcp._tcp", "_adisk._tcp", "_ssh._tcp",
    "_sftp-ssh._tcp", "_rfb._tcp", "_http._tcp", "_https._tcp", "_workstation._tcp",
    "_googlecast._tcp", "_hap._tcp", "_homekit._tcp", "_matter._tcp", "_sonos._tcp",
    "_spotify-connect._tcp", "_mediaremotetv._tcp", "_amzn-wplay._tcp", "_hue._tcp",
    "_axis-video._tcp", "_qdiscover._tcp", "_nvstream._tcp", "_sleep-proxy._udp",
    "_touch-able._tcp", "_daap._tcp", "_miio._udp", "_esphomelib._tcp",
]

# 服務 → 中文說明（設備詳情顯示用）
SERVICE_LABEL = {
    "_airplay._tcp": "AirPlay", "_raop._tcp": "AirPlay 音訊", "_ipp._tcp": "IPP 列印",
    "_ipps._tcp": "IPP 列印 (TLS)", "_printer._tcp": "LPD 列印", "_pdl-datastream._tcp": "RAW 列印",
    "_scanner._tcp": "掃描器", "_uscan._tcp": "掃描器 (eSCL)", "_smb._tcp": "SMB 檔案分享",
    "_afpovertcp._tcp": "AFP 檔案分享", "_adisk._tcp": "Time Machine", "_ssh._tcp": "SSH",
    "_sftp-ssh._tcp": "SFTP", "_rfb._tcp": "螢幕共享 (VNC)", "_http._tcp": "網頁介面",
    "_https._tcp": "網頁介面 (TLS)", "_workstation._tcp": "工作站", "_googlecast._tcp": "Google Cast",
    "_hap._tcp": "HomeKit", "_homekit._tcp": "HomeKit", "_matter._tcp": "Matter",
    "_sonos._tcp": "Sonos", "_spotify-connect._tcp": "Spotify Connect",
    "_mediaremotetv._tcp": "Apple TV 遙控", "_companion-link._tcp": "Apple 接續互通",
    "_amzn-wplay._tcp": "Amazon Fire TV", "_hue._tcp": "Philips Hue", "_axis-video._tcp": "Axis 攝影機",
    "_qdiscover._tcp": "QNAP", "_nvstream._tcp": "NVIDIA GameStream", "_sleep-proxy._udp": "Sleep Proxy",
    "_touch-able._tcp": "Apple 遙控", "_daap._tcp": "iTunes 分享", "_miio._udp": "小米 IoT",
    "_esphomelib._tcp": "ESPHome", "_device-info._tcp": "裝置資訊",
}


def _dns_name(buf: bytes, off: int, depth: int = 0):
    """解析（可能壓縮的）DNS 名稱 → (名稱, 下一個位移)。"""
    labels, jumped, end = [], False, off
    for _ in range(64):
        if off >= len(buf):
            raise ValueError("truncated")
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(buf) or depth > 10:
                raise ValueError("bad pointer")
            ptr = ((n & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                end = off + 2
            off, jumped, depth = ptr, True, depth + 1
            continue
        off += 1
        labels.append(buf[off:off + n].decode("utf-8", "replace"))
        off += n
    return ".".join(labels), (end if jumped else off)


def _dns_parse(buf: bytes) -> list:
    """回傳所有資源紀錄 [(name, type, rdata)]；rdata 依型別解成字串 / tuple / dict。"""
    out = []
    try:
        _id, _fl, qd, an, ns, ar = struct.unpack("!6H", buf[:12])
        off = 12
        for _ in range(qd):
            _, off = _dns_name(buf, off)
            off += 4
        for _ in range(min(an + ns + ar, 200)):
            name, off = _dns_name(buf, off)
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", buf[off:off + 10])
            off += 10
            rd = buf[off:off + rdlen]
            if rtype == T_A and rdlen == 4:
                out.append((name, T_A, socket.inet_ntoa(rd)))
            elif rtype == T_PTR:
                out.append((name, T_PTR, _dns_name(buf, off)[0]))
            elif rtype == T_SRV and rdlen >= 7:
                out.append((name, T_SRV, (struct.unpack("!H", rd[4:6])[0], _dns_name(buf, off + 6)[0])))
            elif rtype == T_TXT:
                txt, i = {}, 0
                while i < len(rd):
                    n = rd[i]
                    kv = rd[i + 1:i + 1 + n].decode("utf-8", "replace")
                    i += 1 + n
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        txt[k.lower()[:40]] = v[:200]
                out.append((name, T_TXT, txt))
            off += rdlen
    except (ValueError, struct.error, IndexError):
        pass
    return out


def _dns_query(questions) -> bytes:
    """questions: [(name, type)] → mDNS 查詢封包。"""
    pkt = struct.pack("!6H", 0, 0, len(questions), 0, 0, 0)
    for name, qtype in questions:
        for label in name.rstrip(".").split("."):
            raw = label.encode("utf-8")[:63]
            pkt += bytes([len(raw)]) + raw
        pkt += b"\x00" + struct.pack("!HH", qtype, 1)
    return pkt


def _udp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except OSError:
        pass
    return s


def _send(s, data: bytes, addr) -> None:
    for _ in range(3):
        try:
            s.sendto(data, addr)
            return
        except BlockingIOError:
            time.sleep(0.002)
        except OSError:
            return


def _collect(s, deadline: float, on_packet, should_stop=None) -> None:
    """收封包直到期限；on_packet(data, src_ip)。"""
    while True:
        left = deadline - time.time()
        if left <= 0 or (should_stop and should_stop()):
            return
        r, _, _ = select.select([s], [], [], min(0.1, left))
        if not r:
            continue
        while True:
            try:
                data, addr = s.recvfrom(9000)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                return
            try:
                on_packet(data, addr[0])
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
#  mDNS / Bonjour
# ════════════════════════════════════════════════════════════════════════════
def probe_mdns(ips, timeout: float = 3.0, should_stop=None) -> dict:
    """回傳 {ip: {"hostname", "names": [..], "services": [..], "txt": {服務: {k: v}}}}。
    從一般埠送出（legacy unicast），設備會直接回覆到我們的埠，不需綁定 5353。
    同時做兩件事：多點傳送問全網、對每台直接詢問（有些設備只回其中一種）。"""
    targets = set(ips)
    info = {}
    asked = set()                 # 已送出的 (ip, 名稱, 型別)，避免重複
    inst_owner = {}               # 服務實例全名 → ip
    host_ip = {}                  # 主機名 → ip（A 紀錄）
    pending_srv = {}              # 實例全名 → SRV 目標主機名（等 A 紀錄對應）
    try:
        s = _udp_socket()
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            pass
    except OSError:
        return {}

    def rec(ip):
        return info.setdefault(ip, {"hostname": "", "names": [], "services": [], "txt": {}})

    def ask(ip, name, qtype):
        key = (ip, name.lower(), qtype)
        if key in asked or len(asked) > 4000:
            return
        asked.add(key)
        _send(s, _dns_query([(name, qtype)]), (ip, 5353))

    def svc_of(full: str) -> str:
        """'Living Room._airplay._tcp.local' → '_airplay._tcp'"""
        m = re.search(r"(_[a-z0-9-]+\._(?:tcp|udp))\.local$", full, re.I)
        return m.group(1).lower() if m else ""

    def on_packet(data, src):
        rrs = _dns_parse(data)
        if not rrs:
            return
        for name, rtype, val in rrs:                      # 先收 A 紀錄，後面歸屬要用
            if rtype == T_A:
                host_ip[name.lower()] = val
        # 封包來源在目標內 → 紀錄歸它；否則（代答、多網卡）看 A 紀錄
        owner = src if src in targets else None
        for name, rtype, val in rrs:
            lname = name.lower()
            if rtype == T_A:
                if val in targets and lname.endswith(".local"):
                    r = rec(val)
                    if not r["hostname"]:
                        r["hostname"] = name
            elif rtype == T_PTR:
                if lname == _SVC_ENUM:                    # 服務列舉 → 再問這個服務有哪些實例
                    if owner:
                        st = svc_of(val) or val.lower().removesuffix(".local")
                        if st and st not in rec(owner)["services"]:
                            rec(owner)["services"].append(st)
                        ask(owner, val, T_PTR)
                elif lname.endswith(".in-addr.arpa"):     # 反查 → 主機名
                    if owner and not rec(owner)["hostname"]:
                        rec(owner)["hostname"] = val
                else:                                     # 服務 → 實例
                    st = svc_of(name)
                    if not st or not owner:
                        continue
                    r = rec(owner)
                    if st not in r["services"]:
                        r["services"].append(st)
                    inst = val[:-len(name) - 1] if val.lower().endswith("." + lname) else ""
                    inst_owner[val.lower()] = owner
                    if inst and inst not in r["names"] and st != "_device-info._tcp":
                        r["names"].append(inst)
                    ask(owner, val, T_TXT)
                    ask(owner, val, T_SRV)
                    if inst:                              # 型號放在 <實例>._device-info._tcp.local
                        ask(owner, f"{inst}._device-info._tcp.local", T_TXT)
            elif rtype == T_SRV:
                pending_srv[lname] = val[1].lower()
                ip = inst_owner.get(lname) or host_ip.get(val[1].lower()) or owner
                if ip in targets:
                    inst_owner[lname] = ip
                    r = rec(ip)
                    if not r["hostname"] and val[1].lower().endswith(".local"):
                        r["hostname"] = val[1]
            elif rtype == T_TXT and val:
                ip = inst_owner.get(lname) or host_ip.get(pending_srv.get(lname, "")) or owner
                st = svc_of(name)
                if ip in targets and st:
                    rec(ip)["txt"].setdefault(st, {}).update(val)

    def blast():
        qs = [(_SVC_ENUM, T_PTR)] + [(f"{t}.local", T_PTR) for t in _COMMON_SERVICES]
        for i in range(0, len(qs), 12):                   # 一包問 12 題，避免超過 MTU
            _send(s, _dns_query(qs[i:i + 12]), _MDNS_ADDR)

    try:
        start = time.time()
        blast()
        for n, ip in enumerate(sorted(targets)):          # 逐台直接問：服務列舉 + 反查主機名
            rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
            _send(s, _dns_query([(_SVC_ENUM, T_PTR), (rev, T_PTR)]), (ip, 5353))
            if n % 32 == 31:
                _collect(s, time.time() + 0.004, on_packet)
        _collect(s, start + min(1.0, timeout / 2), on_packet, should_stop)
        if not (should_stop and should_stop()):
            blast()                                       # 補問一輪（Wi-Fi 上多點傳送容易掉包）
            _collect(s, start + timeout, on_packet, should_stop)
    finally:
        s.close()
    return info


# ════════════════════════════════════════════════════════════════════════════
#  NetBIOS 名稱（Windows / NAS）
# ════════════════════════════════════════════════════════════════════════════
_NBSTAT = (b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
           b"\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00\x00\x21\x00\x01")


def _nbstat_parse(data: bytes):
    """NBSTAT 回應 → {"name", "workgroup", "file_server"}；不是合法回應回 None。"""
    try:
        if len(data) < 57 or not data[2] & 0x80:          # 必須是回應
            return None
        off = 12
        while data[off] != 0:                             # 跳過名稱
            off += data[off] + 1
        off += 1 + 10                                     # 結尾 0 + type/class/ttl/rdlen
        n = data[off]
        off += 1
        r = {"name": "", "workgroup": "", "file_server": False}
        for i in range(n):
            ent = data[off + i * 18: off + i * 18 + 18]
            if len(ent) < 18:
                break
            nm = "".join(c for c in ent[:15].decode("cp437", "replace").strip() if c.isprintable())
            suffix, flags = ent[15], struct.unpack("!H", ent[16:18])[0]
            group = bool(flags & 0x8000)
            if suffix == 0x00 and not group and not r["name"]:
                r["name"] = nm
            elif suffix == 0x00 and group and not r["workgroup"]:
                r["workgroup"] = nm
            elif suffix == 0x20 and not group:
                r["file_server"] = True
        return r if r["name"] else None
    except (IndexError, struct.error):
        return None


def probe_netbios(ips, timeout: float = 1.2, should_stop=None) -> dict:
    """回傳 {ip: {"name", "workgroup", "file_server": bool}}。"""
    targets, out = set(ips), {}

    def on_packet(data, src):
        if src in targets and src not in out:
            r = _nbstat_parse(data)
            if r:
                out[src] = r

    try:
        s = _udp_socket()
    except OSError:
        return {}
    try:
        for rnd in range(2):
            for n, ip in enumerate(ips):
                if ip not in out:
                    _send(s, _NBSTAT, (ip, 137))
                if n % 32 == 31:
                    _collect(s, time.time() + 0.004, on_packet)
            _collect(s, time.time() + timeout / 2, on_packet, should_stop)
    finally:
        s.close()
    return out


# ════════════════════════════════════════════════════════════════════════════
#  SNMP v2c（community: public）— 最小 BER 編解碼
# ════════════════════════════════════════════════════════════════════════════
_SNMP_OIDS = {
    "1.3.6.1.2.1.1.1.0": "descr",
    "1.3.6.1.2.1.1.5.0": "name",
    "1.3.6.1.2.1.1.6.0": "location",
}


def _ber(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 128:
        return bytes([tag, n]) + body
    ln = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(ln)]) + ln + body


def _ber_oid(oid: str) -> bytes:
    p = [int(x) for x in oid.split(".")]
    body = bytes([p[0] * 40 + p[1]])
    for v in p[2:]:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.insert(0, (v & 0x7F) | 0x80)
            v >>= 7
        body += bytes(chunk)
    return _ber(0x06, body)


def _snmp_get() -> bytes:
    binds = b"".join(_ber(0x30, _ber_oid(o) + b"\x05\x00") for o in _SNMP_OIDS)
    pdu = _ber(0xA0, _ber(0x02, b"\x4e\x49") + _ber(0x02, b"\x00") + _ber(0x02, b"\x00") + _ber(0x30, binds))
    return _ber(0x30, _ber(0x02, b"\x01") + _ber(0x04, b"public") + pdu)


def _ber_read(buf: bytes, off: int):
    tag = buf[off]
    n = buf[off + 1]
    off += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[off:off + k], "big")
        off += k
    return tag, buf[off:off + n], off + n


def _oid_str(body: bytes) -> str:
    parts, v = [body[0] // 40, body[0] % 40], 0
    for b in body[1:]:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def _snmp_parse(data: bytes) -> dict:
    out = {}
    try:
        _, msg, _ = _ber_read(data, 0)
        off = 0
        for _ in range(2):                                # version, community
            _, _, off = _ber_read(msg, off)
        tag, pdu, _ = _ber_read(msg, off)
        if tag != 0xA2:                                   # GetResponse
            return out
        off = 0
        for _ in range(3):                                # request-id, error, index
            _, _, off = _ber_read(pdu, off)
        _, binds, _ = _ber_read(pdu, off)
        off = 0
        while off < len(binds):
            _, vb, off = _ber_read(binds, off)
            t1, oid, o2 = _ber_read(vb, 0)
            t2, val, _ = _ber_read(vb, o2)
            key = _SNMP_OIDS.get(_oid_str(oid)) if t1 == 0x06 else None
            if key and t2 == 0x04:
                txt = " ".join(val.decode("utf-8", "replace").split())
                if txt:
                    out[key] = txt[:300]
    except (IndexError, ValueError):
        pass
    return out


def probe_snmp(ips, timeout: float = 1.4, should_stop=None) -> dict:
    """回傳 {ip: {"name", "descr", "location"}}（只有開放 public 的設備會回）。"""
    targets, out = set(ips), {}
    pkt = _snmp_get()

    def on_packet(data, src):
        if src in targets and src not in out:
            r = _snmp_parse(data)
            if r:
                out[src] = r

    try:
        s = _udp_socket()
    except OSError:
        return {}
    try:
        for rnd in range(2):
            for n, ip in enumerate(ips):
                if ip not in out:
                    _send(s, pkt, (ip, 161))
                if n % 32 == 31:
                    _collect(s, time.time() + 0.004, on_packet)
            _collect(s, time.time() + timeout / 2, on_packet, should_stop)
    finally:
        s.close()
    return out


# ════════════════════════════════════════════════════════════════════════════
#  SSDP / UPnP
# ════════════════════════════════════════════════════════════════════════════
_SSDP_MSEARCH = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                 'MAN: "ssdp:discover"\r\nMX: 1\r\nST: {st}\r\n\r\n')
_XML_FIELDS = {"friendlyName": "friendly", "manufacturer": "manufacturer", "modelName": "model",
               "modelNumber": "model_number", "modelDescription": "model_desc",
               "serialNumber": "serial", "deviceType": "device_type"}
_MAX_XML = 96 * 1024


def _http_get_raw(ip: str, port: int, path: str, tls: bool, timeout: float, limit: int) -> bytes:
    """最小 HTTP/1.0 GET：只連指定 IP、不跟隨轉址、限制大小與時間。"""
    deadline = time.time() + timeout
    with socket.create_connection((ip, port), timeout=timeout) as raw:
        s = raw
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)     # 只為了讀橫幅：不驗證憑證
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1    # 舊設備的管理介面常只有舊版 TLS
                ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except (ValueError, ssl.SSLError):
                pass
            s = ctx.wrap_socket(raw, server_hostname=None)
        s.settimeout(max(0.2, deadline - time.time()))
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: NetInspectPro\r\n"
                  f"Accept: */*\r\nConnection: close\r\n\r\n".encode("ascii", "ignore"))
        buf = b""
        while len(buf) < limit and time.time() < deadline:
            try:
                chunk = s.recv(8192)
            except (socket.timeout, ssl.SSLError, OSError):
                break
            if not chunk:
                break
            buf += chunk
        return buf[:limit]


def _decode_html(body: bytes, headers: str) -> str:
    m = re.search(r"charset=[\"']?([\w-]+)", headers, re.I) or \
        re.search(r"charset=[\"']?([\w-]+)", body[:2048].decode("latin-1"), re.I)
    enc = m.group(1) if m else "utf-8"
    for e in (enc, "utf-8", "big5", "gb18030"):
        try:
            return body.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def _clean(s: str, n: int = 120) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">") \
         .replace("&quot;", '"').replace("&#39;", "'")
    return " ".join(s.split())[:n]


def probe_ssdp(ips, timeout: float = 2.2, should_stop=None) -> dict:
    """回傳 {ip: {"server", "friendly", "manufacturer", "model", "model_number", "serial", ...}}。"""
    targets, found = set(ips), {}

    def on_packet(data, src):
        if src not in targets:
            return
        text = data.decode("utf-8", "replace")
        hdr = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdr[k.strip().lower()] = v.strip()
        r = found.setdefault(src, {"server": "", "locations": [], "st": []})
        if hdr.get("server") and not r["server"]:
            r["server"] = hdr["server"][:120]
        loc = hdr.get("location", "")
        if loc and loc not in r["locations"] and len(r["locations"]) < 3:
            r["locations"].append(loc)
        st = hdr.get("st") or hdr.get("nt") or ""
        if st and st not in r["st"] and len(r["st"]) < 20:
            r["st"].append(st[:120])

    try:
        s = _udp_socket()
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass
    except OSError:
        return {}
    try:
        start = time.time()
        for st in ("ssdp:all", "upnp:rootdevice"):
            _send(s, _SSDP_MSEARCH.format(st=st).encode(), ("239.255.255.250", 1900))
        _collect(s, start + timeout / 2, on_packet, should_stop)
        if not (should_stop and should_stop()):
            _send(s, _SSDP_MSEARCH.format(st="ssdp:all").encode(), ("239.255.255.250", 1900))
            _collect(s, start + timeout, on_packet, should_stop)
    finally:
        s.close()

    def fetch(ip, loc):
        # 只抓「回應者自己位址」上的描述檔（LOCATION 是設備給的，不能拿來連別的主機）
        m = re.match(r"^http://(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?(/[^\s]*)?$", loc)
        if not m or m.group(1) != ip:
            return None
        port = int(m.group(2) or 80)
        if not 0 < port < 65536:
            return None
        try:
            raw = _http_get_raw(ip, port, m.group(3) or "/", False, 1.6, _MAX_XML)
        except OSError:
            return None
        head, _, body = raw.partition(b"\r\n\r\n")
        xml = _decode_html(body, head.decode("latin-1", "replace"))
        dev = {}
        for tag, key in _XML_FIELDS.items():              # 用樣式比對，不交給 XML 解析器（避免實體展開）
            mm = re.search(rf"<{tag}>\s*([^<]{{1,200}})", xml)
            if mm:
                dev[key] = _clean(mm.group(1))
        return dev

    out = {}
    jobs = [(ip, loc) for ip, r in found.items() for loc in r["locations"][:2]]
    if jobs and not (should_stop and should_stop()):
        with ThreadPoolExecutor(max_workers=min(24, len(jobs))) as ex:
            results = list(ex.map(lambda j: (j[0], fetch(*j)), jobs))
        for ip, dev in results:
            if dev:
                cur = out.setdefault(ip, {})
                for k, v in dev.items():
                    cur.setdefault(k, v)
    for ip, r in found.items():
        cur = out.setdefault(ip, {})
        if r["server"]:
            cur["server"] = r["server"]
        if any("InternetGatewayDevice" in x for x in r["st"]):
            cur["igd"] = True
        if any("MediaRenderer" in x for x in r["st"]):
            cur["media_renderer"] = True
        if any("MediaServer" in x for x in r["st"]):
            cur["media_server"] = True
    return out


# ════════════════════════════════════════════════════════════════════════════
#  HTTP(S) 首頁 / SSH / FTP 橫幅
# ════════════════════════════════════════════════════════════════════════════
_WEB_PORTS = [(80, False), (443, True), (8080, False), (8443, True), (5000, False), (5001, True), (8081, False)]


def _probe_web(ip: str, ports: set) -> dict:
    out = {}
    for port, tls in _WEB_PORTS:
        if port not in ports:
            continue
        try:
            path = "/"
            for _hop in range(2):                         # 最多跟一次「同一台、同一埠」的轉址
                raw = _http_get_raw(ip, port, path, tls, 1.8, 24 * 1024)
                head, _, body = raw.partition(b"\r\n\r\n")
                hs = head.decode("latin-1", "replace")
                m = re.match(r"HTTP/\d\.\d\s+(\d{3})", hs)
                if not m:
                    break
                loc = re.search(r"^location:\s*(\S+)", hs, re.I | re.M)
                if m.group(1) in ("301", "302", "303", "307", "308") and loc and _hop == 0:
                    target = loc.group(1)
                    if target.startswith("/"):
                        path = target[:300]
                        continue
                    mm = re.match(rf"^https?://{re.escape(ip)}(?::{port})?(/\S*)?$", target)
                    if mm:
                        path = (mm.group(1) or "/")[:300]
                        continue
                break
            else:
                continue
            server = re.search(r"^server:\s*(.+)$", hs, re.I | re.M)
            realm = re.search(r'^www-authenticate:.*realm="([^"]{1,80})"', hs, re.I | re.M)
            html = _decode_html(body, hs)
            title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            ent = {"port": port,
                   "server": server.group(1).strip()[:80] if server else "",
                   "title": _clean(title.group(1), 100) if title else "",
                   "realm": realm.group(1).strip() if realm else ""}
            if ent["server"] or ent["title"] or ent["realm"]:
                out.setdefault("web", []).append(ent)
            if len(out.get("web", [])) >= 2:
                break
        except (OSError, ssl.SSLError, ValueError):
            continue
    return out


def _probe_banner(ip: str, port: int) -> str:
    try:
        with socket.create_connection((ip, port), timeout=1.0) as s:
            s.settimeout(1.2)
            data = s.recv(160)
        line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        return "".join(c for c in line if c.isprintable())[:100]
    except OSError:
        return ""


def probe_tcp(ports_by_ip: dict, should_stop=None) -> dict:
    """依已知開放埠讀橫幅。回傳 {ip: {"web": [...], "ssh": "...", "ftp": "..."}}。"""
    jobs = []
    for ip, ports in ports_by_ip.items():
        if ports & {p for p, _ in _WEB_PORTS}:
            jobs.append((ip, "web", None))
        if 22 in ports:
            jobs.append((ip, "ssh", 22))
        if 21 in ports:
            jobs.append((ip, "ftp", 21))
    out = {}
    if not jobs:
        return out

    def run(job):
        ip, kind, port = job
        if should_stop and should_stop():
            return ip, kind, None
        if kind == "web":
            return ip, kind, _probe_web(ip, ports_by_ip[ip])
        return ip, kind, _probe_banner(ip, port)

    with ThreadPoolExecutor(max_workers=min(48, len(jobs))) as ex:
        for ip, kind, res in ex.map(run, jobs):
            if not res:
                continue
            cur = out.setdefault(ip, {})
            if kind == "web":
                cur.update(res)
            else:
                cur[kind] = res
    return out


# ════════════════════════════════════════════════════════════════════════════
#  彙整：全部探測並行 → 每台主機的名稱 / 型號 / 作業系統 / 類型
# ════════════════════════════════════════════════════════════════════════════
def discover(ips, ports_by_ip: dict, should_stop=None) -> dict:
    """並行跑完所有探測。回傳 {ip: {"mdns": {...}, "netbios": {...}, "upnp": {...},
    "snmp": {...}, "web": [...], "ssh": "...", "ftp": "..."}}（只含有結果的項目）。"""
    ips = [ip for ip in ips if _is_ipv4(ip)]
    if not ips:
        return {}
    box = {}

    def run(key, fn, *a):
        try:
            box[key] = fn(*a, should_stop=should_stop)
        except Exception:
            box[key] = {}

    threads = [threading.Thread(target=run, args=a, daemon=True) for a in (
        ("mdns", probe_mdns, ips), ("netbios", probe_netbios, ips), ("snmp", probe_snmp, ips),
        ("upnp", probe_ssdp, ips), ("tcp", probe_tcp, ports_by_ip))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=12)
    out = {}
    for key in ("mdns", "netbios", "snmp", "upnp"):
        for ip, v in (box.get(key) or {}).items():
            if v:
                out.setdefault(ip, {})[key] = v
    for ip, v in (box.get("tcp") or {}).items():
        out.setdefault(ip, {}).update(v)
    return out


def _is_ipv4(s) -> bool:
    try:
        return ipaddress.ip_address(s).version == 4
    except ValueError:
        return False


# Apple 機型代號 → 產品名（_device-info 的 model，例 MacBookPro18,3）
_APPLE_MODELS = [
    ("macbookpro", "MacBook Pro", "macOS", "Mac"), ("macbookair", "MacBook Air", "macOS", "Mac"),
    ("macbook", "MacBook", "macOS", "Mac"), ("macmini", "Mac mini", "macOS", "Mac"),
    ("macstudio", "Mac Studio", "macOS", "Mac"), ("macpro", "Mac Pro", "macOS", "Mac"),
    ("imacpro", "iMac Pro", "macOS", "Mac"), ("imac", "iMac", "macOS", "Mac"),
    ("mac", "Mac", "macOS", "Mac"),
    ("iphone", "iPhone", "iOS", "手機"), ("ipad", "iPad", "iPadOS", "平板"),
    ("ipod", "iPod touch", "iOS", "手機"), ("appletv", "Apple TV", "tvOS", "電視/串流裝置"),
    ("audioaccessory", "HomePod", "audioOS", "智慧喇叭"), ("watch", "Apple Watch", "watchOS", "穿戴裝置"),
    ("airport", "AirPort 基地台", "", "Wi-Fi AP"), ("timecapsule", "Time Capsule", "", "NAS"),
    ("realitydevice", "Apple Vision Pro", "visionOS", "穿戴裝置"),
]


def _apple_model(code: str):
    c = (code or "").lower()
    for prefix, name, os_name, dtype in _APPLE_MODELS:
        if c.startswith(prefix):
            return f"{name}（{code}）", os_name, dtype
    return None


def _ttl_hint(ttl) -> str:
    if not isinstance(ttl, int) or ttl <= 0:
        return ""
    if ttl <= 64:
        return "類 Unix（Linux / macOS / iOS / Android）"
    if ttl <= 128:
        return "可能是 Windows"
    return "可能是網路設備（路由器 / 交換器）"


def identify(host: dict, d: dict) -> dict:
    """把探測結果 d 併入主機資料，回傳要更新的欄位：
    name（顯示名稱）、model（型號）、os、type / type_conf（有更可靠的證據才覆蓋）、
    details（所有原始資料，供詳情與報告使用）。"""
    d = d or {}
    mdns, nb, upnp, snmp = d.get("mdns") or {}, d.get("netbios") or {}, d.get("upnp") or {}, d.get("snmp") or {}
    web, ssh, ftp = d.get("web") or [], d.get("ssh") or "", d.get("ftp") or ""
    txt = mdns.get("txt") or {}
    services = set(mdns.get("services") or [])
    ports = set(host.get("open_ports") or [])
    vendor = (host.get("vendor") or "").lower()

    name = model = os_name = ""
    dtype, conf = None, None

    def set_type(t, c="high"):
        nonlocal dtype, conf
        rank = {"low": 0, "medium": 1, "high": 2}
        if dtype is None or rank[c] > rank[conf]:
            dtype, conf = t, c

    # ── 型號 ────────────────────────────────────────────────────────────────
    apple = _apple_model((txt.get("_device-info._tcp") or {}).get("model", ""))
    if not apple:
        for st in ("_airplay._tcp", "_raop._tcp"):
            apple = apple or _apple_model((txt.get(st) or {}).get("model") or (txt.get(st) or {}).get("am", ""))
    if apple:
        model, os_name = apple[0], apple[1]
        set_type(apple[2])
    air = txt.get("_airplay._tcp") or {}
    if air.get("osvers") and os_name:
        os_name = f"{os_name} {air['osvers']}"
    if not model and (air.get("model") or air.get("manufacturer")):       # 非 Apple 的 AirPlay（電視、喇叭）
        model = " ".join(x for x in (air.get("manufacturer", ""), air.get("model", "")) if x)
        set_type("電視/串流裝置", "medium")
    cast = txt.get("_googlecast._tcp") or {}
    if cast:
        model = model or cast.get("md", "")
        set_type("電視/串流裝置")
    for st in ("_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp", "_uscan._tcp", "_scanner._tcp"):
        t = txt.get(st) or {}
        if st in services or t:
            set_type("印表機/掃描器")
            model = model or t.get("ty") or t.get("product", "").strip("()") or t.get("usb_mdl", "")
    if upnp:
        um = " ".join(x for x in (upnp.get("manufacturer", ""), upnp.get("model", "")) if x)
        num = upnp.get("model_number", "")
        if num and num not in um:
            um = f"{um} {num}".strip()
        model = model or um
        dt = upnp.get("device_type", "")
        if upnp.get("igd") or "InternetGatewayDevice" in dt:
            set_type("路由器/防火牆")
        elif "MediaRenderer" in dt or upnp.get("media_renderer"):
            set_type("電視/串流裝置", "medium")
        elif "MediaServer" in dt or upnp.get("media_server"):
            set_type("NAS", "medium")
        elif "Printer" in dt:
            set_type("印表機/掃描器")
        elif "Camera" in dt or "NetworkCamera" in dt:
            set_type("IP 攝影機")
    descr = snmp.get("descr", "")
    if descr:
        model = model or descr[:90]
        dl = descr.lower()
        for keys, t in ((("switch", "procurve", "catalyst", "edgeswitch", "usw-", "gs7", "gs3", "smart switch"), "交換器"),
                        (("routeros", "router", "edgeos", "fortigate", "vigor", "firewall", "pfsense", "opnsense"), "路由器/防火牆"),
                        (("access point", "wireless", " uap", "unifi ap", "aironet"), "Wi-Fi AP"),
                        (("printer", "laserjet", "officejet", "mfp", "imagerunner", "ecosys", "bizhub"), "印表機/掃描器"),
                        (("synology", "qnap", "diskstation", "readynas", "truenas"), "NAS"),
                        (("ups", "smart-ups"), "UPS / 電力設備"),
                        (("camera", "nvr", "dvr", "hikvision", "dahua"), "IP 攝影機")):
            if any(k in dl for k in keys):
                set_type(t)
                break
    titles = " | ".join(x for w in web for x in (w.get("title"), w.get("realm")) if x)
    servers = " ".join(w.get("server", "") for w in web)
    blob = f"{titles} {servers}".lower()
    if not model and titles:
        model = titles.split(" | ")[0][:80]
    for keys, t, c in ((("synology", "diskstation", "qnap", "qts", "truenas", "asustor", "openmediavault", "unraid"), "NAS", "high"),
                       (("routeros", "mikrotik", "fortigate", "pfsense", "opnsense", "vigor", "draytek", "edgerouter",
                         "openwrt", "luci", "sonicwall", "sophos", "zywall", "usg"), "路由器/防火牆", "high"),
                       (("unifi", "omada", "aruba", "ruckus", "access point", "engenius", "aimesh", "deco"), "Wi-Fi AP", "medium"),
                       (("hikvision", "dahua", "ipcam", "ip camera", "network camera", "nvr", "dvr", "webcam", "axis"), "IP 攝影機", "high"),
                       (("laserjet", "officejet", "printer", "epson", "brother", "canon", "ricoh", "kyocera", "xerox",
                         "konica", "bizhub", "mfp"), "印表機/掃描器", "medium"),
                       (("idrac", "ilo", "ipmi", "esxi", "vmware", "proxmox"), "伺服器 / 虛擬化", "high"),
                       (("home assistant", "esphome", "tasmota", "shelly", "philips hue"), "手機/IoT", "medium"),
                       (("switch", "edgeswitch", "procurve"), "交換器", "medium")):
        if any(k in blob for k in keys):
            set_type(t, c)
            break

    # ── 服務類型 → 設備類型 ─────────────────────────────────────────────────
    if services & {"_sonos._tcp", "_spotify-connect._tcp"} and dtype is None:
        set_type("智慧喇叭", "medium")
    if services & {"_hap._tcp", "_homekit._tcp", "_matter._tcp", "_hue._tcp", "_miio._udp", "_esphomelib._tcp"} \
            and dtype is None:
        set_type("手機/IoT", "medium")
    if "_axis-video._tcp" in services:
        set_type("IP 攝影機")
    if services & {"_adisk._tcp", "_qdiscover._tcp"} and dtype is None:
        set_type("NAS", "medium")
    if "_workstation._tcp" in services and dtype is None:
        set_type("伺服器 / Linux", "medium")
    if 554 in ports and dtype is None and not ports & {445, 139, 3389}:
        set_type("IP 攝影機", "medium")

    # ── 作業系統 ────────────────────────────────────────────────────────────
    if not os_name:
        sl, dl = ssh.lower(), descr.lower()
        if "windows" in sl or "windows" in dl or "microsoft" in servers.lower():
            os_name = "Windows"
        elif "routeros" in dl or "rosssh" in sl or "mikrotik" in blob:
            os_name = "RouterOS"
        elif "cisco" in dl or "cisco" in sl:
            os_name = "Cisco IOS"
        elif "ubuntu" in sl:
            os_name = "Ubuntu Linux"
        elif "debian" in sl or "raspbian" in sl:
            os_name = "Debian Linux"
        elif "dropbear" in sl or "openwrt" in blob:
            os_name = "嵌入式 Linux"
        elif "linux" in dl or "freebsd" in sl or "openssh" in sl:
            os_name = "Linux / Unix"
        elif nb.get("name") and ports & {445, 3389, 139} and "apple" not in vendor \
                and dtype not in ("NAS", "印表機/掃描器"):
            os_name = "Windows"
            set_type("PC / Windows", "medium")
    os_hint = _ttl_hint(host.get("ttl"))

    # ── 顯示名稱：設備自己宣告的優先 ─────────────────────────────────────────
    cand = []
    for n in mdns.get("names") or []:
        n = re.sub(r"^[0-9A-Fa-f]{12}@", "", n)           # AirPlay 音訊的「MAC@名稱」
        n = re.sub(r"\s*\[[0-9A-Fa-f:]{6,}\]$", "", n)    # 工作站的「名稱 [MAC]」
        if n and not re.fullmatch(r"[0-9A-Fa-f:-]{12,}", n):
            cand.append(n)
    cand += [upnp.get("friendly", ""), nb.get("name", ""), snmp.get("name", ""),
             re.sub(r"\.local\.?$", "", mdns.get("hostname", ""))]
    name = next((c.strip() for c in cand if c and c.strip()), "")

    out = {"details": {k: v for k, v in d.items() if v}}
    if name:
        out["name"] = name[:80]
    if model:
        out["model"] = " ".join(model.split())[:100]
    if os_name:
        out["os"] = os_name
    elif os_hint:
        out["os_hint"] = os_hint
    if dtype:
        out["type"], out["type_conf"] = dtype, conf
    if nb.get("workgroup"):
        out["workgroup"] = nb["workgroup"]
    serial = (upnp.get("serial") or "").strip()
    if serial and not re.fullmatch(r"[0Xx_\-\s]*|none|n/?a|unknown|1234567890?|default", serial, re.I):
        out["serial"] = serial[:60]                       # 全 0、none 之類的佔位值不算序號
    if services:
        out["service_labels"] = sorted({SERVICE_LABEL.get(s, s.strip("_").split("._")[0]) for s in services
                                        if s != "_device-info._tcp"})
    return out
