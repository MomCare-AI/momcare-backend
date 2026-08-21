from django.contrib import admin

from momcare_platform.core.staff.models import Staff, StaffInvite


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ["employee_id", "user", "hospital", "is_active", "created_at"]
    list_filter = ["is_active", "user__role"]
    search_fields = ["employee_id", "user__email", "user__first_name", "user__last_name"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description="Hospital", ordering="user__organization__name")
    def hospital(self, obj: Staff):
        return obj.user.organization


@admin.register(StaffInvite)
class StaffInviteAdmin(admin.ModelAdmin):
    list_display = ["email", "organization", "role", "invite_status", "invited_by", "created_at"]
    list_filter = ["organization", "role"]
    search_fields = ["email", "first_name", "last_name", "organization__name"]
    readonly_fields = ["token", "created_at", "updated_at", "accepted_at", "accepted_user", "revoked_at"]
    ordering = ["-created_at"]

    @admin.display(description="Status")
    def invite_status(self, obj: StaffInvite) -> str:
        return obj.status

    def has_add_permission(self, request):
        # Invitations are issued by a hospital admin through the API, so the
        # inviting user and organization are always recorded. Creating one here
        # would bypass that.
        return False
