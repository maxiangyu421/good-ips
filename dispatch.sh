#!/bin/sh
# good-ips 定时 dispatch 驱动器 (systemd timer 每 45 分钟调一次)
# 规则: 上一轮还在跑就跳过; 静默失败只写日志
cd /root/good-ips
TOK=$(cat .gh_token)
LOG=/root/good-ips/dispatch.log

# 上一轮还在跑?
RUNNING=$(curl -s --max-time 15 -H "Authorization: Bearer $TOK" \
  "https://api.github.com/repos/maxiangyu421/good-ips/actions/runs?per_page=1" \
  | python3 -c "import json,sys;r=json.load(sys.stdin)['workflow_runs'][0];print(r['status'])" 2>/dev/null)
# 09-12: GitHub API 抖动/限流时上面会返回空串或报错, 旧写法当成「没在跑」直接再 dispatch
# -> 一轮没跑完又来一轮, 两个 job 抢同一批候选互相判死。状态查不到就这一拍不动作。
case "$RUNNING" in
  in_progress|queued)
    echo "$(date '+%m-%d %H:%M') skip (上一轮 $RUNNING)" >> $LOG
    exit 0 ;;
  completed)
    : ;;
  *)
    echo "$(date '+%m-%d %H:%M') skip (状态查询异常: '${RUNNING:-空}')" >> $LOG
    exit 0 ;;
esac

ST=$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOK" -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/maxiangyu421/good-ips/actions/workflows/sift.yml/dispatches \
  -d '{"ref":"main","inputs":{"count":"25"}}')
echo "$(date '+%m-%d %H:%M') dispatch count=25 http=$ST" >> $LOG
