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
    # 原 5 源(合计唯一 ~2500)
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=3000&country=all",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    # 08-30 新增(实测净增: 合计唯一 2493 -> 105704)
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt",          # ~100k, 净增 99647
    "https://raw.githubusercontent.com/casals-ar/proxy-list/main/socks5",                 # 5419, 净增 2865
    "https://raw.githubusercontent.com/dpangestuw/Free-Proxy/refs/heads/main/socks5_proxies.txt",  # 2840, 净增 367
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",  # 405, 净增 262
    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/socks5.txt",      # 113, 净增 50
    "https://proxyspace.pro/socks5.txt",
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
            if not line or line.startswith("#"): continue
            if "://" in line: line = line.split("://", 1)[1]
            if "@" in line: line = line.split("@")[-1]      # user:pass@host:port
            line = line.split()[0]                           # 行尾带国家/延迟注释
            if ":" in line and line.replace(".", "").replace(":", "").isdigit():
                raw.add(line)
        print(f"[src] {url.split('/')[2]} +{len(raw)-n0}")
    return raw

def gist_file(name):
    st, d = jreq(f"https://api.github.com/gists/{GIST_ID}",
                 hdrs={"Authorization": "token " + GIST_TOKEN})
    return ((d.get("files") or {}).get(name, {}).get("content") or "")

# ===== 判据修正(08-30, 关键) =====
# 实测所有历史过盾 IP 的 ip-api 标记:
#   184.181.217.210 Cox Communications  proxy=True  hosting=False  ← 当前置顶
#   72.195.114.169  Cox Communications  proxy=True  hosting=False  ← 三次全成功
#   72.195.101.99   Cox Communications  proxy=True  hosting=False
#   199.66.183.251  NET2ATLANTA.COM     proxy=True  hosting=False
#   82.114.228.35   SCTS(俄)            proxy=False hosting=False
# → 免费 SOCKS5 出口天然被 ip-api 标 proxy=True, 按 proxy 过滤等于砍掉最能过盾的
#   住宅宽带代理。真正必砍的只有 hosting=True(机房/云) + ISP 名是云厂商。
# 优先级: 住宅ISP白名单 > 普通非机房 > 其他
DC_KW = ("alibaba", "aliyun", "tencent", "huawei cloud", "amazon", "google llc",
         "microsoft", "azure", "oracle", "digitalocean", "ovh", "hetzner", "linode",
         "vultr", "contabo", "choopa", "leaseweb", "m247", "zenlayer", "datacamp",
         "colocrossing", "psychz", "performive", "secured servers", "hostinger",
         "cloudflare", "gcore", "datacenter", "data center", "idc", " vps", "hosting",
         # 08-30 实测(ipapi.is is_datacenter=True, ip-api 却报 hosting=False)补入:
         "net2atlanta", "readydedis", "global connectivity solutions", "dedis",
         "server", "colo", "网络科技", "bandwidth", "ipvolume", "stark industries")

# 住宅宽带 ISP(过盾概率最高) —— Cox 是实测最强的一家
RES_KW = ("cox communications", "comcast", "charter", "spectrum", "verizon", "at&t",
          "centurylink", "frontier", "optimum", "cablevision", "rogers", "shaw",
          "telus", "bell canada", "virgin media", "bt ", "sky broadband", "talktalk",
          "orange", "telefonica", "movistar", "vodafone", "deutsche telekom",
          "telecom italia", "kpn", "telia", "telenor", "swisscom", "chinanet",
          "china unicom", "china mobile", "china telecom", "kddi", "ntt", "softbank",
          "korea telecom", "sk broadband", "lg dacom", "bsnl", "jio", "airtel",
          "telkom", "megafon", "mts", "beeline", "rostelecom", "komtel",
          "cincinnati bell", "cox ")
# 注: net2atlanta 曾在白名单, 08-30 实测 ipapi.is is_datacenter=True(机房) 已移除

def _has(s, kws):
    s = (s or "").lower()
    return any(k in s for k in kws)

def ip_quality(ip_list):
    """ip-api batch 质量筛。返回 (prime, normal, unknown) 三档:
      prime  = 住宅宽带 ISP 白名单命中(过盾概率最高, 优先吃浏览器预算)
      normal = 非机房、非白名单
      unknown= ip-api 查不到(只做填充)
    只按 hosting / ISP名 判机房; proxy=True 不再淘汰(见文件头判据说明)。
    限速真相: batch 端点 100 个/请求、15 请求/分钟 —— 旧版 15/批 会把 600 候选拆成
    40 请求直接撞限速, 于是大批 '整批放行' 让机房 IP 混进试盾队列。"""
    hosts = list(dict.fromkeys(p.split(":")[0] for p in ip_list))
    verdict, fails = {}, 0
    for i in range(0, len(hosts), 100):
        chunk = hosts[i:i+100]
        d = None
        for attempt, tmo in enumerate((25, 30, 35)):
            st, r = jreq("http://ip-api.com/batch?fields=query,proxy,hosting,isp,country",
                         "POST", chunk, timeout=tmo)
            if st == 200 and isinstance(r, list):
                d = r; break
            time.sleep(5 * (attempt + 1))
        if d is None:
            fails += 1
            print(f"[api] batch {i//100} 三次均失败, 该批标 unknown(不放行)")
        else:
            for x in d: verdict[x.get("query")] = x
        if i + 100 < len(hosts): time.sleep(4.5)   # 15 请求/分钟 -> 4.5s 安全间隔
    prime, normal, unk, dc, isp_dc = [], [], [], 0, 0
    for p in ip_list:
        info = verdict.get(p.split(":")[0])
        if info is None: unk.append(p); continue
        isp = info.get("isp")
        if info.get("hosting"): dc += 1
        elif _has(isp, DC_KW): isp_dc += 1
        elif _has(isp, RES_KW): prime.append(p)
        else: normal.append(p)
    print(f"[api] 质量筛: 住宅宽带{len(prime)} 普通{len(normal)} 机房{dc} "
          f"ISP名判机房{isp_dc} 未知{len(unk)} (失败批 {fails})")
    for p in prime[:10]:
        v = verdict.get(p.split(":")[0], {})
        print(f"   ★ {p} {v.get('country','')} {(v.get('isp') or '')[:38]}")
    return prime, normal, unk

# ===== 二级精筛: ipapi.is (08-30 用户实测打脸后加) =====
# ip-api 的 hosting 字段漏判严重。用户用 ping0 实测 178.130.47.21 是机房，
# ip-api 却报 hosting=False。换 ipapi.is 复核全池 11 个 IP:
#   178.130.47.21 / 45.95.232.35 / 147.45.60.139 / 193.25.215.182
#   / 199.66.182.243 / 199.66.183.226   → is_datacenter=True  ❌ 机房(ip-api 全漏判)
#   66.42.224.229 / 184.178.172.26 / 98.175.31.195 / 72.223.188.92 / 98.175.31.222 → 住宅 ✅
# 对照组(历史真过盾成功): 184.181.217.210 / 72.195.114.169 / 72.195.101.99 全部住宅 ✅
# → ipapi.is 的 is_datacenter 与「能否过盾」高度吻合, 作为送盾前最后一道闸。
# 免费额度 1000/天 单查(batch 端点要付费 key, 实测 POST 返回 403), ~1.6s/个。
# 每 45min 一轮 × 每轮 ≤25 个 = 800/天, 在配额内。
IPIS_MAX = int(os.environ.get("IPIS_MAX", "30"))   # 每轮精筛上限(护住 1000/天 配额)

def ipis_check(pxs):
    """对候选逐个查 ipapi.is。返回 (residential, datacenter, unknown) 三组 host:port。
    查不到的进 unknown(保留但排在住宅之后), 不因单点故障丢掉整批。"""
    res, dc, unk = [], [], []
    seen = {}
    for i, px in enumerate(pxs[:IPIS_MAX]):
        h = px.split(":")[0]
        if h in seen:                       # 同 IP 只查一次
            (res if seen[h] == 1 else dc if seen[h] == 0 else unk).append(px); continue
        verdict = None
        for attempt in range(2):
            try:
                r = U.Request(f"https://api.ipapi.is/?q={h}",
                              headers={"User-Agent": "Mozilla/5.0"})
                with U.urlopen(r, timeout=15) as resp:
                    d = json.loads(resp.read().decode())
                verdict = d; break
            except Exception as e:
                if attempt == 0: time.sleep(3)
                else: print(f"[ipis] {h} 查询失败: {str(e)[:50]}")
        if verdict is None:
            seen[h] = 2; unk.append(px); continue
        is_dc = bool(verdict.get("is_datacenter"))
        asn = (verdict.get("asn") or {})
        org = (asn.get("org") or (verdict.get("company") or {}).get("name") or "")
        if is_dc:
            seen[h] = 0; dc.append(px)
            print(f"[ipis] ❌ 机房 {h} {org[:34]}")
        else:
            seen[h] = 1; res.append(px)
            print(f"[ipis] ✅ 住宅 {h} {org[:34]}")
        time.sleep(1.2)   # 免费额度友好节流
    # 超出 IPIS_MAX 的部分不查, 直接归 unknown 尾部
    unk += [p for p in pxs[IPIS_MAX:]]
    print(f"[ipis] 精筛: 住宅 {len(res)} / 机房剔除 {len(dc)} / 未知 {len(unk)}")
    return res, dc, unk

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
    # ---- IP 级去重(关键): 免费列表里同一 IP 会挂几十个端口, 同 IP 不同端口的
    # Turnstile/CF 信誉几乎完全一致 -> 逐端口重测纯属白烧浏览器预算。
    # 实测: dead_pool 176 条只有 117 个唯一 IP, 118.145.128.100 一个 IP 烧了 23 个端口。
    dead_hosts_cnt = {}
    for p in dead:
        h = p.split(":")[0]
        dead_hosts_cnt[h] = dead_hosts_cnt.get(h, 0) + 1
    ban_hosts = {h for h, n in dead_hosts_cnt.items() if n >= 2}   # 同 IP 两个端口都废 -> 整 IP 拉黑
    good_hosts = {p.split(":")[0] for p in good}                   # 已有可用入口, 不再试它的其他端口
    cand = [p for p in all_px
            if p not in dead and p not in good
            and p.split(":")[0] not in ban_hosts
            and p.split(":")[0] not in good_hosts]
    print(f"[sift] 排除 dead {len(dead & all_px)} / 已good {len(good & all_px)} / "
          f"IP级拉黑 {len(ban_hosts)} 段 + 已好 {len(good_hosts)} 段, 候选 {len(cand)}")
    if len(cand) < 40:   # (源扩容后基本不触发) 高频滚动下源没刷新就没有新货, 提前收工省配额
        print(f"[sift] 新候选不足 40, 本轮跳过(不烧 ip-api 配额/浏览器预算)")
        open("sifted.txt", "w").close()
        sys.exit(0)
    random.shuffle(cand)
    # 流程反转(08-30): 源扩到 ~10 万后, ip-api 成了最贵一环(15 请求/分钟)。
    # 先用免费无限的 SOCKS5 握手把 6000 个候选压到几十个活的, 再花 ip-api 配额查质量。
    cand = cand[:6000]
    t0 = time.time()
    with ThreadPoolExecutor(128) as ex:
        results = dict(zip(cand, ex.map(socks5_ok, cand)))
    alive_raw = sorted([(ms, p) for p, (ok, ms) in results.items() if ok])
    print(f"[sift] 连通测 {len(cand)} 个 -> 活 {len(alive_raw)}, 耗时 {int(time.time()-t0)}s")
    # 同 IP 只留最快端口, 再送去查质量(省配额)
    seen0, dedup = set(), []
    for ms, p in alive_raw:
        h = p.split(":")[0]
        if h not in seen0: dedup.append(p); seen0.add(h)
    print(f"[sift] IP 去重后 {len(dedup)} 个进质量筛")
    prime, normal, unk0 = ip_quality(dedup[:900])
    def _by_ms(lst): return [p for ms, p in sorted((results[p][1], p) for p in lst)]
    # 一级(ip-api)排序后, 交二级(ipapi.is)精筛剔机房 —— ip-api 的 hosting 漏判太多
    stage1 = _by_ms(prime) + _by_ms(normal) + _by_ms(unk0)
    ipis_res, ipis_dc, ipis_unk = ipis_check(stage1)
    prime_set = set(prime)
    # 住宅确认的里面, ISP 白名单命中(Cox 等)再优先
    res_a = [p for p in ipis_res if p in prime_set]
    res_b = [p for p in ipis_res if p not in prime_set]
    picked = (res_a + res_b + ipis_unk)[:BROWSER_N]   # 机房(ipis_dc)彻底不送盾
    alive = picked
    print(f"[sift] 送盾队列: ★住宅+白名单 {len(res_a)} + 住宅 {len(res_b)} + "
          f"未知 {len(ipis_unk)} (机房剔除 {len(ipis_dc)}) -> 取前 {len(picked)}")
    if ipis_dc:
        # 机房 IP 直接写进 dead 由阶段2 累积; 这里只提示
        print("[sift] 已剔除机房: " + ", ".join(ipis_dc[:8]))
    with open("sifted.txt", "w") as f:
        f.write("\n".join(picked))
    for p in alive[:20]:
        print("  " + p)
    print(f"[sift] sifted.txt {len(picked)} 个, 交给阶段2真·试盾")
