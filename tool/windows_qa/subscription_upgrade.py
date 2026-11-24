"""Exercise the real Flutter application through Windows UI Automation."""

import argparse
import re
import subprocess
import tempfile
import time
from pathlib import Path

from pywinauto import Desktop, keyboard


CASE = "ShowCase upgrade acceptance matter"
EXHIBIT = "Upgrade fixture exhibit.txt"


def elements(window):
    return [window, *window.descendants()]


def locate(window, pattern, seconds=35):
    deadline = time.monotonic() + seconds
    matcher = re.compile(pattern, re.I | re.S)
    while time.monotonic() < deadline:
        for item in elements(window):
            if matcher.search(item.window_text()) and item.is_visible():
                return item
        time.sleep(0.15)
    names = [repr(item.window_text()) for item in elements(window) if item.window_text()]
    raise RuntimeError(f"Could not find {pattern!r}. Visible labels: {', '.join(names[:65])}")


def click(item):
    item.click_input()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("old", "upgraded", "clean"))
    parser.add_argument("exe", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    process = subprocess.Popen([str(args.exe)], cwd=args.exe.parent)
    window = Desktop(backend="uia").window(process=process.pid, title_re=".*(?:ShowCase|litigation_tool).*")
    try:
        window.wait("visible ready", timeout=65)
        # Request the accessibility tree before searching Flutter controls.
        window.set_focus()
        window.descendants()
        if args.phase == "old":
            click(locate(window, "^(Create new case|Create Case)$"))
            locate(window, "^Case Name$")
            keyboard.send_keys(CASE, with_spaces=True)
            click(locate(window, "^Create$"))
            click(locate(window, re.escape(CASE)))
            click(locate(window, r"^Evidence(?:\n.*)?$"))
            click(locate(window, "^Import Files$"))
            dialog = Desktop(backend="win32").window(class_name="#32770", process=process.pid)
            dialog.wait("visible ready", timeout=25)
            fixture = Path(tempfile.gettempdir(), EXHIBIT)
            fixture.write_text("Fictional evidence for the Windows upgrade test.", encoding="utf-8")
            edits = dialog.descendants(class_name="Edit")
            file_name = next((field for field in edits if field.control_id() == 1148), edits[0])
            file_name.set_edit_text(str(fixture))
            dialog.child_window(control_id=1).click()
            dialog.wait_not("visible", timeout=25)
            locate(window, re.escape(EXHIBIT))
        elif args.phase == "upgraded":
            # The current first-run guide can be dismissed without creating another case.
            try:
                click(locate(window, "^(Skip for now|Exit)$", seconds=5))
            except RuntimeError:
                pass
            click(locate(window, re.escape(CASE)))
            click(locate(window, r"^Evidence(?:\n.*)?$"))
            locate(window, re.escape(EXHIBIT))
            click(locate(window, "^Import Files$"))
            locate(window, "Demo Mode includes tutorials and the bundled sample case")
            click(locate(window, "^ShowCase Subscription$"))
            locate(window, "^Continue with Google$")
            locate(window, "^Email$")
        else:
            try:
                click(locate(window, "^(Skip for now|Exit)$", seconds=5))
            except RuntimeError:
                pass
            click(locate(window, "^App Options$"))
            click(locate(window, "^Subscription & Offline Access$"))
            locate(window, "^Continue with Google$")
            locate(window, "^Email$")
        print(f"Completed {args.phase} Windows application acceptance.")
    finally:
        if process.poll() is None:
            try:
                window.capture_as_image().save(args.output)
                window.close()
            except Exception as error:
                print(f"Could not capture or close window: {error}")
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)


if __name__ == "__main__":
    main()
