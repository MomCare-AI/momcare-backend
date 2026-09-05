"""DatabaseStorage — files survive a redeploy because they live in
Postgres, not on the container's disk. See docs/PLAN.md item 1 and the
module's own docstring for why.
"""

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import Client

from momcare_platform.core.common.storage import DatabaseStorage
from momcare_platform.core.organization.models import FileBlob

pytestmark = pytest.mark.django_db


def test_a_saved_file_can_be_read_back_byte_for_byte():
    storage = DatabaseStorage()
    name = storage.save("licenses/2026/09/test.pdf", ContentFile(b"pdf-bytes-here"))

    with storage.open(name) as f:
        assert f.read() == b"pdf-bytes-here"


def test_two_uploads_never_collide_even_with_the_same_original_name():
    storage = DatabaseStorage()
    first = storage.save("staff/2026/09/photo.jpg", ContentFile(b"first"))
    second = storage.save("staff/2026/09/photo.jpg", ContentFile(b"second"))

    assert first != second
    with storage.open(first) as f:
        assert f.read() == b"first"
    with storage.open(second) as f:
        assert f.read() == b"second"


def test_exists_and_size_reflect_a_real_saved_file():
    storage = DatabaseStorage()
    name = storage.save("organizations/2026/09/building.jpg", ContentFile(b"1234567890"))

    assert storage.exists(name)
    assert storage.size(name) == 10
    assert not storage.exists("organizations/2026/09/never-saved.jpg")


def test_delete_actually_removes_the_row():
    storage = DatabaseStorage()
    name = storage.save("staff/2026/09/photo.jpg", ContentFile(b"data"))

    storage.delete(name)

    assert not storage.exists(name)
    assert FileBlob.objects.filter(name=name).count() == 0


def test_content_type_comes_from_the_uploaded_file_itself():
    """Regression: an earlier version read ``content.file.content_type``,
    which does not exist on Django's UploadedFile classes and would have
    silently sent every future upload out as application/octet-stream,
    breaking next/image on the frontend."""
    storage = DatabaseStorage()
    upload = SimpleUploadedFile("photo.jpg", b"jpeg-bytes", content_type="image/jpeg")

    name = storage.save("staff/2026/09/photo.jpg", upload)

    assert FileBlob.objects.get(name=name).content_type == "image/jpeg"


def test_url_points_at_the_download_endpoint():
    storage = DatabaseStorage()
    name = storage.save("licenses/2026/09/test.pdf", ContentFile(b"data"))

    assert storage.url(name) == f"/media/db/{name}"


def test_the_download_endpoint_serves_the_right_bytes_and_content_type():
    storage = DatabaseStorage()
    upload = SimpleUploadedFile("photo.jpg", b"jpeg-bytes", content_type="image/jpeg")
    name = storage.save("staff/2026/09/photo.jpg", upload)

    response = Client().get(f"/media/db/{name}")

    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert response.content == b"jpeg-bytes"


def test_the_download_endpoint_404s_for_a_name_that_was_never_saved():
    response = Client().get("/media/db/staff/2026/09/never-existed.jpg")
    assert response.status_code == 404
