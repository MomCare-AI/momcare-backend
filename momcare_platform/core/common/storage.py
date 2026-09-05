"""A Django Storage backend that keeps file bytes in Postgres instead of on
the container's disk.

Why: Railway's container filesystem is wiped on every redeploy, so anything
written to local disk (the default for a FileField with no ``storage=``)
silently disappears the next time code ships — see ``docs/PLAN.md`` item 1
and ``FileBlob``'s own docstring (``core/organization/models.py``) for the
full reasoning, including why this is Postgres and not a new object-storage
service at this project's current scale.

Applied per-field via ``storage=DatabaseStorage()`` on the handful of
FileFields that need to survive a redeploy — never set as the project's
``STORAGES["default"]``, so anything not explicitly opted in keeps behaving
exactly as it does today.

Files are served back unauthenticated, by name, through
``file-blob-download`` (``config/urls.py``) — the same security posture
Django's own ``MEDIA_URL`` static serving already has today for a
disk-backed FileField. This is not a new hole opened by this change; it is
the same "if you have the URL, you can fetch it" behavior that already
exists, just pointed at Postgres instead of disk.
"""

from __future__ import annotations

import posixpath
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.http import Http404, HttpResponse
from django.utils.deconstruct import deconstructible
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET


@deconstructible
class DatabaseStorage(Storage):
    def _blob_model(self):
        # Function-local: core.common loads before core.organization is
        # guaranteed ready during Django's app-loading sequence, so a
        # module-level import of a concrete app model here would risk an
        # AppRegistryNotReady error depending on import order.
        from momcare_platform.core.organization.models import FileBlob

        return FileBlob

    def _open(self, name, mode="rb"):  # noqa: ARG002 - Storage's own signature
        blob = self._blob_model().objects.get(name=name)
        return ContentFile(bytes(blob.data), name=name)

    def _save(self, name, content):
        content.seek(0)
        data = content.read()
        self._blob_model().objects.create(
            name=name,
            # UploadedFile (what DRF/Django hand every real upload) carries
            # its MIME type directly - not on ``.file``, a mistake that
            # would have silently sent every future upload out as
            # application/octet-stream, breaking next/image on the frontend.
            content_type=getattr(content, "content_type", "") or "",
            size=len(data),
            data=data,
        )
        return name

    def get_available_name(self, name, max_length=None):  # noqa: ARG002
        """A fresh, collision-proof name for every upload.

        FileSystemStorage detects a collision and suffixes it; a random
        name here makes a collision structurally impossible instead of
        detected-and-renamed, which also means never overwriting a
        previous upload still referenced by an old FileField value.
        """
        directory = posixpath.dirname(name)
        ext = "".join(posixpath.splitext(name)[1:])
        unique = f"{uuid.uuid4().hex}{ext}"
        return posixpath.join(directory, unique) if directory else unique

    def exists(self, name) -> bool:
        return self._blob_model().objects.filter(name=name).exists()

    def size(self, name) -> int:
        return self._blob_model().objects.values_list("size", flat=True).get(name=name)

    def url(self, name) -> str:
        from django.urls import reverse

        return reverse("file-blob-download", kwargs={"name": name})

    def delete(self, name) -> None:
        self._blob_model().objects.filter(name=name).delete()

    def get_created_time(self, name):
        return self._blob_model().objects.values_list("created_at", flat=True).get(name=name)


@require_GET
@cache_control(max_age=60 * 60 * 24 * 7)  # a week - blobs are never overwritten, only replaced under a new name
def file_blob_download(request, name: str):  # noqa: ARG001 - request required by Django's view signature
    """Serve a file stored via ``DatabaseStorage`` back out by name.

    Plain Django view, not DRF — this is bytes-out, not JSON, and it must
    stay reachable the same way Django's own MEDIA_URL static serving is:
    unauthenticated, by name. That is not a new hole; it is the same
    security posture disk-backed media already has today (see this
    module's own docstring).
    """
    from momcare_platform.core.organization.models import FileBlob

    try:
        blob = FileBlob.objects.get(name=name)
    except FileBlob.DoesNotExist:
        raise Http404("No such file.") from None

    return HttpResponse(bytes(blob.data), content_type=blob.content_type or "application/octet-stream")
