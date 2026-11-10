# 给 AI 的用法：kytdl 命令行

在项目目录下运行 `kytdl <命令> --json`（即 `kytdl.cmd`，等同在项目目录运行 `.venv\Scripts\python.exe -m ablesci`）。
输出是 JSON；退出码 0 = 正常，1 = 有事要用户决定或业务错误，2 = 服务没运行（先 `kytdl start`），3 = 没有权限（先 `kytdl ai on`）。

## 原则
- 静默：不弹窗。只有两种情况去找用户：要花积分（每篇超过免确认额度，或网站要求提高悬赏）；有结果（下载好了 / 拿不到 / 积分不够 / 需要登录）。
- 花积分只由用户决定：执行 `kytdl ai on --confirmed`、`kytdl confirm ... --confirmed` 之前，先把不带 `--confirmed` 时返回的规则或预览原样告诉用户。
- `kytdl verify` 只报告你在 PDF 页面图上亲眼读到的 DOI 和标题，不要猜，不要抄条目里的字段。

## 安装与第一次使用
```
install.cmd                  # 建 .venv、装依赖、运行 kytdl doctor（可重复运行）
kytdl doctor                 # 每项 ok=true 才继续；否则按 detail 处理
kytdl start                  # 后台启动服务（没有窗口）
kytdl ai on                  # 返回规则文字（默认每篇 ≤10 分不问）-> 原样告诉用户
kytdl ai on --confirmed      # 用户同意后；要改额度加 --auto-max N（0–50）
kytdl status                 # needs_user 为空 = 可以开始；否则按每行的 next 处理（通常是 kytdl login）
```
`kytdl login` 会打开科研通登录窗口，让用户自己扫码 / 输入验证码（这是命令行唯一会打开的窗口）。登录后 `kytdl sync`。
其它设置都不用动：AI 接管时，出版社页面要人工验证就直接改走科研通求助；免费全文 DOI 和标题都对的自动保留（投稿版、录用稿会注明）；
科研通要求提高悬赏时，在已同意的金额以内自动接受，超过才停下问用户。想开机自动运行：`kytdl autostart on`。

## 场景 1：用户给一个清单（推荐：边下边处理，不要干等）
```
kytdl get 清单.txt --no-wait --json   # 立刻返回 batch_id；清单也可以是 '-'（标准输入）或直接写 DOI / 参考文献文字
kytdl watch <batch_id> --json         # 放到后台运行（能逐行通知你的方式，如 Claude Code 的 Monitor）
```
`watch` 每有变化输出一行 JSON（`event=paper`：`id`、`status`、`note`；需要处理时带 `next`，最低悬赏不够时带 `site_min_reward`；
`done` 带本地 `path`），所有文献都有结果后输出 `event=finished` 并退出。收到一行就处理一行：done 的马上告诉用户或放到用户要的位置，
出错的按 `next` 处理或问用户，不必等整批结束。
不方便后台运行时，也可以 `kytdl get 清单.txt --json` 一次等到结束（最多 600 秒），过程中同样逐行把进度写到 stderr。

任何文献类型都可以求助（期刊论文、书籍章节、学位论文、书籍……）。非期刊文献在科研通的最低悬赏通常更高（章节 30、学位论文 50、书籍 200），
超过用户同意的金额时会停下（`needs_person`，`reason=min_reward_raised`），按 `next` 问用户。

逐篇看 `status`：
| status | 怎么做 |
|---|---|
| done | 把 `path` 告诉用户（`kytdl open <id>` 可替用户打开） |
| waiting_answer / downloading / adopting / screening | 后台继续；之后用 `kytdl inbox` 取结果 |
| needs_points | `kytdl confirm <batch_id>` 看预览 → 告诉用户要花多少 → 同意后加 `--confirmed` |
| needs_check | `kytdl pages <id> --pages 1` → 看图。科研通文件：`kytdl verify <id> --doi ... --title ... --confirmed`；免费全文文件：把读到的 DOI / 标题告诉用户，同意后 `kytdl resolve <id> keep --confirmed`（保留）或 `kytdl resolve <id> request --confirmed`（改走科研通） |
| needs_person | 把 `note` 告诉用户（例如网站要求更高悬赏：用户同意后 `kytdl confirm <id> --reward 30 --confirmed`） |
| not_requested | 看 `note`（例如已有免费全文，或不建议求助） |

## 场景 2：AI 自己找到清单
```
kytdl search "关键词" --open-access    # 或 --source pubmed
```
选好后先问用户“要下载这些吗”，同意后把 DOI 逐行传给 `kytdl get -`。

## 每次开始工作
```
kytdl inbox                         # 后台到达的结果
kytdl inbox --ack                   # 告诉用户之后标记为已告知
```

## 其它命令
- `kytdl status`、`kytdl events -n 20`、`kytdl batch <id>`、`kytdl result <id>`、`kytdl wait <id> --until done`
- `kytdl sync`：立刻只读同步一次“我的求助”
- `kytdl oa <DOI> [--download]`：只查 / 下载免费全文
- `kytdl email <地址>`：可选。问用户要一个用户本人的邮箱，只发给 OpenAlex / Crossref / Unpaywall，能多查一个免费全文来源（Unpaywall）
- `kytdl stop`：停止后台服务；`kytdl ai off`：关闭 AI 接管（恢复开启前的设置）

## 不会替用户做的
登录和验证码；驳回；超过每日上限、保留积分或已同意金额的花费；账号受限时继续求助。
