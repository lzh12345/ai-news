# 中文 AI 资讯(翻译型 RSS)

抓一批**优质英文 AI 源**(OpenAI、TechCrunch AI、The Decoder、HuggingFace、Apple ML、Gary Marcus 等)→ 用 **DeepSeek 把非中文条目翻成中文** → 生成一个**中文 RSS feed**,发布到 **GitHub Pages**,你在任意 RSS 阅读器里订阅。

- **抓取和翻译都在 GitHub Actions(墙外)完成,你本地不用架梯子。**
- 纯标准库 Python,无第三方依赖;用你自己的 DeepSeek key(很便宜)。
- 已是中文的条目自动跳过翻译,翻译结果带 guid 缓存,**每次只翻新条目**,省 token。

> 另有一个可选的「推送到个人微信」变体(`ai_news_push.py`),见文末附录。

---

## 怎么工作

```
GitHub Actions(每 4 小时,墙外 runner)
  └─ python rss_translate.py
       ├─ 读 feeds.txt,抓所有源(RSS/Atom)
       ├─ 去重 + 按时间排序,取最新 60 条
       ├─ 非中文条目 → DeepSeek 译中(中文跳过;guid 缓存避免重译)
       └─ 生成 docs/feed.xml → 提交回仓库 → GitHub Pages 发布
  你 → 在 RSS 阅读器订阅 https://<你的用户名>.github.io/<仓库名>/feed.xml
```

---

## 部署清单

### 1. 建 GitHub 仓库(**Public**)
- 新建 repo(如 `ai-news`)→ 选 **Public**
- ⚠️ 免费版 **GitHub Pages 需要仓库是 Public**。这没问题:仓库里只有代码 + 公开的 AI 资讯,**DeepSeek key 存在加密的 Actions Secrets 里,不会进仓库、也不会泄露**。

### 2. 加 Secret
- repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- `DEEPSEEK_API_KEY` = 你的 `sk-...`(只需要这一个)

### 3. 推代码到 `main`
把本文件夹内容提交上去(`rss_translate.py`、`feeds.txt`、`.github/`、`docs/`、`config.sample.json`、`README.md`)。`.gitignore` 已确保本地密钥文件不会被提交。

### 4. 开启 GitHub Pages
- repo → **Settings** → **Pages** → Build and deployment → Source 选 **Deploy from a branch** → Branch 选 **main**、文件夹选 **/docs** → Save
- 几十秒后,你的 feed 地址就是:`https://<你的用户名>.github.io/<仓库名>/feed.xml`

### 5. 先手动跑一次
- repo → **Actions** → **AI RSS 翻译聚合** → **Run workflow**
- 跑完它会把最新 60 条翻译好的中文 feed 提交进 `docs/feed.xml`
- 之后每 4 小时自动刷新

### 6. 在 RSS 阅读器订阅
把上面的 feed 地址加进你的 RSS 阅读器即可。
- 用 **Inoreader / Feedly 这类云端阅读器**:由它们的墙外服务器抓 feed,完全没有访问问题,很多还支持新条目推送通知(≈ 早报推送)。
- 用**手机本地 RSS 阅读器**:`github.io` 国内一般能直接订阅(偶尔慢);若读不动,见下方「国内访问」。

---

## 改源 / 改频率

- **改源**:编辑 [feeds.txt](feeds.txt),一行一个 RSS 链接(`#` 开头是注释),提交到 `main` 即生效。
- **改刷新频率**:编辑 [.github/workflows/rss.yml](.github/workflows/rss.yml) 里的 cron(UTC)。例如每 2 小时 `0 */2 * * *`、每天早 8 点 `0 0 * * *`(=北京 08:00)。
- **改保留条数**:`rss_translate.py` 里 `DEFAULT_MAX_ITEMS`(默认 60),或运行时 `--limit N`。

---

## 本地测试(可选)

1. 复制 `config.sample.json` 为 `config.json`,填上 `DEEPSEEK_API_KEY`
2. 验证抓取/解析/翻译(只试译 3 条,不写文件):
   ```
   python rss_translate.py --dry-run
   ```
3. 真出一份 feed(写 `docs/feed.xml`):
   ```
   python rss_translate.py --limit 10
   ```

| 参数 | 作用 |
|---|---|
| `--dry-run` | 抓取 + 试译 3 条样本,打印不写文件 |
| `--no-translate` | 不翻译(原文聚合,排查抓取/解析用) |
| `--limit N` | feed 保留条数 |
| `--feeds 路径` | 用别的源清单 |

---

## 国内访问 feed 的几种情况

| 你怎么读 | 能不能直连 |
|---|---|
| Inoreader / Feedly 等**云端**阅读器 | ✅ 阅读器墙外抓,放哪都行 |
| 手机**本地**阅读器读 `github.io` | ⚠️ 一般能,偶尔慢/抽风 |
| 手机本地读 `raw.githubusercontent.com` | ❌ 常被污染,本方案没用它 |

万一你的本地阅读器读 `github.io` 不顺,可以再加一份 **Gitee(码云)Pages 镜像**(国内最稳,需实名);告诉我,我帮你把发布步骤加上。

---

## 注意

- 抓取在 GitHub runner(墙外)完成,即使某些源(如 Google/OpenAI)你本地打不开也不影响。
- GitHub cron 仅 UTC,高负载时段可能延迟几分钟。
- Actions 用内置 `GITHUB_TOKEN` 提交 feed,不会触发额外运行、不会死循环。
- `cache.json` 是翻译缓存(会被提交),让每次只翻新条目;删了它下次会全量重译一遍。

---

## 附录:可选的「推送到个人微信」变体

如果你**还想要**每天主动推到微信(而不只是 RSS 订阅),仓库里保留了 `ai_news_push.py`:
- 抓 [aihot](https://aihot.virxact.com) 的中文日报(已是中文)+ DeepSeek 速览 → 经 **Server酱 / PushPlus** 推进个人微信。
- 对应 workflow 是 `.github/workflows/daily-ai-news.yml`,已设为**仅手动触发**;要定时就取消其中 `schedule` 注释,并加 `SERVERCHAN_SENDKEY` 或 `PUSHPLUS_TOKEN` Secret。
- 它和 RSS 方案各自独立,可以只用一个,也可以都用。
