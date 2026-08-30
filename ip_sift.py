#!/usr/bin/env python3
"""阶段1: 免费socks5抓取 → ip-api 质量预筛(只留住宅/ISP) → SOCKS5握手+连通实测 → sifted.txt
不依赖浏览器; 阶段2(sift_browser.py)再用 uc_ts.py 逐个真·试盾。
排除名单/优质名单都存 Gist(dead_pool.txt / good_pool.txt), 跨 run 累积。"""
import os, sys, json, time, socket, struct, random, urllib.request as U
from concurrent.futures import ThreadPoolExecutor

from cfg_open import load as _cfg
_CFG = _cfg()
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_ID = _CFG["GIST_ID"]
BROWSER_N = int(os.environ.get("SIFT_COUNT", "15"))   # 交给阶段2浏览器实测的数量
GIST_FILE = "dead_pool.txt"  # 只读; good_pool.txt 由阶段2写

SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=3000&country=all",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5/list.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
]

def jreq(url, method="GET", data=None, hdrs=None, timeout=20):
    h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if hdrs: h.update(hdrs)
    body = json.dumps(data).encode() if data is not None else None
    r = U.Request(url, data=body, headers=h, method=method)
    try:
        with U.urlopen(r, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode())
    except Exception as e:
        try: return getattr(e, "code", -1) or -1, json.loads(e.read().decode())
        except Exception: return -1, {"error": str(e)[:120]}

def fetch_lists():
    raw = set()
    for url in SOURCES:
        try:
            st, _ = jreq(url, timeout=25)
            txt = _ if isinstance(_, str) else ""
        except Exception:
            st, txt = -1, ""
        # jreq 假定 json; 列表是纯文本, 单独拉
        try:
            with U.urlopen(U.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as res:
                txt = res.read().decode("utf-8", "ignore")
        except Exception as e:
            print(f"[src] {url.split('/')[2]} 失败: {str(e)[:60]}"); continue
        n0 = len(raw)
        for line in txt.splitlines():
            line = line.strip()
            if "://" in line: line = line.split("://", 1)[1]
            if ":" in line and line.replace(".", "").replace(":", "").isdigit():
                raw.add(line)
        print(f"[src] {url.split('/')[2]} +{len(raw)-n0}")
    return raw

def gist_file(name):
    st, d = jreq(f"https://api.github.com/gists/{GIST_ID}",
                 hdrs={"Authorization": "token " + GIST_TOKEN})
    return ((d.get("files") or {}).get(name, {}).get("content") or "")

def ip_quality(ip_list):
    """ip-api batch: 只留 proxy=False 且 hosting=False(住宅/ISP出口)。15/批, 45/分钟。"""
    keep, stats = [], {"dc": 0, "flagged": 0, "err": 0}
    for i in range(0, len(ip_list), 15):
        chunk = ip_list[i:i+15]
        hosts = [p.split(":")[0] for p in chunk]   # ip-api 只认纯 IP, host:port 会整批无效
        st, d = jreq("http://ip-api.com/batch?fields=query,proxy,hosting,isp,country",
                     "POST", hosts, timeout=15)
        if st != 200 or not isinstance(d, list):
            print(f"[api] batch {i//15} 失败 st={st}, 整批放行(交连通性测兜底)")
            keep += chunk; continue
        for item in chunk:
            info = next((x for x in d if x.get("query") == item.split(":")[0]), None)
            if not info: keep.append(item); continue
            if info.get("proxy"): stats["flagged"] += 1
            elif info.get("hosting"): stats["dc"] += 1
            else: keep.append(item)
        if i + 15 < len(ip_list): time.sleep(1.4)   # 免费版限速
    print(f"[api] 质量筛: 留{len(keep)} 数据中心{stats['dc']} 被标代理{stats['flagged']}")
    return keep

def socks5_ok(target, timeout=7):
    """完整握手 + 连通目标, 返回(是否可用, 延迟ms)。IP-ATYP 直连(实测这批代理拒域名ATYP)。"""
    host, port = target.split(":")[0], int(target.split(":")[1])
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00": s.close(); return False, 0
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("1.1.1.1") + struct.pack(">H", 80))
        r = s.recv(64)
        s.close()
        if len(r) < 2 or r[1] != 0: return False, 0
        return True, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0

if __name__ == "__main__":
    all_px = fetch_lists()
    print(f"[sift] 抓到 {len(all_px)} 个(去重后)")
    dead = set(l.strip() for l in gist_file("dead_pool.txt").splitlines() if l.strip())
    good = set(l.strip() for l in gist_file("good_pool.txt").splitlines() if l.strip())
    cand = [p for p in all_px if p not in dead and p not in good]
    print(f"[sift] 排除 dead {len(dead & all_px)} / 已good {len(good & all_px)}, 候选 {len(cand)}")
    random.shuffle(cand)
    cand = cand[:600]   # ip-api 配额内(15/批 + 1.4s 间隔); 源扩到 5 个后池子更大
    keep = ip_quality(cand)
    t0 = time.time()
    with ThreadPoolExecutor(64) as ex:
        results = dict(zip(keep, ex.map(socks5_ok, keep)))
    alive = sorted([(ms, p) for p, (ok, ms) in results.items() if ok])
    seen, uniq = set(), []
    for ms, p in alive:            # 同一 IP 多端口只留最快的 2 个, 避免浪费浏览器预算
        h = p.split(":")[0]
        if h not in seen or sum(1 for x in uniq if x.startswith(h + ":")) < 2:
            uniq.append(p); seen.add(h)
    alive = uniq
    print(f"[sift] 连通测 {len(keep)} 个 -> 活 {len(results) and len([1 for v in results.values() if v[0]])}, 耗时 {int(time.time()-t0)}s")
    picked = alive[:BROWSER_N]
    with open("sifted.txt", "w") as f:
        f.write("\n".join(picked))
    for p in alive[:20]:
        print("  " + p)
    print(f"[sift] sifted.txt {len(picked)} 个, 交给阶段2真·试盾")
