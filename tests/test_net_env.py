"""net_env：對外路徑判讀（用假的 traceroute 輸出）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import net_env as N          # noqa: E402


def path(txt, gateway=""):
    with mock.patch.object(N, "_run", return_value=txt):
        return N._nat_path(gateway=gateway)


class NatPathTest(unittest.TestCase):
    def test_single_nat(self):
        r = path(" 1  192.168.1.1  2.1 ms  1.9 ms\n 2  61.220.1.1  8 ms  7 ms\n", "192.168.1.1")
        self.assertFalse(r["double_nat"])
        self.assertFalse(r["cgnat"])
        self.assertEqual(r["routers"], ["192.168.1.1"])

    def test_double_nat(self):
        r = path(" 1  192.168.1.1  3 ms  3 ms\n 2  192.168.0.1  4 ms  4 ms\n 3  61.220.1.1  9 ms 9 ms\n", "192.168.1.1")
        self.assertTrue(r["double_nat"])
        self.assertEqual(r["routers"], ["192.168.1.1", "192.168.0.1"])

    def test_gateway_silent_on_first_hop(self):
        # 閘道沒回應 traceroute（第一跳是 * *）→ 用已知閘道補上，仍要判出雙重 NAT
        r = path(" 1  * *\n 2  192.168.0.1  4 ms  4 ms\n 3  61.220.1.1  9 ms\n", "192.168.1.1")
        self.assertTrue(r["double_nat"])
        self.assertEqual(r["hops"][0]["ip"], "192.168.1.1")

    def test_one_probe_lost(self):
        r = path(" 1  192.168.1.1  3 ms  3 ms\n 2  * 192.168.0.1  4 ms\n 3  61.220.1.1  9 ms\n", "192.168.1.1")
        self.assertTrue(r["double_nat"])

    def test_cgnat_range(self):
        r = path(" 1  192.168.1.1  3 ms\n 2  100.72.0.1  9 ms\n 3  61.220.1.1  9 ms\n", "192.168.1.1")
        self.assertTrue(r["cgnat"])
        self.assertFalse(r["double_nat"])

    def test_isp_private_backbone_is_not_double_nat(self):
        # 第二跳以後的 10.x / 172.16-31.x 多半是 ISP 骨幹，不算客戶自己的第二台路由器
        r = path(" 1  192.168.1.1  3 ms\n 2  172.31.11.130  9 ms\n 3  10.2.37.21  9 ms\n", "192.168.1.1")
        self.assertFalse(r["double_nat"])
        self.assertTrue(r["cgnat"])

    def test_no_output(self):
        r = path("", "192.168.1.1")
        self.assertFalse(r["double_nat"])
        self.assertEqual(r["routers"], ["192.168.1.1"])


class AssessTest(unittest.TestCase):
    def test_flags(self):
        lan = {"ip": "192.168.1.5", "gateway": "192.168.1.1", "dns": ["192.168.1.1"],
               "dhcp": {"server": "192.168.1.9", "lease_sec": 600}, "dhcp_not_gateway": True}
        wan = {"online": True, "nat": {"double_nat": True, "routers": ["192.168.1.1", "192.168.0.1"]}}
        titles = [i["title"] for i in N.assess(lan, wan)]
        self.assertTrue(any("雙重 NAT" in t for t in titles))
        self.assertTrue(any("DHCP 伺服器不是閘道" in t for t in titles))
        self.assertTrue(any("租約很短" in t for t in titles))
        self.assertTrue(any("只有一台 DNS" in t for t in titles))

    def test_healthy(self):
        lan = {"ip": "192.168.1.5", "gateway": "192.168.1.1", "dns": ["192.168.1.1", "1.1.1.1"], "dhcp": {}}
        out = N.assess(lan, {"online": True, "nat": {}})
        self.assertEqual([i["severity"] for i in out], ["good"])

    def test_offline(self):
        out = N.assess({"ip": "169.254.3.4", "apipa": True, "gateway": ""}, {"online": False, "nat": {}})
        self.assertEqual({i["severity"] for i in out}, {"critical"})


if __name__ == "__main__":
    unittest.main()
