#!/usr/bin/env python3
"""本机直连测 good_pool 每个代理的本地可用性(09-12)。

和 VPS 面板的存活探测是两回事:
  - VPS(香港)面板判「此刻活着」= 从香港能不能握手;
  - 本脚本从【你当前设备】直接连, 回答的是「这个 IP 在我这台机器上能不能用」。
国内家宽到美国家宽代理的可达性和香港差异很大, 两个数字都对但含义不同。

用法:
  python3 live_check.py                 # 测 Gist 里的 good_pool
  python3 live_check.py 1.2.3.4:1080    # 测指定代理
  python3 live_check.py --list          # 只列池子不测
输出按延迟升序, 末尾给可用/不可用汇总。
"""
import json
import os
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

GIST_ID = "a9bf7eaa80c95fb337c7320fd47773dd"


def _fetch_pool():
    """从 Gist 拉 good_pool.txt(优先), 失败则读本地缓存 good_pool.txt。
    Gist 响应偶尔被截断(IncompleteRead), 所以重试 3 次再放弃。"""
    tok = ""
    for p in ("/tmp/gh_token", os.path.join(os.path.dirname(__file__), ".gist_token")):
        if os.path.exists(p):
            tok = open(p).read().strip()
            if tok:
                break
    if tok:
        import urllib.request as U
        for attempt in range(3):
            try:
                r = U.Request(f"https://api.github.com/gists/{GIST_ID}",
                              headers={"Authorization": "Bearer " + tok})
                with U.urlopen(r, timeout=25) as res:
                    d = json.loads(res.read().decode())
                c = (d.get("files", {}).get("good_pool.txt", {}) or {}).get("content") or ""
                out = [l.strip() for l in c.splitlines() if l.strip()]
                if out:
                    return out, "Gist"
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[warn] Gist 拉取失败({str(e)[:60]}), 尝试本地副本", file=sys.stderr)
                else:
                    time.sleep(2)
    for p in ("/tmp/good_pool.txt", os.path.join(os.path.dirname(__file__), "good_pool.txt")):
        if os.path.exists(p):
            out = [l.strip() for l in open(p) if l.strip()]
            if out:
                return out, f"本地 {p}"
    return [], "无来源"


def _socks5_probe(target, timeout=8):
    """完整 SOCKS5 握手 + 连到 1.1.1.1:80。返回 (ok, ms, 说明)。
    IP-ATYP 直连(免费代理普遍拒域名 ATYP, 与 stage1 判活方式一致)。"""
    try:
        host, port = target.rsplit(":", 1)
        port = int(port)
    except Exception:
        return False, 0, "格式错"
    t0 = time.time()
    s = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            return False, 0, "握手被拒"
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("1.1.1.1") + struct.pack(">H", 80))
        r = s.recv(64)
        if len(r) < 2:
            return False, 0, "无响应"
        if r[1] != 0:
            # 0x05 = 连接被拒(出口不通/目标不可达)
            return False, 0, f"CONNECT 失败(code={r[1]})"
        return True, int((time.time() - t0) * 1000), "OK"
    except socket.timeout:
        return False, 0, "超时"
    except OSError as e:
        return False, 0, f"网络错({str(e)[:30]})"
    except Exception as e:
        return False, 0, str(e)[:30]
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def main():
    args = [a for a in sys.argv[1:] if a != "--list"]
    if args:
        pool, src = args, "命令行"
    else:
        pool, src = _fetch_pool()
    if not pool:
        print("没有可测的代理(池子空或没拿到)。")
        return 1
    print(f"来源: {src} | 共 {len(pool)} 个代理")
    if "--list" in sys.argv:
        for p in pool:
            print("  " + p)
        return 0
    print("从本机直连测试(与 VPS 面板的『香港可达性』不是同一回事)…")

    def _probe(p):
        ok, ms, note = _socks5_probe(p)
        return ok, ms, note, p

    t0 = time.time()
    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(_probe, pool))
    ok = [r for r in results if r[0]]
    bad = [r for r in results if not r[0]]
    ok.sort(key=lambda x: x[1])
    print(f"\n=== 可用 {len(ok)} / {len(pool)} (耗时 {time.time()-t0:.1f}s) ===")
    for _ok, ms, _note, p in ok:
        print(f"  ✅ {p:26s} {ms:5d}ms")
    if bad:
        print(f"\n=== 不可用 {len(bad)} ===")
        for _ok, _ms, note, p in bad:
            print(f"  ❌ {p:26s} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
