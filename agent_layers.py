"""Popup layers: reading, dismissing, and handing a blocked click to the person (BrowserAgent mixin, moved unchanged from browser_agent.py in refactor stage 3)."""
import re
from datetime import datetime
from core import now
from agent_common import _count


class LayersMixin:
    def dismiss_info_layers(self):
        """The detail page opens with an informational layer ("已经有人上传了文件，请在 48 小时内查看并审核") that blocks clicks.
        Only that kind of purely informational layer is closed (its single 确定 button)."""
        for _ in range(3):
            closed = False
            for layer, text in self.visible_layers():
                if "已经有人上传了文件" in text or ("信息" in text and "审核" in text and "采纳" not in text and "驳回" not in text):
                    button = layer.locator(".layui-layer-btn a").filter(has_text=re.compile(r"^\s*确定\s*$"))
                    if button.count() == 1:
                        button.first.click()
                        self.page.wait_for_timeout(600)
                        closed = True
            if not closed:
                return

    RISKY_LAYER = re.compile(r"采纳|驳回|拒绝|提交|积分|扣|删除|支付|付费|充值|悬赏")
    CLOSE_TEXT = re.compile(r"^\s*(?:确定|我知道了|知道了|关闭|好的|朕知道了|×|x|X)\s*$")

    def clear_blocking_layers(self, rounds=3):
        """The site sometimes pops up layers with varying close controls. Close only purely informational ones
        (no inputs, no wording about adopt/reject/submit/points/payment): try the × icon, then a common close button.
        Returns the texts of layers that could NOT be closed safely."""
        left = []
        for _ in range(rounds):
            left, closed = [], False
            for layer, text in self.visible_layers():
                if _count(layer.locator("textarea, input:not([type=hidden]), select")) or self.RISKY_LAYER.search(text):
                    left.append(" ".join(text.split())[:120])
                    continue
                for candidate in (layer.locator(".layui-layer-close, .layui-layer-setwin a, [aria-label*=close i], [class*=close]"),
                                  layer.locator(".layui-layer-btn a, button, a").filter(has_text=self.CLOSE_TEXT)):
                    if _count(candidate):
                        try:
                            candidate.first.click(timeout=3000)
                            closed = True
                            break
                        except Exception:
                            continue
                else:
                    left.append(" ".join(text.split())[:120])
            if not closed:
                break
            self.page.wait_for_timeout(600)
        return left

    def click_or_handover(self, item_id, control, what):
        """Click an adopt/reject control. If something covers it, clear informational layers and try ONE more time.
        Playwright never dispatches a click that fails its actionability checks, so a failed click sent nothing.
        Still blocked -> hand over to the human (gate stays locked, nothing retried later)."""
        for attempt in range(2):
            try:
                control.click(timeout=8000)
                return
            except Exception:
                left = self.clear_blocking_layers()
        shot = ""
        try:
            folder = self.store.root / "screenshots"
            folder.mkdir(exist_ok=True)
            shot = str((folder / f"blocked-{item_id}-{datetime.now():%H%M%S}.png").relative_to(self.store.root))
            self.page.screenshot(path=str(self.store.root / shot), mask=[self.page.locator("input")])
        except Exception:
            pass
        message = f"{what}科研通弹窗遮挡了按钮，软件未能点击（未提交）。请在浏览器中手动{what}。完成后点击“已在科研通处理”。"
        self.store.update(item_id, status="attention", note=message, handover=dict(kind=what, time=now(), screenshot=shot, layers=left))
        self.store.event(message, kind="needs_action", item_id=item_id, action="open_site")
        self.notify_user("needs_action", f"blocked:{item_id}", "需处理", message)
        raise ValueError(message)

    def visible_layers(self):
        """[(locator, text)] of the layer popups that are visible right now."""
        layers = self.page.locator(".layui-layer")
        found = []
        total = layers.count()
        for n in range(total if isinstance(total, int) else 0):
            layer = layers.nth(n)
            try:
                if layer.is_visible():
                    found.append((layer, layer.inner_text(timeout=3000)))
            except Exception:
                pass
        return found

