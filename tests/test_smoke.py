def test_imports():
    import importlib
    pkg = importlib.import_module("mylittletracker")
    assert hasattr(pkg, "__version__")

    cli = importlib.import_module("mylittletracker.cli")
    assert hasattr(cli, "main")

