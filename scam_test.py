#!/usr/bin/env python3
"""09-26: 测 runner 出口对 scamalytics / ping0 的 HTTP 可达性(不用浏览器/不走代理)。
背景: ping0 的 /ip/<addr> 已被阿里盾+CF 双盾封死, 但 ping0.cc/geo 与 /apiloc 对 VPS 免盾;
scamalytics 对 VPS 免盾且可直接查任意 IP。本脚本验证 runner 出口是否同样可用。
"""
import re, time, urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return r.status, len(b), b.decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return None, 0, "ERR:%s" % str(e)[:160], time.time() - t0


print("== 出口 IP ==", flush=True)
print(get("https://api.ipify.org")[2].strip(), flush=True)

IPS = ["1.1.1.1", "8.8.8.8", "72.195.101.99", "184.181.178.33"]
for ip in IPS:
    st, n, body, dt = get("https://scamalytics.com/ip/%s" % ip)
    sc = re.search(r"Fraud Score:\s*(\d+)", body)
    op = re.search(r"IP address %s is operated by ([^.]{0,70})" % re.escape(ip), body)
    print("scam %-16s http=%s size=%-6d score=%-5s %.1fs by=%s"
          % (ip, st, n, sc.group(1) if sc else None, dt, (op.group(1)[:60] if op else "")), flush=True)

print("== ping0 /geo ==", flush=True)
st, n, body, dt = get("https://ping0.cc/geo")
print("geo    http=%s size=%-6d %.1fs body=%r" % (st, n, dt, body[:140]), flush=True)

print("== ping0 /apiloc dummy ==", flush=True)
st, n, body, dt = get("https://ping0.cc/apiloc/apikey(test)/ip(1.1.1.1)")
print("apiloc http=%s size=%-6d %.1fs body=%r" % (st, n, dt, body[:140]), flush=True)

print("== ip-api (对照) ==", flush=True)
st, n, body, dt = get("http://ip-api.com/json/1.1.1.1?fields=status,country,isp,hosting,proxy")
print("ipapi  http=%s size=%-6d %.1fs body=%r" % (st, n, dt, body[:160]), flush=True)
print("DONE", flush=True)
