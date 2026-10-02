from __future__ import annotations

import multiprocessing
import traceback

multiprocessing.freeze_support()


def main() -> None:
    try:
        from app.paths import ensure_runtime_files
        from app.gui.app import run_gui

        ensure_runtime_files()
        run_gui()
    except Exception as error:
        text = traceback.format_exc()
        try:
            from app.paths import user_dir

            log_dir = user_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "startup_error.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, str(error), "AUTO-TRADER", 16)
        except Exception:
            raise


if __name__ == "__main__":
    main()
