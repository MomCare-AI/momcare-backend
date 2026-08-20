"""Shared pytest fixtures.

Deliberately minimal — fixtures that depend on real business logic (a user
factory, patient onboarding, staff auto-creation) are future work, added
alongside that logic, not before it.
"""

import pytest


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath
