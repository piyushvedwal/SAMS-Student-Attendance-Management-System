import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sams import auth, db, seed  # noqa: E402


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Seed the demo data once; each test works on its own copy."""
    path = tmp_path_factory.mktemp("template") / "sams.db"
    db.set_path(path)
    seed.ensure_seeded()
    with db.tx() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
def app_db(template_db, tmp_path, monkeypatch):
    path = tmp_path / "sams.db"
    shutil.copy(template_db, path)
    db.set_path(path)
    monkeypatch.setenv("SAMS_DB", str(path))
    yield path


def user(username: str) -> dict:
    row = db.one("SELECT id FROM users WHERE username = ?", (username,))
    return auth.get_user(row["id"])


@pytest.fixture
def admin(app_db):
    return user("admin")


@pytest.fixture
def teacher(app_db):
    return user("kavita.rao")


@pytest.fixture
def student(app_db):
    return user("aarav.sharma")
