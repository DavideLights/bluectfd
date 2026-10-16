"""Test configuration and fixtures for BlueAgent plugin tests."""

import os
import sys
import pytest

ctfd_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
if ctfd_dir not in sys.path:
    sys.path.insert(0, ctfd_dir)

from tests.helpers import create_ctfd, destroy_ctfd, setup_ctfd
from CTFd.models import db
from CTFd.plugins.blueagent import load


@pytest.fixture(scope="session")
def app():
    """Session-wide test Flask application with BlueAgent plugin loaded before setup."""
    app = create_ctfd(setup=False, user_mode="teams")
    load(app)
    app = setup_ctfd(app)
    with app.app_context():
        db.create_all()
        yield app
    destroy_ctfd(app)


@pytest.fixture
def client(app):
    """Test client."""
    return app.test_client()


@pytest.fixture
def db_session(app):
    """Database session for tests with automatic rollback."""
    with app.app_context():
        yield db.session
        db.session.rollback()
