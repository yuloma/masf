.DEFAULT_GOAL := help

PYTHON ?= python3
MASF_PROVIDERS ?= codex cursor
MASF_REMOTE ?= origin
MASF_MAIN_BRANCH ?= main
export MASF_PROVIDERS MASF_REMOTE MASF_MAIN_BRANCH
# Export command-line overrides as well as variables inherited from the shell.
export MASF_ROOT MASF_CONTROL MASF_CONFIG_DIR MASF_STATE_DIR MASF_JOBS_DIR
export MASF_CONTROLLER_IMAGE MASF_PROJECT MASF_DOCKER_SOCKET MASF_DOCKER_GID

.PHONY: help start stop restart ps status logs logs-follow doctor doctor-offline scan \
        build build-controller build-runner update test test-engine smoke check

help start stop restart ps status logs logs-follow doctor doctor-offline scan \
build build-controller build-runner update:
	@bash scripts/factory.sh $@

test:
	$(PYTHON) -m unittest discover -v

test-engine:
	MASF_TEST_ENGINE=1 $(PYTHON) -m unittest tests.test_engine tests.test_process_recovery.ComposeProcessRecovery -v

smoke:
	bash examples/docker/smoke-profile.sh

check:
	git diff --check
	bash -n scripts/factory.sh examples/docker/build-runner.sh
