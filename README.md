# good-ips — CF 盾优质 SOCKS5 IP 猎手

全自动流水线, 每 6 小时一滚(手动 dispatch 可加急): 免费代理源抓取 → IP 质量预筛(只留住宅/ISP) → SOCKS5 握手连通测 → UC 浏览器真·试 Turnstile → 出 token 的进优质池。

公开库 + 配置加密: 目标站 / Gist 等敏感配置 AES-256-CBC(PBKDF2 100k) 加密为 `config.enc` 提交, 密钥只存两处 —— **GitHub Secret `CONFIG_KEY`**(Actions 运行时解密) 和 **本地 VPS `config.key`**(离线解密), 仓库与日志中永不出现明文。

## 流水线

1. **Stage 1** `ip_sift.py`: proxyscrape + proxifly 拉免费 socks5(~350个) → ip-api batch 质量筛(`proxy=false && hosting=false`) → 64线程握手+连通实测(同 IP 多端口只留最快 2 个) → `sifted.txt`
2. **Stage 2** `sift_browser.py`: 逐个 `SINGLE_PROXY` 喂 `uc_ts.py`(xvfb + SeleniumBase UC 模式, 每个预算 110s)真·试盾 → 出 token 进 **good_pool**(cap 10) / 失败进 **dead_pool**(cap 600, 跨 run 排除不重测)

结果写 Gist(与注册机共享同池): `good_pool.txt` / `dead_pool.txt` / `good_proxy.txt`(最近过盾)。

## 用法

- 手动加急: Actions → sift → Run workflow(count=浏览器实测候选数, 默认 15)
- 自动: 每 6 小时一滚(GitHub schedule 有漂移, 实际 6~12h, 无碍)

## 本地解密(VPS)

```sh
# 密钥放同目录 config.key (chmod 600)
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -in config.enc -out config.json -pass file:config.key
```

## 坑位备忘

- ip-api batch 只认纯 IP, 传 `host:port` 整批无效
- GITHUB_TOKEN 的 grep 残段(带省略号)会 401, 要完整正则锚定
- 免费列表产出率: 住宅幸存者中约 1/13 过盾, 靠 6h 滚动累积
