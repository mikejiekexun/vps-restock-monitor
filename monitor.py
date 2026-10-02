#!/usr/bin/env python3
"""VPS restock watcher.

拉取商品页 -> 按 monitors.json 规则判断库存 -> 有货状态变化时推 Telegram。
仅用标准库。为 GitHub Actions 设计：
- watch 模式：一个 job 连续跑 ~4 小时，内部每 CHECK_INTERVAL 秒查一次，
  时间预算用完干净退出，由下一个调度窗口无缝接力；
- state.json 记录各目标上次库存，跨进程/跨 job 去重，只在状态变化时提交回仓库；
- 被 Cloudflare 拦截的站点通过 FlareSolverr（无头浏览器服务）抓取。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "40"))      # 每轮检查间隔（秒）
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "349"))         # 本进程最长运行分钟数
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "6"))   # 心跳消息间隔（小时）
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")   # Server酱 SendKey（微信推送，备用渠道）
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")   # PushPlus token（微信推送，备用渠道）
WEIXIN_HEARTBEAT = os.environ.get("WEIXIN_HEARTBEAT") == "1"  # 微信渠道是否也发心跳（默认只发补货警报）
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "")
ALLOW_STATE_COMMIT = os.environ.get("ALLOW_STATE_COMMIT") == "1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FLARE_SESSION = "vpsmon"
_flare_ready = False


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def visible_text(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _flare_post(payload):
    req = urllib.request.Request(
        FLARESOLVERR_URL.rstrip("/") + "/v1",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def flare_get(url):
    global _flare_ready
    if not FLARESOLVERR_URL:
        raise RuntimeError("页面被 Cloudflare 拦截，且未配置 FLARESOLVERR_URL")
    if not _flare_ready:
        _flare_post({"cmd": "sessions.create", "session": FLARE_SESSION})
        _flare_ready = True
    data = _flare_post({"cmd": "request.get", "url": url,
                        "session": FLARE_SESSION, "maxTimeout": 60000})
    if data.get("status") != "ok":
        _flare_post({"cmd": "sessions.destroy", "session": FLARE_SESSION})
        _flare_post({"cmd": "sessions.create", "session": FLARE_SESSION})
        data = _flare_post({"cmd": "request.get", "url": url,
                            "session": FLARE_SESSION, "maxTimeout": 60000})
        if data.get("status") != "ok":
            raise RuntimeError(f"FlareSolverr: {str(data.get('message'))[:120]}")
    return data["solution"]["response"]


def fetch_html(target):
    mode = target.get("fetch", "auto")
    if mode == "flare":
        return flare_get(target["url"])
    try:
        html = http_get(target["url"])
        low = html.lower()
        if "just a moment" in low or "cf-chl" in low or "cf-challenge" in low:
            raise RuntimeError("cloudflare challenge")
        return html
    except (urllib.error.HTTPError, RuntimeError) as e:
        if mode == "auto" and FLARESOLVERR_URL:
            log(f"[flare] {target['url']} 直连失败({e})，改走 FlareSolverr")
            return flare_get(target["url"])
        raise


def compute_stock(target, text=None):
    """返回库存数。count 模式取页面数字；badge 模式 0=缺货 1=有货；json 模式读接口字段。"""
    mode = target.get("stock", "count")
    if mode == "badge":
        return 0 if re.search(target["oos_pattern"], text) else 1
    if mode == "json":
        return compute_stock_json(target)
    m = re.search(target["pattern"], text)
    return int(m.group(1)) if m else None


# ---- panstar.ai JSON 接口（响应 AES-256-GCM 加密，密钥来自其前端 JS）----

PANSTAR_KEY = base64.b64decode("26lAIOdVLW74vJFTawTAiA79kXTtdlibFh49gt+j5Zk=")


def fetch_json(target):
    url = target["json_url"]
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        encrypted = r.headers.get("X-Api-Encrypt")
        iv = r.headers.get("X-Api-Encrypt-Iv")
        data = json.loads(r.read())
    if encrypted == "1" or isinstance(data.get("ciphertext"), str):
        if AESGCM is None:
            raise RuntimeError("需要 pip install cryptography 才能解密 panstar 响应")
        plain = AESGCM(PANSTAR_KEY).decrypt(base64.b64decode(iv),
                                            base64.b64decode(data["ciphertext"]), None)
        data = json.loads(plain)
    return data


def _plans_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "list", "records", "data"):
            if isinstance(data.get(k), list):
                return data[k]
    raise RuntimeError("plans 数据结构不认识，接口可能已改版")


def _sellable(plans):
    return [p for p in plans if not p.get("archived") and p.get("status", 0) == 0]


def compute_stock_json(target):
    plans = _sellable(_plans_list(fetch_json(target)))
    pick = target.get("json_pick", "cheapest")

    def fmt(p):
        return (f'{p.get("name")}: ${p.get("price") / 100:g}/月, '
                f'{p.get("cpu")}核/{p.get("memory")}M内存/{p.get("disk")}G盘/{p.get("traffic")}G流量')

    if pick == "plan_id":
        plan = next((p for p in plans if p.get("id") == target.get("plan_id")), None)
        if plan is None:
            raise RuntimeError(f'plan {target.get("plan_id")} 不存在或已下架')
        target["_detail"] = fmt(plan)
        return 1 if plan.get("stockAvailable") else 0
    if pick == "cheapest":
        best = min(plans, key=lambda p: p["price"] if isinstance(p.get("price"), (int, float)) else 1e18)
        target["_detail"] = fmt(best)
        return 1 if best.get("stockAvailable") else 0
    if pick == "count_in_stock":
        return sum(1 for p in plans if p.get("stockAvailable"))
    raise RuntimeError(f"未知 json_pick: {pick}")


def stock_text(target, n):
    return "有货（数量未知）" if target.get("stock", "count") == "badge" else f"{n} 可用"


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log("[tg] 未配置 TG_TOKEN/TG_CHAT，本应发送:", text.replace("\n", " | "))
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("[tg] 已发送")
    except Exception as e:
        log("[tg] 发送失败:", repr(e))


def weixin_send(title, body):
    """通过 Server酱 / PushPlus 推微信，两个都配置就都发。返回是否至少成功一个。"""
    sent = False
    if SERVERCHAN_KEY:
        try:
            data = urllib.parse.urlencode({"title": title[:32], "desp": body}).encode()
            req = urllib.request.Request(
                f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send", data=data)
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            if res.get("code") == 0:
                sent = True
                log("[wechat/Server酱] 已发送")
            else:
                log("[wechat/Server酱] 失败:", str(res)[:150])
        except Exception as e:
            log("[wechat/Server酱] 发送失败:", repr(e))
    if PUSHPLUS_TOKEN:
        try:
            payload = json.dumps({"token": PUSHPLUS_TOKEN, "title": title,
                                  "content": body, "template": "txt"}).encode()
            req = urllib.request.Request("https://www.pushplus.plus/send",
                                         data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            if res.get("code") == 200:
                sent = True
                log("[wechat/PushPlus] 已发送")
            else:
                log("[wechat/PushPlus] 失败:", str(res)[:150])
        except Exception as e:
            log("[wechat/PushPlus] 发送失败:", repr(e))
    if not (SERVERCHAN_KEY or PUSHPLUS_TOKEN):
        log("[wechat] 未配置微信推送渠道")
        return None
    return sent


def notify(title, body, weixin=True):
    """多渠道通知：Telegram + 微信（Server酱/PushPlus）。心跳类消息可指定 weixin=False 省额度。"""
    tg_send(f"{title}\n{body}" if body else title)
    if weixin:
        weixin_send(title, body)


def load_targets():
    with open(os.path.join(BASE_DIR, "monitors.json"), encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        with open(os.path.join(BASE_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_and_commit_state(state):
    path = os.path.join(BASE_DIR, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    if not ALLOW_STATE_COMMIT:
        return
    try:
        def git(*args):
            return subprocess.run(("git",) + args, capture_output=True, text=True)
        git("add", os.path.relpath(path))
        c = git("-c", "user.name=stock-bot", "-c",
                "user.email=actions@users.noreply.github.com",
                "commit", "-m", "stock state update")
        if c.returncode != 0:
            return  # 无变化或另一进程已提交
        git("pull", "--rebase", "-q")
        r = git("push", "-q", "origin", "HEAD")
        if r.returncode != 0:
            git("pull", "--rebase", "-q")
            git("push", "-q", "origin", "HEAD")
        log("[state] 已提交")
    except Exception as e:
        log("[state] 提交失败(不影响监控):", repr(e))


def main():
    once = "--once" in sys.argv
    targets = load_targets()
    prev = {t["name"]: load_state().get(t["name"]) for t in targets}
    if not once:
        notify("🟢 补货监控已启动", "盯住:\n" + "\n".join(
            f"· {t['name']}" for t in targets))
    start = time.time()
    last_beat = start
    while True:
        cache = {}
        dirty = False
        for t in targets:
            try:
                if t.get("stock") == "json":
                    n = compute_stock(t)
                else:
                    key = t["url"]
                    if key not in cache:
                        cache[key] = visible_text(fetch_html(t))
                    n = compute_stock(t, cache[key])
            except Exception as e:
                log(f"[net] {t['name']}: {e!r}")
                continue
            if n is None:
                log(f"[warn] {t['name']}: 未匹配到库存，页面可能改版")
                continue
            old = prev[t["name"]]
            threshold = t.get("notify_above", 0)
            if old is None:
                log(f"[init] {t['name']}: {n}")
            elif n > threshold >= old:
                detail = t.pop("_detail", "")
                extra = f"\n{detail}" if detail else ""
                notify(f"🚨 {t['name']} 补货啦: {stock_text(t, n)}！",
                       f"{extra}\n{t.get('buy_url', t.get('url', ''))}".strip())
            if old is None or n != old:
                prev[t["name"]] = n
                dirty = True
            log(f"[check] {t['name']}: {n}")
        if dirty:
            save_and_commit_state(prev)
        if once:
            break
        if time.time() - last_beat >= HEARTBEAT_HOURS * 3600:
            last_beat = time.time()
            notify("💓 心跳：监控运行中", "\n".join(
                f"· {t['name']}: {stock_text(t, prev[t['name']]) if prev[t['name']] is not None else '未知'}"
                for t in targets), weixin=WEIXIN_HEARTBEAT)
        if time.time() - start >= MAX_MINUTES * 60:
            log("时间预算用完，干净退出，等下一次调度接力")
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
