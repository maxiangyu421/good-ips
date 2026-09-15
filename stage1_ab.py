#!/usr/bin/env python3
"""stage1 A/B 对照: 现行「半开握手」 vs monosans 式「完整取回响应体」。

纯观测脚本 —— 不读写任何 Gist / 池子文件, 只往 stdout 打印对照矩阵。
目的: 量化 stage1 判据放进来多少「假活」代理(连得上但跑不完真实流量)。

臂 A = 现行 stage1 判据: SOCKS5 CONNECT 1.1.1.1:80, 只看回复码 (ip_sift.socks5_ok)
臂 B = monosans 判据:   隧道内真发一次 HTTPS GET 并读回完整响应体

用法: python3 stage1_ab.py            # 默认抽 800 个
      AB_N=500 python3 stage1_ab.py
"""
import os, random, socket, ssl, struct, sys, time, json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

AB_N = int(os.environ.get("AB_N", "800"))
AB_THREADS = int(os.environ.get("AB_THREADS", "300"))
CONNECT_TMO = float(os.environ.get("AB_CONNECT_TMO", "5"))
FULL_TMO = float(os.environ.get("AB_FULL_TMO", "10"))

# 与生产同源的小源列表(socks5)。这里故意只取小源, 大源(僵尸)不参与测量。
SRC = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=3000&country=all",
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/refs/heads/main/socks5_proxies.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt",
    "https://proxyspace.pro/socks5.txt",
]

HTTPS_HOST = "api.ipify.org"      # 小、稳、https
HTTP_HOST = "ip-api.com"          # 明文兜底(仅诊断用)


def _pull(url, raw):
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
            txt = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[src] {url.split('/')[2]} 失败: {str(e)[:60]}", flush=True)
        return
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            line = line.split("://", 1)[1]
        if "@" in line:
            line = line.split("@")[-1]
        line = line.split()[0]
        if ":" in line and line.replace(".", "").replace(":", "").isdigit():
            raw.add(line)


def socks_connect(host, port, dst_ip, dst_port, timeout):
    """建 SOCKS5 隧道到 dst_ip:dst_port(IP-ATYP, 与本批代理实测兼容)。"""
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        s.close(); return None
    s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(dst_ip) + struct.pack(">H", dst_port))
    r = s.recv(64)
    if len(r) < 2 or r[1] != 0:
        s.close(); return None
    return s


def arm_a(target, timeout=None):
    """臂 A: 现行判据 —— 半开握手, 只 CONNECT 1.1.1.1:80 看回复码。"""
    timeout = timeout or CONNECT_TMO
    host, port = target.split(":")[0], int(target.split(":")[1])
    t0 = time.time()
    try:
        s = socks_connect(host, port, "1.1.1.1", 80, timeout)
        if s is None:
            return False, 0
        s.close()
        return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0


def arm_b(target, timeout=None):
    """臂 B: monosans 判据 —— 隧道内完整取回 HTTPS 响应体。
    注意: 走 IP-ATYP 连接, SNI 必须手动设成主机名, 否则证书校验挂在 IP 上。"""
    timeout = timeout or FULL_TMO
    host, port = target.split(":")[0], int(target.split(":")[1])
    t0 = time.time()
    try:
        dst_ip = socket.gethostbyname(HTTPS_HOST)
        s = socks_connect(host, port, dst_ip, 443, timeout)
        if s is None:
            return False, 0
        ctx = ssl.create_default_context()
        ts = ctx.wrap_socket(s, server_hostname=HTTPS_HOST)
        ts.settimeout(timeout)
        ts.sendall(f"GET / HTTP/1.1\r\nHost: {HTTPS_HOST}\r\n"
                   f"User-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while len(buf) < 65536:
            chunk = ts.recv(4096)
            if not chunk:
                break
            buf += chunk
        ts.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        ok = head.startswith(b"HTTP/") and b"200" in head.split(b"\r\n")[0] and len(body) > 3
        return ok, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0


def ipapi_lookup(hosts):
    """批量查 ip-api 判断住宅/机房, 用于看臂 B 是否也提升了住宅占比。"""
    verdict = {}
    for i in range(0, len(hosts), 100):
        chunk = hosts[i:i + 100]
        try:
            req = Request("http://ip-api.com/batch?fields=query,proxy,hosting,isp,country",
                          data=json.dumps(chunk).encode(),
                          headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=25) as r:
                for x in json.loads(r.read().decode()):
                    verdict[x.get("query")] = x
        except Exception as e:
            print(f"[api] batch {i//100} 失败 {str(e)[:50]}", flush=True)
        if i + 100 < len(hosts):
            time.sleep(4.5)
    return verdict


def main():
    print(f"=== stage1 A/B 对照 (N={AB_N}, threads={AB_THREADS}) ===", flush=True)
    t_all = time.time()
    raw = set()
    for u in SRC:
        _pull(u, raw)
    pool = sorted(raw)
    print(f"[src] 汇总候选 {len(pool)} 个 (唯一 ip:port)", flush=True)

    random.seed(20260915)
    sample = random.sample(pool, min(AB_N, len(pool)))
    print(f"[ab] 抽样 {len(sample)} 个\n", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(AB_THREADS) as ex:
        a = list(ex.map(arm_a, sample))
    ta = int(time.time() - t0)
    t0 = time.time()
    with ThreadPoolExecutor(AB_THREADS) as ex:
        b = list(ex.map(arm_b, sample))
    tb = int(time.time() - t0)

    pa = {p for p, (ok, _) in zip(sample, a) if ok}
    pb = {p for p, (ok, _) in zip(sample, b) if ok}

    both = pa & pb
    only_a = pa - pb          # 半开过、全量不过 = 假活
    only_b = pb - pa          # 全量过、半开不过 = 会被现行判据误杀
    print(f"臂 A 半开握手通过      : {len(pa):>4}  ({100*len(pa)/len(sample):5.1f}%)   耗时 {ta}s")
    print(f"臂 B 完整取回通过      : {len(pb):>4}  ({100*len(pb)/len(sample):5.1f}%)   耗时 {tb}s")
    print()
    print(f"  两者都过              : {len(both):>4}")
    print(f"  仅 A 过(B 不过)= 假活 : {len(only_a):>4}   占 A 的 {100*len(only_a)/max(1,len(pa)):.1f}%")
    print(f"  仅 B 过 = A 会误杀    : {len(only_b):>4}")
    print(f"  两者都不过            : {len(sample)-len(pa)-len(only_b):>4}")

    if pb:
        hosts = sorted({p.split(":")[0] for p in pb})
        v = ipapi_lookup(hosts)
        prime = [h for h in hosts if v.get(h) and not v[h].get("hosting")]
        print(f"\n[住宅率] 臂 B 幸存者 {len(hosts)} 个 IP, ip-api 非机房标记 {len(prime)} 个 "
              f"({100*len(prime)/max(1,len(hosts)):.0f}%)", flush=True)
        for h in sorted(hosts)[:15]:
            x = v.get(h) or {}
            print(f"   {h:<16} {x.get('country','?'):<14} {(x.get('isp') or '?')[:40]}"
                  f"{'  [机房]' if x.get('hosting') else ''}")

    if pa:
        hosts_a = sorted({p.split(":")[0] for p in pa})
        v2 = ipapi_lookup(hosts_a)
        pa_non = sum(1 for h in hosts_a if v2.get(h) and not v2[h].get("hosting"))
        print(f"[住宅率] 臂 A 幸存者 {len(hosts_a)} 个 IP, ip-api 非机房标记 {pa_non} 个 "
              f"({100*pa_non/max(1,len(hosts_a)):.0f}%)", flush=True)

    print(f"\n总耗时 {int(time.time()-t_all)}s")


if __name__ == "__main__":
    main()
