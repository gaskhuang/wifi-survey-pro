"""device_probe 的封包解析與辨識規則（用合成封包，不需要網路）。
執行：.venv/bin/python3 -m unittest discover -s tests"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import device_probe as P          # noqa: E402
import lan_scanner as L           # noqa: E402


def _name(n):
    return b"".join(bytes([len(x)]) + x.encode() for x in n.split(".")) + b"\x00"


class DnsTest(unittest.TestCase):
    def test_roundtrip_and_compression(self):
        # 回應：PTR（用指標壓縮）+ TXT + SRV + A，class 帶 cache-flush 位元（0x8001）
        svc = _name("_airplay._tcp.local")
        hdr = struct.pack("!6H", 0, 0x8400, 0, 4, 0, 0)
        ptr_rd = b"\x06Living" + b"\xc0\x0c"                         # "Living" + 指向位移 12 的服務名
        rr1 = svc + struct.pack("!HHIH", P.T_PTR, 1, 120, len(ptr_rd)) + ptr_rd
        inst_off = len(hdr) + len(svc) + 10
        txt_rd = b"\x0fmodel=AppleTV6,2\x07osvers=17.4"[:0] or (b"\x10model=AppleTV6,2" + b"\x0bosvers=17.4")
        rr2 = struct.pack("!H", 0xC000 | inst_off) + struct.pack("!HHIH", P.T_TXT, 0x8001, 120, len(txt_rd)) + txt_rd
        host = _name("Living-Room.local")
        srv_rd = struct.pack("!HHH", 0, 0, 7000) + host
        rr3 = struct.pack("!H", 0xC000 | inst_off) + struct.pack("!HHIH", P.T_SRV, 0x8001, 120, len(srv_rd)) + srv_rd
        rr4 = host + struct.pack("!HHIH", P.T_A, 0x8001, 120, 4) + bytes([192, 168, 1, 50])
        rrs = P._dns_parse(hdr + rr1 + rr2 + rr3 + rr4)
        self.assertIn(("_airplay._tcp.local", P.T_PTR, "Living._airplay._tcp.local"), rrs)
        self.assertIn(("Living._airplay._tcp.local", P.T_TXT, {"model": "AppleTV6,2", "osvers": "17.4"}), rrs)
        self.assertIn(("Living._airplay._tcp.local", P.T_SRV, (7000, "Living-Room.local")), rrs)
        self.assertIn(("Living-Room.local", P.T_A, "192.168.1.50"), rrs)

    def test_garbage_is_ignored(self):
        self.assertEqual(P._dns_parse(b""), [])
        self.assertEqual(P._dns_parse(b"\xff" * 40), [])
        loop = struct.pack("!6H", 0, 0, 0, 1, 0, 0) + b"\xc0\x0c" + b"\x00" * 10   # 指標指向自己
        self.assertEqual(P._dns_parse(loop), [])

    def test_query_builds(self):
        q = P._dns_query([("_ipp._tcp.local", P.T_PTR)])
        self.assertEqual(struct.unpack("!6H", q[:12])[2], 1)
        self.assertTrue(q.endswith(struct.pack("!HH", P.T_PTR, 1)))


class SnmpTest(unittest.TestCase):
    def test_request_and_response(self):
        self.assertEqual(P._snmp_get()[0], 0x30)
        binds = b"".join(P._ber(0x30, P._ber_oid(o) + P._ber(0x04, v)) for o, v in (
            ("1.3.6.1.2.1.1.1.0", b"HP J9773A 2530-24G-PoEP Switch, revision YA.16"),
            ("1.3.6.1.2.1.1.5.0", b"SW-1F-Core"), ("1.3.6.1.2.1.1.6.0", b"")))
        pdu = P._ber(0xA2, P._ber(0x02, b"\x4e\x49") + P._ber(0x02, b"\x00") + P._ber(0x02, b"\x00") + P._ber(0x30, binds))
        msg = P._ber(0x30, P._ber(0x02, b"\x01") + P._ber(0x04, b"public") + pdu)
        r = P._snmp_parse(msg)
        self.assertEqual(r["name"], "SW-1F-Core")
        self.assertIn("2530-24G", r["descr"])
        self.assertNotIn("location", r)
        self.assertEqual(P._snmp_parse(b"\x30\x03abc"), {})

    def test_long_length_encoding(self):
        body = b"x" * 300
        enc = P._ber(0x04, body)
        self.assertEqual(P._ber_read(enc, 0)[1], body)


class NetbiosTest(unittest.TestCase):
    def test_nbstat_response(self):
        def ent(name, suffix, flags):
            return name.ljust(15).encode() + bytes([suffix]) + struct.pack("!H", flags)
        names = ent("ACCT-PC01", 0x00, 0x0400) + ent("OFFICE", 0x00, 0x8400) + ent("ACCT-PC01", 0x20, 0x0400)
        rd = bytes([3]) + names + b"\x00\x11\x22\x33\x44\x55" + b"\x00" * 40
        pkt = (struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + b"\x20" + b"CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" + b"\x00"
               + struct.pack("!HHIH", 0x21, 1, 0, len(rd)) + rd)
        self.assertEqual(P._nbstat_parse(pkt), {"name": "ACCT-PC01", "workgroup": "OFFICE", "file_server": True})
        self.assertIsNone(P._nbstat_parse(P._NBSTAT))            # 自己送出的查詢不是回應
        self.assertIsNone(P._nbstat_parse(b"\x00" * 80))
        self.assertIsNone(P._nbstat_parse(pkt[:60]))


class IdentifyTest(unittest.TestCase):
    def host(self, **kw):
        return {"vendor": "", "open_ports": [], "ttl": None, **kw}

    def test_apple_tv(self):
        r = P.identify(self.host(vendor="Apple, Inc."), {"mdns": {
            "hostname": "Living-Room.local", "names": ["AABBCCDDEEFF@客廳"], "services": ["_airplay._tcp", "_raop._tcp"],
            "txt": {"_airplay._tcp": {"model": "AppleTV6,2", "osvers": "17.4"}}}})
        self.assertEqual(r["name"], "客廳")
        self.assertIn("Apple TV", r["model"])
        self.assertEqual(r["os"], "tvOS 17.4")
        self.assertEqual((r["type"], r["type_conf"]), ("電視/串流裝置", "high"))

    def test_windows_pc(self):
        r = P.identify(self.host(open_ports=[139, 445, 3389], ttl=128),
                       {"netbios": {"name": "ACCT-PC01", "workgroup": "OFFICE", "file_server": True}})
        self.assertEqual((r["name"], r["os"], r["workgroup"]), ("ACCT-PC01", "Windows", "OFFICE"))

    def test_switch_by_snmp(self):
        r = P.identify(self.host(), {"snmp": {"name": "SW-1F", "descr": "HP 2530-24G Switch"}})
        self.assertEqual((r["name"], r["type"]), ("SW-1F", "交換器"))

    def test_nas_by_web_title(self):
        r = P.identify(self.host(open_ports=[5000, 5001]),
                       {"web": [{"port": 5000, "server": "nginx", "title": "Synology DiskStation - NAS01", "realm": ""}]})
        self.assertEqual(r["type"], "NAS")

    def test_placeholder_serial_dropped(self):
        r = P.identify(self.host(), {"upnp": {"friendly": "Archer AX73", "manufacturer": "TP-Link",
                                             "model": "Archer AX73", "serial": "00000000", "igd": True}})
        self.assertNotIn("serial", r)
        self.assertEqual(r["type"], "路由器/防火牆")
        self.assertEqual(P.identify(self.host(), {"upnp": {"serial": "SN12345XYZ"}})["serial"], "SN12345XYZ")

    def test_ttl_hint_only_when_no_os(self):
        self.assertIn("Windows", P.identify(self.host(ttl=128), {})["os_hint"])
        self.assertNotIn("os_hint", P.identify(self.host(ttl=64), {"ssh": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13"}))


class LanHelpersTest(unittest.TestCase):
    def test_random_mac(self):
        self.assertTrue(L._is_random_mac("82:35:85:2d:fb:79"))
        self.assertFalse(L._is_random_mac("b4:b0:24:02:1c:5c"))
        self.assertFalse(L._is_random_mac(""))

    def test_csv_formula_guard(self):
        self.assertEqual(L._csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(L._csv_safe("NAS01"), "NAS01")
        self.assertEqual(L._csv_safe(None), "")

    def test_iphone_port(self):
        self.assertEqual(L._type_from("", {62078}, "", False)[0], "iPhone / iPad")


if __name__ == "__main__":
    unittest.main()
