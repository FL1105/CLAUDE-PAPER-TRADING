PY ?= python
.PHONY: setup test lint data research backtest paper dash nightly
setup:
	$(PY) -m pip install -e ".[dev,dash]"
test:
	$(PY) -m pytest
lint:
	$(PY) -m ruff check src tests
data:
	$(PY) -m bot.cli data
research:
	$(PY) -m bot.cli research
backtest:
	$(PY) -m bot.cli backtest
paper:
	$(PY) -m bot.cli paper
dash:
	$(PY) -m bot.cli dash
nightly:
	$(PY) -m bot.cli nightly
