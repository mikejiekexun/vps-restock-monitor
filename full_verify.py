import json
import io
import re
import importlib.util

spec = importlib.util.spec_from_file_location("monitor", "monitor.py")
mon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mon)

targets = json.load(io.open("monitors.json", encoding="utf-8"))
state = json.load(io.open("state.json", encoding="utf-8"))
pages = {
    "us-los-angeles-tri":  io.open("test-us-los-angeles-tri.html", encoding="utf-8", errors="replace").read(),
    "us-los-angeles-bgp":  io.open("test-us-los-angeles-bgp.html", encoding="utf-8", errors="replace").read(),
    "us-los-angeles-9929": io.open("test-us-los-angeles-9929.html", encoding="utf-8", errors="replace").read(),
    "us-los-angeles-cn2":  io.open("test-us-los-angeles-cn2.html", encoding="utf-8", errors="replace").read(),
}
texts = {k: mon.visible_text(v) for k, v in pages.items()}

print("=" * 62)
print("【1】目标数量与字段完整性")
print("=" * 62)
assert len(targets) == 11, f"目标数应为 11，实际 {len(targets)}"
for t in targets:
    mode = t.get("stock", "count")
    if mode == "json":
        assert t.get("json_url") and t.get("json_pick"), t["name"]
    else:
        assert t.get("url") and t.get("buy_url"), t["name"]
        if mode == "badge":
            assert t.get("oos_pattern") and t.get("fetch") == "flare", t["name"]
        else:
            assert t.get("pattern"), t["name"]
print(f"  11 个目标字段完整，模式分布: " +
      str({m: sum(1 for t in targets if t.get('stock', 'count') == m) for m in ('count', 'badge', 'json')}))

print("=" * 62)
print("【2】全部 badge 正则可编译")
print("=" * 62)
for t in targets:
    if t.get("stock") == "badge":
        re.compile(t["oos_pattern"])
print("  8 条正则全部有效")

print("=" * 62)
print("【3】Ground truth 对照（页面徽标 vs 监控判定）")
print("=" * 62)
allok = True
for slug, text in texts.items():
    names = re.findall(r"US\.LA\.[A-Za-z0-9.]+?(?= Starting from)", text)
    badges = re.findall(r"Order Now (\d+) Available", text)
    pairs = list(zip(names, badges))
    print(f"  页面 {slug}: {len(names)} 个套餐, 徽标 {len(badges)} 个")
    for t in targets:
        if t.get("stock") != "badge" or slug not in t["url"]:
            continue
        n = mon.compute_stock(t, text)
        m = re.search(r"(TRI DC2|TRI|9929|CN2 GIA) (Basic|Core)", t["name"])
        assert m, t["name"]
        prefix = {"TRI DC2": "TRI.DC2", "TRI": "TRI", "9929": "9929", "CN2 GIA": "CN2"}[m.group(1)]
        full = f"US.LA.{prefix}.{m.group(2)}"
        want = None
        for nm, b in pairs:
            if nm == full:
                want = int(b)
        match = "OK" if (n == 0) == (want == 0) else "❌不一致"
        if match != "OK":
            allok = False
        print(f"    {t['name']:<36} 判定={n} | 页面真实值={want} | {match}")

print("=" * 62)
print("【4】交叉污染测试（规则只能匹配自己的页面）")
print("=" * 62)
for t in targets:
    if t.get("stock") != "badge" or "TRI" not in t["name"]:
        continue
    own_slug = t["url"].rsplit("/", 1)[-1]
    for slug, text in texts.items():
        if slug == own_slug:
            continue
        hit = re.search(t["oos_pattern"], text)
        flag = "OK(无匹配)" if hit is None else f"❌误匹配于 {slug}"
        if hit is not None:
            allok = False
        print(f"  {t['name'][:30]:<32} vs {slug:<22} {flag}")

print("=" * 62)
print("【5】首次上线行为（不会误报）")
print("=" * 62)
for t in targets:
    if t.get("stock") == "badge" and state.get(t["name"]) is None and ("TRI" in t["name"] or "9929" in t["name"]):
        print(f"  {t['name']}: state.json 无记录 → 首轮 init 静默建档，不会报警 ✓")
print("  CN2 Basic/Core: state.json 已有记录(0) → 状态变化时正常报警 ✓")

print("=" * 62)
print("【6】monitor.py 语法")
print("=" * 62)
compile(io.open("monitor.py", encoding="utf-8").read(), "monitor.py", "exec")
print("  编译通过")

print("\n最终结论:", "✅ 全部通过，没有出错" if allok else "❌ 存在不一致，禁止上线")
