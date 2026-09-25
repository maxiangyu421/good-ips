#!/usr/bin/env python3
"""诊断：用与 sift_browser.py 完全相同的方式（urllib + GIST_TOKEN）调 GitHub API，
把结果指纹写进 Gist probe_status.txt，供远端读取真实 401 原因。"""
import os, json, urllib.request as U, hashlib, time

print("[probe] GIST_TOKEN 前缀 6 =", os.environ.get("GIST_PREFIX", "?"), flush=True)


def jreq(url, method="GET", data=None, hdrs=None, timeout=20):
    h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if hdrs:
        h.update(hdrs)
    body = json.dumps(data).encode() if data is not None else None
    r = U.Request(url, data=body, headers=h, method=method)
    try:
        with U.urlopen(r, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode())
    except Exception as e:
        try:
            return getattr(e, "code", -1) or -1, json.loads(e.read().decode())
        except Exception:
            return -1, {"error": str(e)[:120]}


tok = os.environ["GIST_TOKEN"]
GID = "a9bf7eaa80c95fb337c7320fd47773dd"

st1, d1 = jreq("https://api.github.com/user", hdrs={"Authorization": "token " + tok})
report = {
    "ts": int(time.time()),
    "probe_from": os.environ.get("PROBE_FROM", "unknown-run"),
    "token_len": len(tok),
    "fingerprint": hashlib.sha256(tok.encode()).hexdigest()[:16],
    "user_status": st1,
    "user_login": (d1 or {}).get("login"),
    "user_message": (d1 or {}).get("message"),
}

st2, d2 = jreq(f"https://api.github.com/gists/{GID}",
               "PATCH", {"files": {"probe_status.txt": {"content": json.dumps(report, indent=1)}}},
               {"Authorization": "token " + tok})
report["patch_status"] = st2
report["patch_message"] = (d2 or {}).get("message")

print("[probe] user_status=%s login=%s" % (st1, report["user_login"]), flush=True)
print("[probe] patch_status=%s msg=%s" % (st2, report["patch_message"]), flush=True)
