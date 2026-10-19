"""``python -m ca`` entry point."""

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    from ca.cli.main import main

    raise SystemExit(main())
