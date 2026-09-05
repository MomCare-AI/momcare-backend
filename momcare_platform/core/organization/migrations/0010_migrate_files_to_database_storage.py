"""Carry every existing disk-backed file into FileBlob, so the switch to
DatabaseStorage (previous migration) doesn't orphan a file that was
uploaded before this shipped.

Deliberately does not change ``building_photo``/``license_document``'s
stored ``name`` — that string is unaffected by which storage backend
interprets it, so leaving it alone means the field keeps resolving
correctly through the new backend with zero other code needing to know
this migration ran.

**Timing matters in production**: this reads from local disk
(``FileSystemStorage``, not the app's now-current ``DatabaseStorage``), so
it has to run against a live deploy *before* the next redeploy wipes that
disk out from under it — see docs/PLAN.md item 1.
"""

import mimetypes

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import migrations


def migrate_organization_files(apps, schema_editor):
    Organization = apps.get_model("organization", "Organization")
    FileBlob = apps.get_model("organization", "FileBlob")
    disk = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)

    for field_name in ("building_photo", "license_document"):
        for org in Organization.objects.exclude(**{field_name: ""}).exclude(**{f"{field_name}__isnull": True}):
            name = getattr(org, field_name).name
            if not name or not disk.exists(name) or FileBlob.objects.filter(name=name).exists():
                continue
            with disk.open(name, "rb") as f:
                data = f.read()
            FileBlob.objects.create(name=name, content_type=mimetypes.guess_type(name)[0] or "", size=len(data), data=data)


def migrate_staff_files(apps, schema_editor):
    Staff = apps.get_model("staff", "Staff")
    FileBlob = apps.get_model("organization", "FileBlob")
    disk = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)

    for staff in Staff.objects.exclude(photo="").exclude(photo__isnull=True):
        name = staff.photo.name
        if not name or not disk.exists(name) or FileBlob.objects.filter(name=name).exists():
            continue
        with disk.open(name, "rb") as f:
            data = f.read()
        FileBlob.objects.create(name=name, content_type=mimetypes.guess_type(name)[0] or "", size=len(data), data=data)


def noop_reverse(apps, schema_editor):
    """Not reversible in any meaningful sense — the disk files this read
    from may no longer exist by the time anyone reverses this. Leaving the
    FileBlob rows in place on a reverse is strictly safer than deleting
    real, possibly-irreplaceable uploaded data."""


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0009_fileblob_alter_organization_building_photo_and_more"),
        ("staff", "0005_alter_staff_photo"),
    ]

    operations = [
        migrations.RunPython(migrate_organization_files, noop_reverse),
        migrations.RunPython(migrate_staff_files, noop_reverse),
    ]
