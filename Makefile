# Aurora Clone — pipeline pública
# Uso: make <alvo>   |   make help
# No container (deps via pip da imagem): make PYTHON=python <alvo>

PYTHON ?= python
SCRIPT := aurora_clone.py

.DEFAULT_GOAL := help

.PHONY: help account-aws check-policy generate-policy list-rds \
	clone-prod-to-temp dump-dev restore-temp validate \
	rename-dev-to-old rename-temp-to-dev delete-old-dev \
	pipeline run game

help:
	$(PYTHON) $(SCRIPT) --help
	@echo ""
	@echo "Alvos Make extras:"
	@echo "  make generate-policy   Gera policy.json (ARNs reais via STS + .env)"
	@echo "  make game              Operação Aurora (campanha; TTY no container -it)"

requirements-txt:
	poetry export -f requirements.txt --output requirements.txt --without-hashes

account-aws:
	$(PYTHON) $(SCRIPT) account-aws

check-policy:
	$(PYTHON) $(SCRIPT) check-policy

generate-policy:
	$(PYTHON) generate_policy.py

list-rds:
	$(PYTHON) $(SCRIPT) list-rds

clone-prod-to-temp:
	$(PYTHON) $(SCRIPT) clone-prod-to-temp

dump-dev:
	$(PYTHON) $(SCRIPT) dump-dev

restore-temp:
	$(PYTHON) $(SCRIPT) restore-temp

validate:
	$(PYTHON) $(SCRIPT) validate

rename-dev-to-old:
	$(PYTHON) $(SCRIPT) rename-dev-to-old

rename-temp-to-dev:
	$(PYTHON) $(SCRIPT) rename-temp-to-dev

delete-old-dev:
	$(PYTHON) $(SCRIPT) delete-old-dev

pipeline:
	$(PYTHON) $(SCRIPT) pipeline

run: pipeline

# Operação Aurora — campanha narrativa (TTY; só no container -it)
game:
	$(PYTHON) game.py
