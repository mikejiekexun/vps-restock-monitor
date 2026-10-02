# vps-restock-monitor

跑在 GitHub Actions 上的 VPS 补货监控：云端 7×24 值守，补货即时推送 Telegram，本机不用开机。

## 它在干什么

- `watch`：UTC 每 4 小时启动一个值守 job，内部每 40 秒抓一次页面，连续跑约 4 小时 05 分后干净退出；窗口之间重叠几分钟，实现无缝接力。
- `quickcheck`：每 30 分钟单点查一次，防止值守窗口因 GitHub 排队延迟出现空隙。
- `keepalive`：每月 1 号提交一个空 commit，防止 GitHub 因 60 天无活动自动停用定时任务。
- 补货（缺货 → 有货）→ Telegram 推送购买链接；每 6 小时发一次心跳证明活着。
- `state.json`：仓库里记录每个目标的上次库存状态，跨 job 去重（状态不变绝不重复轰炸），状态变化时自动提交回仓库。

## 两种库存判断模式

- **count 模式**（如 Lamhost）：页面始终显示「N 可用」，正则第一个捕获组 = 数量，库存 > `notify_above` 判定有货。
- **badge 模式**（如 VMISS）：页面只在**缺货**时显示「0 Available / 缺货」徽标，有货时什么都不显示。正则 = 套餐名 + 窗口 + 缺货徽标，匹配到 = 0，匹配不到 = 1。
- **json 模式**（如 Panstar）：直接读站点的公开 JSON 接口里的库存字段，最精准。支持响应解密（panstar 用 AES-GCM，密钥取自其前端 JS）。`json_pick` 三种取法：`plan_id`（盯指定套餐，配 `plan_id`）、`cheapest`（盯最低价套餐）、`count_in_stock`（任意套餐有货就报）。

## 部署步骤（GitHub 网页即可完成）

1. 在 GitHub 新建一个 **public** 仓库（public 才有免费 Actions 额度）。
2. 上传以下文件并保持目录结构：`monitor.py`、`monitors.json`、`state.json`（首次可空 `{}`）、`.github/workflows/watch.yml`、`quickcheck.yml`、`keepalive.yml`。
3. 仓库 Settings → Secrets and variables → Actions → New repository secret，添加两条：
   - `TG_TOKEN`：@BotFather 创建机器人后给的 token（形如 `123456:ABC-xxx`）
   - `TG_CHAT`：你自己的数字 chat id
4. Actions 页面 → watch → Run workflow 手动跑一次验证：收到「🟢 补货监控已启动」即成功。

## 修改监控目标

编辑 `monitors.json`，每个条目：

```json
{
  "name": "显示名称",
  "url": "商品/分类页 URL（count/badge 模式用）",
  "pattern": "count 模式：提取库存数字的正则",
  "oos_pattern": "badge 模式：匹配到即缺货的正则",
  "stock": "count（默认）或 badge 或 json",
  "json_url": "json 模式：接口地址",
  "json_decrypt": "panstar（可选，解密加密响应）",
  "json_pick": "cheapest / plan_id / count_in_stock",
  "plan_id": 432,
  "fetch": "direct（默认）/ flare（过 Cloudflare 用）/ auto（直连失败自动换 flare）",
  "buy_url": "通知里附带的购买链接",
  "notify_above": 0
}
```

json 模式依赖 `cryptography` 包（Actions 工作流里已自动安装；本地调试请先 `pip install cryptography`）。

同一个 URL 的多个目标每轮只抓一次页面。被盾的站（fetch=flare）依赖 Actions 里的 FlareSolverr 服务容器，本地调试没有它时这些目标会跳过。

## 已知边界

- FlareSolverr 过盾不是 100% 稳定，Cloudflare 升级挑战时可能失效，需要换 Playwright 方案。
- GitHub 定时任务高峰期可能延迟几分钟；watch 重叠接力 + quickcheck 双保险就是为此设计。
- TG_TOKEN / PAT 等敏感信息只放 Secrets，绝不写进文件。
