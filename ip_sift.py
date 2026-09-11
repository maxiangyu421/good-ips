#!/usr/bin/env python3
"""阶段1: 免费socks5抓取 → ip-api 质量预筛(只留住宅/ISP) → SOCKS5握手+连通实测 → ipapi.is精筛 → ping0真实出口实测 → sifted.txt
不依赖浏览器; 阶段2(sift_browser.py)再用 uc_ts.py 逐个真·试盾。
排除名单/优质名单都存 Gist(dead_pool.txt / good_pool.txt), 跨 run 累积。"""
import os, sys, json, time, socket, struct, random, re, ssl
import urllib.request as U
from html import unescape
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
# 09-01 ipapi.is 匿名层改版: 检测字段(is_datacenter 等)全部移到 key 层, 匿名层只剩地理信息,
# 且 asn 变字符串(旧代码 .get 崩 = 连续 3 轮 failure 的根因)。免费账号(无需付费)给 key,
# 1000 次/天, 与每轮 30 × 32 轮/天 = 960 配额匹配。key 存加密 config 的 IPAPI_KEY。
IPIS_KEY = (_CFG.get("IPAPI_KEY") or os.environ.get("IPAPI_KEY") or "").strip()

def _ipis_org(verdict):
    """兼容 ipapi.is 新旧两代返回: 旧 asn={org:...}, 新 asn="AS22773 Cox..."(字符串),
    company 也可能是 str 或 dict。永远返回字符串, 不再 .get 崩溃。"""
    asn = verdict.get("asn")
    if isinstance(asn, dict):
        org = asn.get("org") or asn.get("descr") or ""
    elif isinstance(asn, str):
        org = asn
    else:
        org = ""
    if not org:
        comp = verdict.get("company")
        if isinstance(comp, dict): org = comp.get("name") or ""
        elif isinstance(comp, str): org = comp
    return str(org)

def _ipis_url(h):
    u = f"https://api.ipapi.is/?q={h}"
    if IPIS_KEY: u += f"&key={IPIS_KEY}"
    return u

def ipis_check(pxs):
    """对候选逐个查 ipapi.is。返回 (residential, datacenter, unknown) 三组 host:port。
    查不到的进 unknown(保留但排在住宅之后), 不因单点故障丢掉整批。
    09-01 起匿名层没有 is_datacenter —— 没 key 或返回里缺该字段时, 降级为
    「不精筛, 全部归住宅组」并打警告, 宁可多送几个也别把整轮流程搞崩。"""
    if not IPIS_KEY:
        print("[ipis] ⚠️ 未配置 IPAPI_KEY, 跳过二级精筛(全部按 ip-api 结果送盾)")
        return list(pxs), [], []
    res, dc, unk = [], [], []
    seen = {}
    degraded = False
    for i, px in enumerate(pxs[:IPIS_MAX]):
        h = px.split(":")[0]
        if h in seen:                       # 同 IP 只查一次
            (res if seen[h] == 1 else dc if seen[h] == 0 else unk).append(px); continue
        verdict = None
        for attempt in range(2):
            try:
                r = U.Request(_ipis_url(h),
                              headers={"User-Agent": "Mozilla/5.0"})
                with U.urlopen(r, timeout=15) as resp:
                    d = json.loads(resp.read().decode())
                verdict = d; break
            except Exception as e:
                if attempt == 0: time.sleep(3)
                else: print(f"[ipis] {h} 查询失败: {str(e)[:50]}")
        if verdict is None:
            seen[h] = 2; unk.append(px); continue
        if "is_datacenter" not in verdict:
            # 返回的是阉割版(匿名层格式): 无判据可用, 降级放行 + 只警告一次
            if not degraded:
                print("[ipis] ⚠️ 返回缺 is_datacenter(key 失效或被降级?), 本次跳过精筛防误杀")
                degraded = True
            seen[h] = 2; unk.append(px); continue
        is_dc = bool(verdict.get("is_datacenter"))
        org = _ipis_org(verdict)
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

# ===== 三级半精筛: ping0.cc 走代理实测「真实出口」类型 (09-07) =====
# ipapi.is 查的是列表 IP 的画像; 免费列表的真实出口可能不是列表 IP(落地/中转)。
# ping0 检查 = 用候选代理自身开 socks5 隧道访问 ping0.cc/ipleak, 直接问它:
#   家庭宽带IP ✅ / IDC机房IP ❌ —— 这是「CF 实际看到的出口」的类型, 与过盾最相关。
# 沙盒实测过的坑(全部归 unknown, 绝不误杀):
#   - ping0 对部分可疑出口弹 Turnstile 挑战页(challenge)
#   - 个别 CDN 边缘证书不标准 -> CERT_NONE(只读公开画像, 无敏感数据)
#   - 响应里 ipinfo 块偶发缺失 -> 重试一次, 仍无则 unknown
#   - 代理瞬断/SSL EOF 常见 -> 任何异常都 unknown
PING0_MAX = int(os.environ.get("PING0_MAX", "30"))   # 每轮实测上限(每代理一次隧道, 3~10s/个)
P0_DROP_KW = ("机房", "IDC", "数据中心", "datacenter", "广播", "保留IP", "bogon")
P0_GOOD_KW = ("家庭宽带", "住宅", "家宽")

def _ping0_tunnel(proxy, rip, timeout=18):
    """socks5 CONNECT(IP-ATYP, 这批代理拒域名ATYP) -> TLS(SNI=ping0.cc) -> GET /ipleak"""
    ph, pp = proxy.split(":")[0], int(proxy.split(":")[1])
    s = socket.create_connection((ph, pp), timeout=timeout); s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00": raise RuntimeError("greet")
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(rip) + struct.pack(">H", 443))
        r = s.recv(64)
        if len(r) < 2 or r[1] != 0: raise RuntimeError("connect")
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        t = ctx.wrap_socket(s, server_hostname="ping0.cc")
        t.sendall(b"GET /ipleak HTTP/1.1\r\nHost: ping0.cc\r\n"
                  b"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
                  b"Connection: close\r\n\r\n")
        buf = b""
        while True:
            c = t.recv(65536)
            if not c: break
            buf += c
            if len(buf) > 300000: break
        t.close()
    finally:
        try: s.close()
        except Exception: pass
    return buf.decode("utf-8", "ignore")

def _ping0_parse(html):
    if "cf-turnstile" in html.lower() or "aliyuncaptchaconfig" in html.lower():
        return None   # 挑战页, 无数据
    m = re.search(r"window\.ipinfo\s*=\s*\{(.*?)\}", html, re.DOTALL)
    if not m: return None
    out = {}
    for k in ("ip", "addr", "countrycode", "asn", "org", "iptype"):
        mm = re.search(r"\b%s:\s*'([^']*)'" % k, m.group(1))
        if mm: out[k] = unescape(mm.group(1))
    return out or None

def ping0_check(pxs):
    """走代理实测真实出口。返回 (pass, drop, unknown) 三组, 相对顺序保持不变。
    只有 ping0 明确标机房才淘汰; 挑战/失败/缺数据一律 unknown(保留送盾)。"""
    if not pxs: return [], [], []
    rip = None
    try:   # DoH 拿真实解析(防运行环境 DNS 污染/fake-ip), 失败再走系统 DNS
        st, d = jreq("https://1.1.1.1/dns-query?name=ping0.cc&type=A",
                     hdrs={"Accept": "application/dns-json"}, timeout=10)
        if st == 200:
            for a in (d.get("Answer") or []):
                if a.get("type") == 1: rip = a["data"]; break
    except Exception: pass
    if not rip:
        try: rip = socket.gethostbyname("ping0.cc")
        except Exception:
            print("[ping0] ⚠️ ping0.cc 解析失败, 本层跳过"); return list(pxs), [], []
    keep, drop, unk = [], [], []
    for px in pxs:
        info = None
        for attempt in range(2):
            try:
                info = _ping0_parse(_ping0_tunnel(px, rip))
                if info: break
            except Exception:
                if attempt == 0: time.sleep(1)
        if not info:
            unk.append(px)
            print(f"[ping0] ⚠️ {px.split(':')[0]} 挑战页/无数据/失败 -> 未知"); continue
        t = info.get("iptype") or ""
        tag = f"{t} {info.get('org','')[:30]}"
        eip = info.get("ip") or ""
        if eip and eip != px.split(":")[0]:
            tag += f" [出口≠列表:{eip}]"
        if any(k.lower() in t.lower() for k in P0_DROP_KW):
            drop.append(px); print(f"[ping0] ❌ {px.split(':')[0]} {tag}")
        else:
            mark = "✅" if any(k in t for k in P0_GOOD_KW) else "▫️"
            keep.append(px); print(f"[ping0] {mark} {px.split(':')[0]} {tag}")
        time.sleep(0.5)
    print(f"[ping0] 实测: 通过 {len(keep)} / 机房剔除 {len(drop)} / 未知 {len(unk)}")
    return keep, drop, unk

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
    # ---- 整段拉黑: 同 IP >=3 个端口在 dead 里就封整段(09-10 明星机制已删) ----
    # 已 good 的 IP 复验失败在 stage2 只降级 reserve 不进 dead, 好网段不会被误封。
    dead_hosts_cnt = {}
    for p in dead:
        h = p.split(":")[0]
        dead_hosts_cnt[h] = dead_hosts_cnt.get(h, 0) + 1
    ban_hosts = {h for h, n in dead_hosts_cnt.items() if n >= 3}
    good_hosts = {p.split(":")[0] for p in good}
    # 09-12 回炉通道: 删明星机制后 good IP 被 good_hosts 整体排除出候选,
    # 永远无法复验 -> meta 时间戳永不刷新 -> 12h 保鲜变全员死刑(good_pool 30→5 断崖根因)。
    # 现把「快到期」的 good IP 插队回炉, 过盾后 stage2 刷新 meta 续命;
    # 名额不挤占新 IP(stage2 名额只数新 IP)。窗口 (9h, 36h): 每轮测不完下轮继续, 不漏。
    REVERIFY_HOURS = int(os.environ.get("REVERIFY_HOURS", "9"))
    _demote_h = int(os.environ.get("DEMOTE_HOURS", "12"))
    try:
        _meta = json.loads(gist_file("good_pool_meta.json") or "{}")
    except Exception:
        _meta = {}
    _now = int(time.time())
    reverify = [p for p in good
                if _meta.get(p)
                and REVERIFY_HOURS * 3600 < _now - _meta.get(p) < (_demote_h + 24) * 3600]
    if reverify:
        print(f"[sift] 回炉复验 {len(reverify)} 个(超{REVERIFY_HOURS}h未复验): "
              + ", ".join(reverify))
    cand = [p for p in all_px
            if p not in dead and p not in good
            and p.split(":")[0] not in ban_hosts
            and p.split(":")[0] not in good_hosts]
    # ---- 种子队列: 外部投喂的代理(如别人分享的住宅列表)插队优先试盾 ----
    # 用法: 往 Gist 的 seed_queue.txt 写 host:port 每行一个; 试过一轮即清空。
    seed = [l.strip() for l in gist_file("seed_queue.txt").splitlines() if l.strip()]
    seed = [p for p in seed if p not in good]
    if seed:
        print(f"[sift] 种子队列 {len(seed)} 个插队(外部投喂)")
    cand = seed + reverify + cand    # 种子 > 回炉复验 > 常规候选
    print(f"[sift] 排除 dead {len(dead & all_px)} / 已good {len(good & all_px)} / "
          f"IP级拉黑 {len(ban_hosts)} 段 + 已好 {len(good_hosts)} 段, 候选 {len(cand)}")
    if len(cand) < 40 and not (seed or reverify):   # 源没新货可跳过, 但有回炉复验/种子时必须照跑(09-12), 否则复验通道失效
        print(f"[sift] 新候选不足 40, 本轮跳过(不烧 ip-api 配额/浏览器预算)")
        open("sifted.txt", "w").close()
        sys.exit(0)
    # 优先队列(种子)不参与 shuffle/截断, 保证一定被测到
    prio = seed + reverify
    rest = [p for p in cand if p not in set(prio)]
    random.shuffle(rest)
    # 流程反转(08-30): 源扩到 ~10 万后, ip-api 成了最贵一环(15 请求/分钟)。
    # 先用免费无限的 SOCKS5 握手把 6000 个候选压到几十个活的, 再花 ip-api 配额查质量。
    cand = prio + rest[:6000]
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
    prime_set, prio_set = set(prime), set(prio)
    # 排序: 优先队列(种子) > ISP白名单住宅 > 其他住宅 > 未知
    res_p = [p for p in ipis_res if p in prio_set]
    res_a = [p for p in ipis_res if p not in prio_set and p in prime_set]
    res_b = [p for p in ipis_res if p not in prio_set and p not in prime_set]
    pre_pick = res_p + res_a + res_b + ipis_unk      # 机房(ipis_dc)已彻底出局
    # 三级半: ping0 走代理实测真实出口, 明确标机房的不送盾(挑战/失败归未知照送)
    p0_keep, p0_drop, p0_unk = ping0_check(pre_pick[:PING0_MAX])
    picked = (p0_keep + p0_unk + pre_pick[PING0_MAX:])[:BROWSER_N]
    if res_p: print(f"[sift] 优先队列命中 {len(res_p)} 个进送盾队列头部")
    alive = picked
    print(f"[sift] 送盾队列: ping0通过 {len(p0_keep)} + ping0未知 {len(p0_unk)} + "
          f"未实测 {len(pre_pick) - PING0_MAX if len(pre_pick) > PING0_MAX else 0} "
          f"-> 取前 {len(picked)} (ping0剔除 {len(p0_drop)})")
    if ipis_dc or p0_drop:
        print("[sift] 已剔除机房: " + ", ".join((ipis_dc + p0_drop)[:10]))
    with open("sifted.txt", "w") as f:
        f.write("\n".join(picked))
    for p in alive[:20]:
        print("  " + p)
    print(f"[sift] sifted.txt {len(picked)} 个, 交给阶段2真·试盾")
