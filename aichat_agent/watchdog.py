"""Prevent a killed controller from leaving its SDK/browser children running."""
import os
import signal
import threading
import time


def watch_parent(expected_pid: int) -> None:
    def check():
        while True:
            if os.getppid() != expected_pid:
                os.killpg(os.getpid(), signal.SIGKILL)
                return
            time.sleep(0.5)
    threading.Thread(target=check, daemon=True).start()
