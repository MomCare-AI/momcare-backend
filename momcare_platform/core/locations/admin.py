from django.contrib import admin, messages

from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import LocationError, deactivate_location, reactivate_location


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    """A hospital's own sites. Created/managed through its own portal in the
    normal case (see ``core/locations/api/views.py``); this exists for
    platform-admin visibility and manual correction, not as the primary path.
    """

    list_display = ["name", "organization", "location_manager", "is_active", "active_patient_count", "created_at"]
    search_fields = ["name", "organization__name", "city"]
    list_filter = ["is_active", "organization"]
    raw_id_fields = ["organization", "location_manager", "deactivated_by"]
    readonly_fields = ["active_patient_count", "deactivated_at", "created_at", "updated_at"]
    actions = ["deactivate_locations", "reactivate_locations"]

    fieldsets = (
        (None, {"fields": ("organization", "name", "location_manager", "timezone")}),
        ("Contact", {"fields": ("phone", "email")}),
        ("Address", {"fields": ("address_line1", "address_line2", "city", "state", "postal_code", "country")}),
        ("Settings", {"fields": ("date_format",)}),
        ("Deactivation", {"fields": ("is_active", "deactivated_at", "deactivated_by", "deactivation_reason")}),
        ("Stats & timestamps", {"fields": ("active_patient_count", "created_at", "updated_at")}),
    )

    def has_delete_permission(self, request, obj=None):
        # Sites are never physically deleted — only deactivated.
        return False

    @admin.action(description="Deactivate selected (only if 0 active patients)")
    def deactivate_locations(self, request, queryset):
        done, blocked = 0, []
        for location in queryset:
            try:
                deactivate_location(location, by=request.user, reason="Deactivated via admin.")
                done += 1
            except LocationError:
                blocked.append(location.name)
        if done:
            self.message_user(request, f"{done} location(s) deactivated.", messages.SUCCESS)
        if blocked:
            self.message_user(
                request,
                f"Skipped (active patients present): {', '.join(blocked)}",
                messages.ERROR,
            )

    @admin.action(description="Reactivate selected")
    def reactivate_locations(self, request, queryset):
        count = 0
        for location in queryset:
            reactivate_location(location)
            count += 1
        self.message_user(request, f"{count} location(s) reactivated.", messages.SUCCESS)
