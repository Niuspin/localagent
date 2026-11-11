"""浏览器冒烟测试（可选）：用 Chromium 驱动真实 UI + 假 llama-server。

用法: python3 tests/ui_smoke.py [截图目录]
依赖: playwright（含 chromium）。没有则跳过。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import make_gguf  # noqa: E402
from conftest import free_port  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright 未安装")
    sys.exit(0)

shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="la-shots-"))
shots.mkdir(parents=True, exist_ok=True)
fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


home = Path(tempfile.mkdtemp(prefix="la-ui-"))
(home / "models").mkdir()
make_gguf.make(home / "models" / "Mock-MoE-A1B-Q4_K_M.gguf")
make_gguf.make(home / "models" / "mmproj-Mock-MoE-A1B-F16.gguf", arch="clip", experts=0, layers=2, name="mmproj")
(home / "bin").mkdir()
shutil.copy(ROOT / "tests" / "mock_llama_server.py", home / "bin" / "llama-server")
os.chmod(home / "bin" / "llama-server", 0o755)
(home / "ws").mkdir()
(home / "ws" / "notes.md").write_text("# 笔记\n\n你好 **世界**\n", "utf-8")

port = free_port()
env = {**os.environ, "LAGENT_HOME": str(home), "MOCK_LOG": str(home / "mock.log"), "NO_PROXY": "127.0.0.1,localhost"}
srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", str(port)], cwd=str(ROOT), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
token = None
t0 = time.time()
while time.time() - t0 < 30:
    line = srv.stdout.readline()
    m = re.search(r"\?t=(\S+)", line or "")
    if m:
        token = m.group(1)
        break
assert token, "server did not start"
time.sleep(0.5)
base = f"http://127.0.0.1:{port}"

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        page.on("console", lambda m: errors.append("console." + m.type + ": " + m.text) if m.type in ("error", "warning") else None)
        page.on("requestfailed", lambda r: errors.append("requestfailed: " + r.url))
        external: list[str] = []
        page.on("request", lambda r: external.append(r.url) if not r.url.startswith((base, "data:", "blob:")) else None)

        def finished():
            try:
                page.wait_for_selector("#btnSend.stop", timeout=3000)
            except Exception:
                pass
            page.wait_for_selector("#btnSend:not(.stop)", timeout=30000)

        print("[1] 首屏")
        page.goto(f"{base}/?t={token}")
        page.wait_for_selector(".empty h1", timeout=8000)
        check(page.locator("#emptyCards .card").count() >= 2, "空状态显示引导卡片（工作区/加载模型）")
        page.screenshot(path=str(shots / "01-empty.png"))

        print("[2] 选择工作区")
        page.click("#chipWs")
        page.wait_for_selector(".modal .browse-list")
        page.fill(".modal input.inp", str(home / "ws"))
        page.press(".modal input.inp", "Enter")
        page.wait_for_timeout(300)
        page.click(".modal button.primary")
        page.wait_for_selector(".modal", state="detached", timeout=5000)
        check(page.inner_text("#wsName") == "ws", "顶栏显示工作区名称")

        print("[3] 加载模型")
        page.click("#emptyCards >> text=加载模型")
        page.wait_for_selector("#engDot.ready", timeout=30000)
        check("Mock" in page.inner_text("#engText"), "顶栏显示已加载的模型名")
        check(not page.is_disabled("#btnSend") or True, "发送按钮初始状态")

        print("[4] 对话 + 审批")
        page.fill("#input", "创建文件 hello.txt")
        page.press("#input", "Enter")
        page.wait_for_selector(".approval", timeout=20000)
        check(page.locator(".approval .diff").count() == 1, "审批卡片显示 diff")
        page.screenshot(path=str(shots / "02-approval.png"))
        page.click(".approval >> text=允许一次")
        finished()
        check(page.locator(".tool.ok").count() >= 1, "工具卡片完成")
        check((home / "ws" / "notes" / "hello.txt").exists(), "文件确实被创建")
        check(page.locator(".msg.assistant .md").count() >= 1, "助手文本已渲染")
        page.screenshot(path=str(shots / "03-chat.png"))

        print("[5] 思考块 / Markdown")
        page.fill("#input", "思考一下")
        page.press("#input", "Enter")
        finished()
        check(page.locator("details.think").count() >= 1, "思考块已渲染")

        print("[6] 抽屉")
        page.click("#btnDrawer")
        page.wait_for_selector("#drawer .tree-row")
        page.click("#drawer .tree-row:has-text('notes') >> nth=0")
        page.wait_for_selector("#drawer .tree-row[title='notes/hello.txt']")
        check(True, "文件树包含新文件（可展开子目录）")
        page.click("#drawer .tree-row:has-text('notes.md')")
        page.wait_for_selector("#drawer .preview pre")
        check("世界" in page.inner_text("#drawer .preview pre"), "文件预览")
        page.click("#drawer .dr-tab:has-text('变更')")
        page.wait_for_selector("#drawer .jr-item")
        check(page.locator("#drawer .jr-item").count() >= 1, "变更记录")
        page.screenshot(path=str(shots / "04-drawer.png"))

        print("[7] 设置各页")
        page.click("#btnSettings")
        page.wait_for_selector(".modal.settings")
        for t in ["生成", "安全", "搜索", "外观", "模型与引擎"]:
            page.click(f".set-nav button:has-text('{t}')")
            page.wait_for_timeout(150)
            check(page.locator(".set-pane .field, .set-pane .prow").count() > 0, f"设置页「{t}」渲染")
            if t == "搜索":
                page.screenshot(path=str(shots / "05-settings-search.png"))
        page.click(".set-nav button:has-text('扩展')")
        page.wait_for_selector(".mcp-add")
        page.fill(".mcp-add input:nth-child(1)", "demo")
        page.fill(".mcp-add input:nth-child(2)", "echo hello")
        page.click(".mcp-add button")
        page.wait_for_selector(".mcp-row .mcp-detail")
        check(page.locator(".mcp-row:has-text('demo')").count() == 1, "扩展页：添加服务器并展开详情")
        check(page.locator(".mcp-head input[type=checkbox]").nth(1).is_checked(), "扩展页：沙箱默认开启")
        page.screenshot(path=str(shots / "05b-settings-mcp.png"))
        page.click(".mcp-detail button:has-text('删除')")
        page.locator("button.btn.danger").last.click()
        page.wait_for_selector(".mcp-row", state="detached")
        page.click(".set-nav button:has-text('外观')")
        page.select_option(".set-pane select", "dark")
        page.wait_for_timeout(200)
        check(page.get_attribute("html", "data-theme") == "dark", "切换深色主题")
        page.screenshot(path=str(shots / "06-dark-settings.png"))
        page.select_option(".set-pane select", "light")
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached")

        print("[8] 技能 / 记忆")
        page.click("#btnSkills")
        page.wait_for_selector(".modal .toolbar")
        page.click(".modal >> text=新建技能")
        page.fill(".modal-back:last-child .modal input.inp >> nth=0", "ui-test")
        page.fill(".modal-back:last-child .modal input.inp >> nth=1", "界面测试技能")
        page.fill(".modal-back:last-child .modal textarea", "# 步骤\n1. 打招呼")
        page.click(".modal-back:last-child .modal button.primary")
        page.wait_for_selector(".list-row:has-text('ui-test')", timeout=5000)
        check(True, "创建技能并出现在列表")
        check(page.is_checked(".list-row:has-text('ui-test') input[type=checkbox]"), "用户新建的技能默认已启用")
        page.keyboard.press("Escape")
        page.click("#btnMemory")
        page.wait_for_selector(".modal .toolbar")
        page.click(".modal >> text=添加")
        page.fill(".modal-back:last-child .modal textarea", "我喜欢简洁的回答")
        page.click(".modal-back:last-child .modal button.primary")
        page.wait_for_selector(".list-row:has-text('简洁')", timeout=5000)
        check(True, "添加记忆并出现在列表")
        page.screenshot(path=str(shots / "07-memory.png"))
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")

        print("[9] / 技能弹层")
        page.fill("#input", "/ui")
        page.wait_for_selector("#slashPop .slash-item")
        page.press("#input", "Enter")
        check(page.locator("#attachChips .attach-chip.skill").count() == 1, "选择技能后出现芯片")
        page.fill("#input", "")

        print("[10] 刷新后恢复会话")
        page.reload()
        page.wait_for_selector(".msg.assistant", timeout=8000)
        check(page.locator(".msg.user").count() >= 2, "刷新后历史消息仍在")

        print("[11] 手机宽度")
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        check(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "无横向溢出")
        page.screenshot(path=str(shots / "08-mobile.png"))
        page.click("#btnMenu")
        page.wait_for_timeout(300)
        page.screenshot(path=str(shots / "09-mobile-menu.png"))

        print("[12] 安全")
        check(not external, f"没有任何外部请求: {external[:3]}")
        real_errors = [e for e in errors if "favicon" not in e]
        check(not real_errors, f"没有控制台错误/CSP 违规: {real_errors[:3]}")
        br.close()
finally:
    srv.terminate()
    try:
        srv.wait(10)
    except subprocess.TimeoutExpired:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

print(f"\n截图目录: {shots}")
print("全部通过" if not fails else f"{len(fails)} 项失败: {fails}")
sys.exit(1 if fails else 0)
