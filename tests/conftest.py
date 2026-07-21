from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from coderus.config import DatabaseSettings
from coderus.db import create_engine_from_settings
from coderus.models import Base


@pytest.fixture
def engine(tmp_path: Path):
    engine = create_engine_from_settings(DatabaseSettings(path=tmp_path / "test.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
