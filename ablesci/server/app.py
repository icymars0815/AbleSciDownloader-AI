"""The local background service (127.0.0.1 only). Started by `kytdl start` as: python -m ablesci.server.app"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import argparse
import csv
import io
import json
import os
import secrets
import threading

from ablesci.website.accounts import Accounts
from ablesci.papers.ai_service import AIService
from ablesci.server.app_common import ROOT, open_with_system, write_token
from ablesci.system.autostart import Autostart
from ablesci.website.browser_agent import BrowserAgent
from ablesci.flow.core import Store
from ablesci.website.credential_vault import AIKeyVault, OpenAlexKeyVault
from ablesci.website.download_policy import RULES_VERSION, DOWNLOAD_DEFAULTS, DOWNLOAD_RANGES, FORCE_DEFAULT_POINTS, download_settings, migrate_preferences, resolve_highspeed, chosen_mode
from ablesci.system.instance_lock import InstanceLock, AlreadyRunning
from ablesci.server.mcp_api import McpApi, PermissionDenied
from ablesci.flow.monitor import Monitor
from ablesci.flow.notifications import Notifier
from ablesci.flow.pipeline import PIPELINE_RULES_VERSION, Pipeline, stage_of, tier, gate, next_step, rules_ok, has_session, acquisition_state
from ablesci.flow.ratelimit import LIMIT_DEFAULTS, LIMIT_RANGES, limit_settings
from ablesci.website.rejection_policy import rejection_decision, reject_rules
from ablesci.flow.task_states import workflow_state
from ablesci.server import api_routes
from ablesci.server import app_info
from ablesci.system import applog
from ablesci.flow import deletion
from ablesci.flow import intake
from ablesci.flow import notifications
from ablesci.website import publish_policy as pp
from ablesci.flow import settings as app_settings
from ablesci.papers import zotero_bridge


def make_server(port=8769, data_root=None, startup_dir=None, start_monitor=True, auto_screen=True):
    """No Startup-folder access happens here unless asked through the autostart route.
    An isolated server (custom data_root) may only touch the Startup folder if the caller passes one explicitly."""
    fresh_install = not ((Path(data_root) if data_root else ROOT / "data") / "queue.json").exists()
    store = Store(data_root or ROOT / "data")
    accounts = Accounts(store.root)
    agent = BrowserAgent(store, accounts, use_worker=True)
    ai = AIService(AIKeyVault(store.root))
    openalex_vault = OpenAlexKeyVault(store.root)
    ai.restore(store.snapshot().get("preferences", {}).get("ai_settings"))
    report_cache = {}   # the last previewed report: "让 AI 分析" sends exactly this text, nothing else
    pipeline = Pipeline(store, accounts, agent)
    notifier = Notifier(store)
    pipeline.notifier = notifier
    agent.notifier = notifier
    pipeline.auto_screen = auto_screen
    monitor = Monitor(store, accounts, agent, pipeline, notifier, start=start_monitor)
    autostart = Autostart(ROOT, startup_dir, allowed=data_root is None or startup_dir is not None)

    def session_now():
        snap = store.snapshot()
        mine = [i for i in snap["items"] if i.get("account_id", "default") == accounts.active_id]
        return has_session(snap.get("account_metrics", {}).get(accounts.active_id, {}), mine, accounts.has_cookie_file())
    mcp_api = McpApi(store, accounts, pipeline, monitor, session_now)
    with store.lock:
        store.data.setdefault("account_metrics", {}).setdefault("default", dict(store.data.get("account", {})))
        for item in store.data["items"]:
            if item.get("source") != "demo": item.setdefault("account_id", "default")
        store.save()
    migrate_preferences(store)
    if accounts.recovered:
        store.event("账户列表文件无法读取，已从备份恢复。请核对当前账户。", kind="needs_action")
    applog.rotate_server_logs(store.root / "logs")
    applog.trim_screenshots(store.root / "screenshots")
    if store.snapshot().get("preferences", {}).get("pipeline_restart_notice"):
        text = "自动流程已在重启后关闭。"
        store.event(text, kind="needs_action", action="pipeline-enable")
        try:
            notifier.notify("needs_action", "account:pipeline-restart", "自动流程已关闭", text)
        except Exception:
            pass
    if not fresh_install and "disclaimer_ack" not in store.snapshot().get("preferences", {}):
        store.update_preferences(disclaimer_ack="upgrade")   # an existing installation is not interrupted by the first-run dialog
    token = secrets.token_urlsafe(32)
    write_token(store.root, token)   # the command line reads it; only this Windows user can read data/

    def shutdown_service(server_ref):
        agent.request_stop()
        pipeline.close()
        monitor.close()
        agent.commands.put(("close", None, None))
        def close_service():
            agent.thread.join(timeout=50)
            server_ref.shutdown()
        threading.Thread(target=close_service, daemon=True).start()

    def handle_verification(item_id, allow_website=True):
        item = store.get(item_id)
        decision = rejection_decision(item, store.snapshot().get("preferences", {}))
        if not allow_website and decision["action"] == "auto_reject":
            decision.update(action="ask_user", reason="仅完成本地核对，未提交驳回。")
        store.update(item_id, rejection=decision)
        if decision["action"] == "auto_reject" and allow_website:
            try:
                agent.enqueue("reject_auto", item_id)
            except ValueError:
                store.event("检测到文件与求助明确不符，但浏览器正忙，未提交驳回。请稍后重新核对或手动处理。")

    ctx = SimpleNamespace(store=store, accounts=accounts, agent=agent, ai=ai, openalex_vault=openalex_vault, report_cache=report_cache, pipeline=pipeline, notifier=notifier, monitor=monitor, autostart=autostart, session_now=session_now, mcp_api=mcp_api, shutdown_service=shutdown_service, handle_verification=handle_verification)   # what the api_* routes may use

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            self.responded = True
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "仅允许本地地址访问"})
            path = urlparse(self.path).path
            if path == "/":
                text = "%s is running. It has no window: use the kytdl command (see README.md)." % app_info.APP_NAME_ASCII
                return self.respond(200, text.encode(), "text/plain; charset=utf-8")
            if path.startswith("/zotero-file/"):
                # Single-use URL handed to Zotero's connector for one verified PDF (zotero_bridge.FileOffers).
                target = pipeline.zotero_offers.take(path[len("/zotero-file/"):]) if pipeline.zotero_offers else None
                if target is None or not target.is_file():
                    return self.respond(404, {"error": "链接无效或已过期"})
                return self.respond(200, target.read_bytes(), "application/pdf")
            if path == "/api/state":
                try:
                    deletion.sweep(store, notifier)
                except Exception:
                    pass
                snapshot = store.snapshot()
                snapshot["account"] = snapshot.get("account_metrics", {}).get(accounts.active_id, {})
                active_items = [i for i in snapshot["items"] if i.get("source") == "demo" or i.get("account_id", "default") == accounts.active_id]
                snapshot["items"] = [dict(i, workflow=workflow_state(i), stage=stage_of(i), deletion=deletion.view(i),
                                          acquisition=acquisition_state(i, gate(active_items, snapshot["account"].get("points"), i.get("request_reward")))) for i in active_items]
                snapshot.pop("account_metrics", None)
                for internal in ("monitor", "notifications", "publish_times"):
                    snapshot.pop(internal, None)
                prefs = snapshot.setdefault("preferences", {})
                core_configured = bool(prefs.pop("core_api_key", ""))   # the key itself never leaves the server
                prefs["mcp_website_actions"] = bool(prefs.get("mcp_website_actions") and prefs.get("mcp_actions_account", "default") == accounts.active_id)
                prefs_now = snapshot["preferences"]
                points = snapshot["account"].get("points")
                hs_mode, hs_source, _, _ = resolve_highspeed(prefs_now, points)
                snapshot["download"] = dict(settings=download_settings(prefs_now), defaults=dict(DOWNLOAD_DEFAULTS),
                                            ranges={k: list(v) for k, v in DOWNLOAD_RANGES.items()},
                                            highspeed=dict(mode=hs_mode, source=hs_source, chosen=chosen_mode(prefs_now)[1],
                                                           points=points, force_default_points=FORCE_DEFAULT_POINTS,
                                                           rules_version=RULES_VERSION))
                snapshot["batches"] = intake.public_batches(store.snapshot(), accounts.active_id, prefs_now, points)
                snapshot["reward"] = dict(steps=list(intake.REWARD_STEPS), min=intake.REWARD_MIN, cap=intake.reward_cap(points),
                                          default=intake.default_reward(prefs_now, points))
                snapshot["zotero"] = dict(zotero_bridge.settings(prefs_now), **zotero_bridge.status())
                snapshot["settings"] = app_settings.effective(prefs_now, snapshot["account"], accounts.active_id)
                snapshot["about"] = dict(version=app_info.APP_VERSION, repo_url=app_info.REPO_URL, issue_enabled=bool(applog.issue_url(app_info.REPO_URL, "t", "b")),
                                         disclaimer_version=app_info.DISCLAIMER_VERSION, disclaimer_pending=prefs_now.get("disclaimer_ack") != app_info.DISCLAIMER_VERSION)
                snapshot["pipeline"] = dict(
                    enabled=bool(prefs_now.get("pipeline_enabled")), auto_publish=rules_ok(prefs_now, "pipeline_auto_publish"),
                    auto_adopt=rules_ok(prefs_now, "pipeline_auto_adopt"), sync_minutes=prefs_now.get("pipeline_sync_minutes", 10),
                    rules_version=PIPELINE_RULES_VERSION, points=points, tier=tier(points),
                    gate=gate(snapshot["items"], points),
                    next_step=next_step(snapshot["items"], snapshot["account"], prefs_now, datetime.now().astimezone(), session=has_session(snapshot["account"], snapshot["items"], accounts.has_cookie_file())),
                    session=has_session(snapshot["account"], snapshot["items"], accounts.has_cookie_file()),
                    last_tick=pipeline.last_tick, oa_first=prefs_now.get("oa_first", True) is not False,
                    oa_challenge_mode=prefs_now.get("oa_challenge_mode", "prompt"), oa_running=pipeline.oa_running,
                    core_key_configured=core_configured, batch_pending=tier(points) == "batch",
                    restart_notice=bool(prefs_now.get("pipeline_restart_notice")) and not prefs_now.get("pipeline_enabled"))
                snapshot["publish"] = dict(raise_settings=pp.raise_settings(prefs_now), raise_steps=list(pp.RAISE_STEPS),
                                           raise_range=list(pp.RAISE_MAX_RANGE), reserve_range=list(pp.RESERVE_RANGE),
                                           defaults=dict(raise_max=pp.RAISE_MAX_DEFAULT, reserve_points=pp.RESERVE_DEFAULT),
                                           batch_pending=tier(points) == "batch")
                snapshot["reject_rules"] = reject_rules()
                return self.respond(200, dict(snapshot, app_id="ablesci-local-demo", app_root=str(ROOT), busy=agent.busy, download_worker=agent.worker_status(),
                    waiting=agent.commands.qsize(), accounts=accounts.public(), active_account_id=accounts.active_id,
                    credentials_saved=accounts.vault().exists(), ai=ai.public_config(),
                    openalex_key_saved=openalex_vault.exists(), openalex_env_configured=bool(os.environ.get("OPENALEX_API_KEY", "").strip()),
                    monitor=monitor.public(), autostart=dict(enabled=autostart.enabled(), available=autostart.allowed),
                    notify=dict(settings=notifications.settings(prefs_now), kinds={k: v[0] for k, v in notifications.KINDS.items()},
                                defaults=notifications.defaults(), pending=notifier.pending()),
                    limits=dict(settings=limit_settings(prefs_now), defaults=dict(LIMIT_DEFAULTS),
                                ranges={k: list(v) for k, v in LIMIT_RANGES.items()},
                                publish_recent=pipeline.publish_limiter.recent(), publish_wait_seconds=round(pipeline.publish_limiter.wait_seconds()))))
            if path.startswith("/pdf-preview/"):
                item = store.get(path.split("/")[-1])
                if not item or not item.get("pdf"): return self.respond(404, {"error": "未找到本地 PDF"})
                from urllib.parse import parse_qs
                try: page_number = int(parse_qs(urlparse(self.path).query).get("page", ["0"])[0])
                except ValueError: return self.respond(400, {"error": "页码无效"})
                import pymupdf
                target = (store.root / item["pdf"]).resolve()
                if not target.is_relative_to(store.root.resolve()): return self.respond(400, {"error": "文件路径无效"})
                try:
                    with pymupdf.open(target) as doc:
                        if not 0 <= page_number < min(3, doc.page_count): return self.respond(400, {"error": "页码无效"})
                        rect = doc[page_number].rect
                        scale = min(.65, 600 / max(rect.width, rect.height, 1))
                        binary = doc[page_number].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                except Exception:
                    return self.respond(400, {"error": "该 PDF 暂无法预览，请手动检查文件"})
                return self.respond(200, binary, "image/png")
            if path == "/api/browser-view":
                screenshot = store.root / "screenshots" / "browser-last.png"
                if screenshot.exists():
                    return self.respond(200, screenshot.read_bytes(), "image/png")
                return self.respond(404, {"error": "尚无浏览器错误截图"})
            if path.startswith("/api/shot/"):
                item = store.get(path.split("/")[-1])
                shot = ((item or {}).get("publish_check") or {}).get("screenshot", "")
                target = (store.root / shot).resolve() if shot else None
                if target and target.is_relative_to((store.root / "screenshots").resolve()) and target.is_file():
                    return self.respond(200, target.read_bytes(), "image/png")
                return self.respond(404, {"error": "该任务没有截图"})
            if path == "/api/export":
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["title", "doi", "source", "status", "website_status", "verdict", "reason", "assist_url", "pdf"])
                for item in store.snapshot()["items"]:
                    verdict = item.get("verification") or {}
                    values = [item.get(k, "") for k in ["title", "doi", "source", "status", "website_status"]] + [verdict.get("status", ""), verdict.get("reason", ""), item.get("assist_url", ""), item.get("pdf", "")]
                    writer.writerow(["'" + str(value) if str(value).startswith(("=", "+", "-", "@")) else value for value in values])
                return self.respond(200, ("\ufeff" + output.getvalue()).encode(), "text/csv; charset=utf-8")
            if path.startswith("/pdf/"):
                item = store.get(path.split("/")[-1])
                if item and item.get("pdf"):
                    target = (store.root / item["pdf"]).resolve()
                    if target.is_relative_to(store.root.resolve()) and target.is_file():
                        return self.respond(200, target.read_bytes(), "application/pdf")
                return self.respond(404, {"error": "文件不存在"})
            return self.respond(404, {"error": "未找到页面"})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.headers.get("Origin") != origin
                    or self.headers.get("X-Demo-Token") != token
                    or not self.headers.get("Content-Type", "").startswith("application/json")):
                return self.respond(403, {"error": "操作校验失败，请刷新页面"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 36_000_000:
                    raise ValueError("上传内容过大或为空")
                body = json.loads(self.rfile.read(size))
                path = urlparse(self.path).path
                if not api_routes.dispatch(ctx, self, path, body, origin):
                    return self.respond(404, {"error": "不支持该操作"})
                return
            except PermissionDenied as exc:
                return self.respond(400, {"error": str(exc)[:250], "reason": "permission_denied"})
            except (ValueError, KeyError, TypeError) as exc:
                return self.respond(400, {"error": str(exc)[:250]})
            except Exception as exc:
                applog.record(store, "api", urlparse(self.path).path, exc)
                return self.respond(500, {"error": "操作未完成，请检查文件权限或重启软件。已保存的数据不受影响"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.agent = agent
    server.accounts = accounts
    server.ai = ai
    server.pipeline = pipeline
    server.monitor = monitor
    server.mcp_api = mcp_api
    pipeline.zotero_offers = zotero_bridge.FileOffers()
    pipeline.file_base = f"http://127.0.0.1:{server.server_port}"
    server.notifier = notifier
    server.open_path = open_with_system
    server.autostart = autostart
    server.shutdown_service = lambda: shutdown_service(server)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args(argv)
    lock = InstanceLock(ROOT / "data" / ".kytdl.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise SystemExit(str(exc))
    server = None
    try:
        server = make_server(args.port)
        server.pipeline.kick_enabled = True   # wake the scheduler right after an MCP confirmation
        print(f"{app_info.APP_NAME_ASCII}: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        lock.release()


if __name__ == "__main__":
    main()
