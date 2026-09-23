"""Object-level permission for monitoring records: edit/delete is
restricted to the record's own author, or a hospital_admin -- role gating
(who may create/list at all) is handled separately by the ``HasRole``
classes in ``core.common.permissions``.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import BasePermission

from momcare_platform.core.common.permissions import user_role_code


class IsOwnerOrHospitalAdmin(BasePermission):
    def has_permission(self, request, view) -> bool:
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if user.is_superuser or user_role_code(user) == settings.ROLE_HOSPITAL_ADMIN:
            return True
        return obj.added_by_id == user.id
