#!/usr/bin/env python3
"""解密配置 → 输出到 stdout 的 export 行(被 workflow eval), 或 VPS 上直接生成 config.json。
用法: python3 cfg_open.py            # 打印 export KEY=VAL 行
      python3 cfg_open.py --write    # 写出 config.json(仅本地, 勿提交)
密文 config.enc (AES-256-CBC + PBKDF2 100k), 密钥: Actions Secret CONFIG_KEY / 本地 config.key"""
import os, subprocess, sys, json

def key_arg():
    if os.environ.get("CONFIG_KEY"):
        return ["-pass", "env:CONFIG_KEY"]
    if os.path.exists("config.key"):
        return ["-pass", "file:config.key"]
    print("缺密钥: 需要 CONFIG_KEY 环境变量或 config.key 文件", file=sys.stderr)
    sys.exit(3)

def open_config():
    out = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
         "-in", "config.enc", "-out", "/dev/stdout"] + key_arg(),
        capture_output=True)
    if out.returncode != 0:
        print("解密失败(密钥不对?)", file=sys.stderr); sys.exit(4)
    return json.loads(out.stdout.decode())

if __name__ == "__main__":
    cfg = open_config()
    if "--write" in sys.argv:
        open("config.json", "w").write(json.dumps(cfg, ensure_ascii=False, indent=2))
        print("config.json 已生成本地副本(勿提交/勿外传)")
    else:
        for k, v in cfg.items():
            if k != "note":
                print(f"export {k.upper()}={v}")
