"""One-time local website login entry for the persistent quotation profile."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from quote_app.browser.channel_detection import BrowserChoice, detect_browser_choice
from quote_app.browser.session import jd_profile_dir_for, prepare_dedicated_profile

JD_LOGIN_URL = "https://passport.jd.com/new/login.aspx"
TMALL_LOGIN_URL = "https://login.taobao.com/member/login.jhtml"

BrowserProcessLauncher = Callable[[Sequence[str]], Any]


def open_login_browser(
    profile_dir: Path,
    *,
    browser_choice: BrowserChoice | None = None,
    launch: BrowserProcessLauncher = subprocess.Popen,
) -> None:
    """Open Tmall and JD login pages in their separate local profiles.

    Credentials, cookies, and verification codes remain entirely within the browser.
    """
    tmall_profile = prepare_dedicated_profile(profile_dir)
    jd_profile = prepare_dedicated_profile(jd_profile_dir_for(profile_dir))
    choice = browser_choice or detect_browser_choice()
    launch(
        [
            str(choice.executable_path),
            f"--user-data-dir={tmall_profile}",
            "--new-window",
            TMALL_LOGIN_URL,
        ]
    )
    launch(
        [
            str(choice.executable_path),
            f"--user-data-dir={jd_profile}",
            "--new-window",
            JD_LOGIN_URL,
        ]
    )
