## Dev environment tips
- Python 3.12 managed with Poetry. Use `poetry run python` to run python
- Package add and removal must be performed by the user. If needed, explicitly request the user to run the appropriate commands.
- Run python tests: `poetry install --with dev && poetry run python -m pytest`
- Run a python dev tool example: `pipx run ruff check .`