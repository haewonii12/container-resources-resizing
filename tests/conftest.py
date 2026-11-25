"""Shared fixtures: the integration `env` fixture (temp git remote + fakes) is reused by other test modules."""

pytest_plugins = ["tests.integration.test_pipeline"]
