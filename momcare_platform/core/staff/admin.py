from django.contrib import admin

from momcare_platform.core.staff.models import SecondaryProvider, Staff


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ["employee_id", "user", "hospital", "location_names", "is_active", "created_at"]
    list_filter = ["is_active", "user__role", "user__locations"]
    search_fields = ["employee_id", "user__email", "user__first_name", "user__last_name"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description="Hospital", ordering="user__organization__name")
    def hospital(self, obj: Staff):
        return obj.user.organization

    @admin.display(description="Locations")
    def location_names(self, obj: Staff) -> str:
        return ", ".join(obj.user.locations.values_list("name", flat=True))


@admin.register(SecondaryProvider)
class SecondaryProviderAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "phone", "email", "affiliation"]
    list_filter = ["organization"]
    search_fields = ["name", "email", "phone", "affiliation"]
    readonly_fields = ["created_at", "updated_at"]
