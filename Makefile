VENV ?= .venv
PYTHON ?= python3.12
PY := $(VENV)/bin/python

.PHONY: install test serve test-tools deploy
install:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -e '.[test]'

test:
	$(PY) -m pytest -q

serve:
	$(PY) -m uvicorn app.server:app --host 127.0.0.1 --port 8000 --workers 1

test-tools:
	$(PY) -c "import json; from app.tools import HANDLERS; print(HANDLERS['$(TOOL)'](json.loads('$(ARGS)')))"

deploy:
	./deploy/install.sh
