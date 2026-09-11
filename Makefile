PG          := immich_postgres
NETWORK     := immich_default
TEST_DB_URL := postgresql://postgres:postgres@$(PG):5432/immich_test

.PHONY: help test testdb testdb-clean lint schema-dump

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

schema-dump:  ## Re-dump the live Immich schema into the test fixture
	docker exec $(PG) pg_dump -U postgres --schema-only immich > tests/fixtures/schema_v3.2.0.sql

testdb:  ## (Re)create the scratch test database from the fixture
	docker exec $(PG) psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"
	docker exec $(PG) psql -U postgres -c "CREATE DATABASE immich_test;"
	docker exec -i $(PG) psql -U postgres -q -d immich_test < tests/fixtures/schema_v3.2.0.sql

testdb-clean:  ## Drop the scratch test database
	docker exec $(PG) psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"

test:  ## Run the tests in a container on the Immich network (PYTEST_ARGS=tests/x.py to narrow)
	docker run --rm --network $(NETWORK) -v $(PWD):/app -w /app \
	  -e TEST_DB_URL=$(TEST_DB_URL) python:3.12-slim \
	  bash -c 'pip install -q asyncpg pytest pytest-asyncio && python -m pytest -v $(PYTEST_ARGS)'

lint:  ## Syntax-check all source files
	python3 -m py_compile src/*.py
