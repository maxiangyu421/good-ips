#!/usr/bin/env python3
"""灰度探针(09-12): 对给定代理 × 给定 page_load_strategy 跑 uc_ts.py, 只打印结果。
刻意不写 Gist、不动任何池子 —— 纯诊断, 跑多少次都不会污染 good_pool/dead_pool。

用法(Actions workflow probe.yml 里):
  PROBE_LIST="72.195.101.99:4145" PROBE_ARMS="default:90,eager:150" python3 uc_probe.py
  - PROBE_LIST: 逗号分隔 host:port
  - PROBE_ARMS: 逗号分隔 策略:超时秒; 策略 default = 不设 UC_PLS(SB 默认 normal)
"""
import os, signal, subprocess, sys, time

LOG = "/tmp/probe_sub.log"


def run(cmd, env, timeout):
    try: os.remove(LOG)
    except Exception: pass
    t0 = time.time()
    with open(LOG, "wb") as fo:
        p = subprocess.Popen(cmd, env=env, start_new_session=True,
                             stdout=fo, stderr=subprocess.STDOUT)
        ok = True
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            ok = False
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception: pass
            try: p.wait(timeout=10)
            except Exception: pass
    try:
        out = open(LOG, "rb").read().decode(errors="replace")
    except Exception:
        out = ""
    return ok, time.time() - t0, out


def main():
    proxies = [p.strip() for p in os.environ.get("PROBE_LIST", "").split(",") if p.strip()]
    arms = []
    for a in os.environ.get("PROBE_ARMS", "default:90,eager:150").split(","):
        a = a.strip()
        if not a:
            continue
        name, _, tmo = a.partition(":")
        arms.append((name or "default", int(tmo or 150)))
    if not proxies:
        print("[probe] PROBE_LIST 为空, 无事可做"); return
    print("[probe] 代理 %s × 策略 %s" % (proxies, arms), flush=True)

    rows = []
    for px in proxies:
        for arm, tmo in arms:
            for f in ("ts_token.txt", "ts_proxy.txt", "uc_debug.png"):
                if os.path.exists(f): os.remove(f)
            if arm == "default":
                env = dict(os.environ, SINGLE_PROXY=px); env.pop("UC_PLS", None)
            else:
                env = dict(os.environ, SINGLE_PROXY=px, UC_PLS=arm)
            print("\n===== %s × %s (超时 %ds) =====" % (px, arm, tmo), flush=True)
            ok, used, out = run(["xvfb-run", "-a", sys.executable, "uc_ts.py"], env, tmo)
            for line in [l.rstrip() for l in out.splitlines() if l.strip()][-14:]:
                print("[sub] " + line[:220], flush=True)
            tok = ""
            if os.path.exists("ts_token.txt"):
                tok = open("ts_token.txt").read().strip()
            if not tok and os.path.exists("uc_debug.png"):
                try: os.rename("uc_debug.png", "probe_%s_%s.png" % (px.replace(":", "_"), arm))
                except Exception: pass
            st = ("✅ token_len=%d" % len(tok)) if tok else ("⏱ 超时" if not ok else "❌ 无 token")
            rows.append((px, arm, st, used))
            print("[probe] 结果 %s × %s -> %s (%.0fs)" % (px, arm, st, used), flush=True)

    print("\n===== 汇总 =====", flush=True)
    for px, arm, st, used in rows:
        print("%-24s %-8s %-18s %5.0fs" % (px, arm, st, used), flush=True)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("## 灰度探针\n\n| 代理 | pageLoadStrategy | 结果 | 耗时 |\n|---|---|---|---|\n")
            for px, arm, st, used in rows:
                f.write("| %s | %s | %s | %.0fs |\n" % (px, arm, st, used))


if __name__ == "__main__":
    main()
