#!/usr/bin/env python3
"""ipapi.is 画像链路自测(09-26): 验证 sift_browser 的 ipis_prefetch/ipis_profile/
p0_is_idc/p0_sortkey 在 GitHub runner 真实网络下的行为。刻意不写 Gist、不动池子。
触发: dispatch probe.yml, PROBE_SCRIPT=ipis_selftest.py"""
import os, sys, json
os.environ.setdefault("GIST_TOKEN", "probe-nop")      # sift_browser 导入期需要, 本测不碰 Gist
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sift_browser as sb

TEST = ["216.236.6.248:1080", "184.181.178.33:4145", "104.28.215.68:1080",
        "8.8.8.8:53", "72.195.34.41:4145", "121.169.46.116:1090"]
px = os.environ.get("SINGLE_PROXY", "").strip()
if px and px not in TEST:
    TEST.insert(0, px)

print("[selftest] ipapi key len=%d, 候选 %d 个" % (len(sb.IPIS_KEY), len(TEST)), flush=True)
sb.ipis_prefetch(TEST)
for p in TEST:
    prof = sb.ipis_profile(p)
    brief = json.dumps({k: prof.get(k) for k in ("risk", "labels", "org")}, ensure_ascii=False)
    print("[selftest] %-22s idc=%-5s sk=%-4s %s"
          % (p, sb.p0_is_idc(prof), sb.p0_sortkey(prof), brief), flush=True)
