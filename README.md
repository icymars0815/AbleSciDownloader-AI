# 科研通下载器 AI 版（AbleSciDownloader-AI）

让 AI 助手（Claude Code、Codex 等能运行命令的 AI）替你把文献下载下来。你只需要告诉 AI 要哪些文献；
它先找合法的免费全文，找不到再用**你自己的**科研通账户发求助、等应助、下载、核对、采纳。
全程没有窗口、不弹通知，只有两种情况会通过 AI 找你：**要花积分**（超过你同意的额度）和**有结果**（下载好了 / 拿不到 / 积分不够 / 需要登录）。

> 第三方开源工具（GNU AGPL-3.0，免费），不是科研通（ablesci.com）官方程序，与科研通无任何隶属或授权关系；付费购买即属受骗。
> 不使用 Sci-Hub 等影子图书馆。请遵守著作权法、出版商许可、所在机构规定和科研通规则。使用前请读 [DISCLAIMER.md](DISCLAIMER.md)。
> 这是 [AbleSciDownloader](https://github.com/icymars0815/AbleSciDownloader)（带图形界面的旧版，已停止维护）的命令行继任版本。

## 它做什么

```
你：帮我下载这几篇 → AI 运行 kytdl get
                         ├─ 免费全文（OpenAlex / Unpaywall / Europe PMC / PMC / arXiv …）→ 下载并核对 DOI、标题
                         └─ 找不到 → 用你的科研通账户发求助（每篇 ≤ 你设的免确认额度，默认 10 分）
                                      → 检查应助（发布后 1 分钟×5、5 分钟×5，再逐步放慢）
                                      → 下载 → 核对 DOI/标题/作者/摘要 → 吻合则自动采纳
AI ← kytdl inbox：后台到达的结果
```

不会替你做的事：登录和验证码、驳回、超过每日上限或保留积分的花费、超过你同意金额的悬赏。

## 安装（Windows 10/11）

需要：Python 3.10 或更高；Google Chrome 或 Microsoft Edge（Windows 自带 Edge，所以不用另外下载浏览器）。

```bat
git clone https://github.com/icymars0815/AbleSciDownloader-AI.git
cd AbleSciDownloader-AI
install.cmd
```

`install.cmd` 会在本目录建 `.venv`、安装 4 个依赖（playwright、PyMuPDF、certifi、olefile），最后运行 `kytdl doctor` 自检。
没有 Python 时它会提示安装方法（例如 `winget install -e --id Python.Python.3.13`）。重复运行是安全的。

也可以直接把这句话交给 AI：

> 把 https://github.com/icymars0815/AbleSciDownloader-AI 克隆到本机，运行 install.cmd，然后按 docs/CLI_FOR_AI.md 帮我设置。

## 第一次使用（一次性的三步）

以下命令都在项目目录里运行（`kytdl` 即 `kytdl.cmd`）：

```bat
kytdl start          :: 后台启动服务（没有窗口）
kytdl login          :: 打开科研通登录窗口，你自己扫码/输验证码，只需一次
kytdl ai on          :: 显示 AI 接管规则；你同意后：kytdl ai on --confirmed
```

`kytdl ai on --confirmed` 是唯一需要的设置：每篇不超过 10 分的求助不再逐条问你（`--auto-max N` 可改为 0–50，0 表示每篇都问），
每日花费上限 200 分，保留 50 分。`kytdl status` 里 `needs_user` 为空就表示一切就绪。想开机自动运行：`kytdl autostart on`。

## 日常：交给 AI

两种场景：

1. **你给清单**：“帮我下载这些” → AI 运行 `kytdl get 清单.txt --no-wait`（或直接传 DOI / 参考文献文字），再在后台运行 `kytdl watch <批次>`：
   每篇有进展就输出一行，下载好的 AI 马上告诉你（或放到你指定的文件夹），出问题的那一篇 AI 立刻处理或问你，不用等整批结束。
2. **AI 帮你找**：AI 用 `kytdl search "关键词"` 找文献，问你“要下载这些吗”，你同意后再 `kytdl get`。

期刊论文、书籍章节、学位论文、书籍都可以求助；非期刊文献科研通要的积分通常更高，超过你同意的额度时会先问你。
之后 AI 每次开始工作先看 `kytdl inbox`，把后台新到的结果告诉你。AI 的完整用法见 [docs/CLI_FOR_AI.md](docs/CLI_FOR_AI.md)。

## 常用命令

| 命令 | 作用 |
|---|---|
| `kytdl doctor` | 检查安装（Python、依赖、浏览器、数据目录） |
| `kytdl start` / `kytdl stop` | 启动 / 停止后台服务 |
| `kytdl status` | 服务、积分，以及需要你处理的事（`needs_user`） |
| `kytdl get <清单\|DOI\|-> [--no-wait]` | 下载一批文献（等待结果，或立刻返回批次号） |
| `kytdl watch <批次\|id>` | 实时进展：每有变化输出一行，全部有结果后退出 |
| `kytdl inbox [--ack]` | 后台到达的结果 |
| `kytdl confirm <批次> [--confirmed]` | 预览 / 确认超出免确认额度的积分 |
| `kytdl open <id> [--folder]` | 用系统程序打开下载好的 PDF |
| `kytdl ai [on\|off]` | AI 接管的状态 / 开启 / 关闭 |
| `kytdl email <地址>` | 可选：给免费全文服务的联系邮箱（多查 Unpaywall 一个来源） |

所有命令加 `--json` 输出机器可读结果；退出码 0 正常，1 需要用户决定或业务错误，2 服务没运行，3 没有权限。

## 数据与隐私

- 服务只监听 `127.0.0.1`。数据全部在本目录的 `data/`（队列、PDF、浏览器登录信息、日志），不上传到任何地方。
- 免费全文查询只把 DOI 发给公开书目服务（OpenAlex、Crossref、Unpaywall 等）。
- 不保存科研通密码；登录由你在浏览器窗口里亲自完成。
- 从旧版迁移：停掉旧版后，把旧目录的 `data/` 整个复制到本目录即可（数据格式相同）。

## 安装方式评估

| 方式 | 结论 |
|---|---|
| **git clone + install.cmd**（当前） | 采用。一步完成、可重复运行；依赖只有 4 个包；使用系统已有的 Edge/Chrome，不用下载浏览器；AI 也能独立完成。 |
| `pip` / `uv tool install` 装成全局命令 | 暂不采用。程序把数据放在程序目录的 `data/`，装进 site-packages 会把数据放错地方；要先把数据目录移到 `%LOCALAPPDATA%`。 |
| 免安装 ZIP（随带 Python） | 不再提供。旧版做过，包大、维护成本高；AI 场景下 clone + install.cmd 更简单。 |
| Playwright 自带 Chromium | 仅作兜底：电脑上既没有 Chrome 也没有 Edge 时，运行 `.venv\Scripts\python.exe -m playwright install chromium`（约 200 MB）。 |

## 开发

目录结构：

```
AbleSciDownloader-AI/
├─ kytdl.cmd / install.cmd    入口：命令行、一步安装
├─ ablesci/                   全部代码（python -m ablesci <命令> 等同 kytdl）
│  ├─ cli/        kytdl 命令行与本机接口客户端
│  ├─ server/     后台服务（只监听 127.0.0.1）与各接口路由
│  ├─ flow/       流程：队列存储、自动流程、后台监控、收件箱通知、设置与花费上限
│  ├─ website/    科研通网页操作：登录、发布、下载、采纳/驳回规则、账户与凭据
│  ├─ papers/     文献：免费全文、书目核对、文献检索、清单导入、Zotero
│  └─ system/     日志、开机自启、单实例锁、Python/浏览器路径
├─ tests/                     单元测试（只用临时数据和虚构文献）
├─ docs/CLI_FOR_AI.md         给 AI 的用法
└─ data/                      运行时生成：你的队列、PDF、登录信息、日志（不进版本库）
```

```bat
.venv\Scripts\python.exe -m unittest discover -s tests
```

开发规则见 [AGENTS.md](AGENTS.md)。许可证：GNU AGPL-3.0（见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。
