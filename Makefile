# =============================================================================
# Makefile — pgha build helpers
# =============================================================================

NAME    = pgha
VERSION = 1.0.0

.PHONY: rpm srpm clean lint help

help:
	@echo "Targets:"
	@echo "  rpm    — build binary RPM (default)"
	@echo "  srpm   — build source RPM"
	@echo "  clean  — remove build artefacts"
	@echo "  lint   — run flake8 on source"

rpm:
	@bash build.sh

srpm:
	@bash build.sh --srpm

clean:
	@bash build.sh --clean

lint:
	@python3 -m flake8 src/ bin/ --max-line-length=100 \
		--ignore=E501,W503 || true
