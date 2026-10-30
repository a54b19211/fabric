"""ETL command/import compatibility entry; implementation: flows.etl."""
if __name__ == "__main__":
    from flows.etl import cli
    cli()
else:
    from importlib import import_module as _compat_import
    import sys as _compat_sys
    _compat_module = _compat_import("flows.etl")
    # Prefect file entrypoints inspect this module object directly.
    globals().update({k: v for k, v in vars(_compat_module).items() if not k.startswith("__")})
    _compat_sys.modules[__name__] = _compat_module
