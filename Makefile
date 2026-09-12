PG          ?= immich_postgres
NETWORK     ?= immich_default
TEST_DB_URL := postgresql://postgres:postgres@$(PG):5432/immich_test
# $(CURDIR) is make's own idea of where it is. $(PWD) is inherited from the
# environment and can be stale or absent.
DUMP_TMP    := $(CURDIR)/.schema-dump.tmp

.PHONY: help test testdb testdb-clean lint schema-dump

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

schema-dump:  ## Re-dump the live Immich schema into the test fixture
	# The \restrict/\unrestrict tokens pg_dump 17.6+ emits are random per run.
	# Left in, every dump diffs against the last one and the fixture stops
	# being useful as a 'did Immich's schema move?' check. They only guard
	# psql restores against untrusted dumps; this one is ours.
	#
	# Via a temp file, never straight into the fixture. A '>' truncates before
	# pg_dump has written a byte, and make's sh has no pipefail, so grep's exit
	# status hides a pg_dump that failed -- between them, a container that is
	# down replaces the fixture the whole test suite is built from with an
	# empty file, and reports success.
	set -e; \
	  trap 'rm -f $(DUMP_TMP) $(DUMP_TMP).clean' EXIT; \
	  docker exec $(PG) pg_dump -U postgres --schema-only immich > $(DUMP_TMP); \
	  grep -vE '^\\(un)?restrict ' $(DUMP_TMP) > $(DUMP_TMP).clean; \
	  test -s $(DUMP_TMP).clean; \
	  mv $(DUMP_TMP).clean tests/fixtures/schema_v3.2.0.sql

testdb: testdb-clean  ## (Re)create the scratch test database from the fixture
	docker exec $(PG) psql -U postgres -c "CREATE DATABASE immich_test;"
	# ON_ERROR_STOP, or psql runs the whole fixture past the first error and
	# exits 0. The fixture opens with CREATE EXTENSION for cube, vector and
	# earthdistance; without this, one missing extension takes every table that
	# depends on it with it, `make testdb` reports success, and the suite runs
	# against a half-built schema. tests/test_harness.py checks the result too,
	# but this is the half that fails at the point of the mistake.
	docker exec -i $(PG) psql -v ON_ERROR_STOP=1 -U postgres -q -d immich_test < tests/fixtures/schema_v3.2.0.sql

testdb-clean:  ## Drop the scratch test database
	docker exec $(PG) psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"

# The pip cache is mounted, not rebuilt: the container is thrown away each run,
# so without it every `make test` re-downloads and re-builds seven wheels before
# a single test runs.
#
# Dependencies come from pyproject.toml, not a list repeated here. The setup
# wizard is named configure.py precisely so this works: as setup.py it was
# executed by the build frontend, which blocked on its input() prompt and died
# with EOFError.
test:  ## Run the tests in a container on the Immich network (PYTEST_ARGS=tests/x.py to narrow)
	docker run --rm --network $(NETWORK) -v $(CURDIR):/app -w /app \
	  -v $(CURDIR)/.pip-cache:/root/.cache/pip \
	  -e TEST_DB_URL=$(TEST_DB_URL) python:3.12-slim \
	  bash -c 'pip install -q -e ".[dev]" && python -m pytest -v $(PYTEST_ARGS)'

lint:  ## Syntax-check all source files
	python3 -m py_compile src/*.py
