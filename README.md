## 🆓 Scyed 自动续期（GitHub Actions，纯 HTTP 单账号版）

定时续期 [scyed.com](https://scyed.com/) 免费服（点一次 +30 天），`requests` 直调 Next.js Server Action，无浏览器。

### 原理

- `POST /de/gameserver/{ID}/upgrade/freeServer?extend=30`（`next-action` 头 + Cookie），成功后 GET 回页面抓德语日期（`Neues Ablaufdatum`）验+30 天。
- **哈希自动探测**：每次从升级页 HTML 正则捞 40 位候选逐个试（404 即换，Secrets 的 `NEXT_ACTION` 优先试一次），站改版无需手动更新；探测全灭才告警人工抓包。
- 三态：成功 / 冷却中（`wait X hour`，ok 上报，下次 cron 再续）/ 失败（401/403 即 Cookie 失效告警）。
- 站点有 Cloudflare，直连 403，必须挂代理（sing-box）。

### 🔐 Secrets 配置说明

| Secret 名称       | 是否必填 | 说明 |
|-------------------|----------|------|
| SCYED_COOKIE      | ✅ 必填  | scyed.com 的 Cookie 串（至少含 `__Secure-better-auth.session_token`，约 7 天命，Discord 登录） |
| SCYED_SERVER_IDS  | ✅ 必填  | 服务器 ID（面板 URL 里那段，如 `f8f9d4fa`；多服逗号分隔） |
| DISCORD_TOKEN     | ❌ 可选* | Discord Token；*配了则 Cookie 401/403 时自动 OAuth 重登并回写，实现免维护 |
| GH_TOKEN          | ❌ 可选  | GitHub classic PAT（重登成功后自动回写 `SCYED_COOKIE` 用，需与 DISCORD_TOKEN 同配） |
| NEXT_ACTION       | ❌ 可选  | 手动指定的哈希（优先试一次）；不填则纯自动探测 |
| NODE_LINK         | ✅ 必填* | 代理链接（vless/vmess/…），*除非配了 `SCYED_PROXY` |
| SCYED_PROXY       | ❌ 可选  | 显式代理（如 `http://127.0.0.1:7890`），优先级最高 |
| EMAIL             | ❌ 可选  | 通知备注名（脱敏显示） |
| TG_BOT_TOKEN      | ❌ 可选  | Telegram Bot Token |
| TG_CHAT_ID        | ❌ 可选  | Telegram Chat ID |

> Cookie 获取：登录后 F12 → Application → Cookies → `scyed.com`，全选复制（`session_token` 保持原样）。
> 免维护链：Cookie 401/403 → 用 `DISCORD_TOKEN` 走 OAuth 重登 → 新 session 回写 `SCYED_COOKIE`（需 `GH_TOKEN`）→ 重试续期。都没配则 TG 告警手动重拷。

### 部署步骤

1. 仓库（`scyed-renew`）推 `app.py`、`.github/workflows/renew.yml`、`README.md`，Actions 开工作流权限。
2. 配 Secrets，手动跑一次，看 TG/日志确认日期+30 天。
3. cron 默认每天 UTC 10 点/18 点（间隔 8 小时，天然避开 1 小时续期冷却）。

## ⚠️ 免责声明

仅供学习了解；遵守相关法律法规，作者不对不当行为负责。
