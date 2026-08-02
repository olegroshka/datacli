"""Cloud-sync storage backends for the datacli data root (push-only backup).

The sync engine (``engine.py``) is pure and backend-agnostic; backends
(``backends.py``, ``gdrive.py``) implement a tiny upload surface behind the
``StorageBackend`` ABC. The ``sync`` shell command / ``storage/cli.py`` glue
them together with the dry-run-unless-``--run`` culture of the eodhd CLI.
"""
