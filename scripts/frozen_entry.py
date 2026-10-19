"""PyInstaller entry point; divert multiprocessing children before CLI imports."""

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    import sys

    from ca.cli.main import main

    raise SystemExit(main(sys.argv[1:] or ["desktop"]))
