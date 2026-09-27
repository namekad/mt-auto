import multiprocessing

multiprocessing.freeze_support()

from app.gui.app import run_gui

if __name__ == "__main__":
    run_gui()
