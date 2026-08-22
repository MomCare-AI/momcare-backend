from django.contrib import admin

from momcare_platform.core.alerts.models import Alert, AlertEvent


class AlertEventInline(admin.TabularInline):
    model = AlertEvent
    extra = 0
    fields = ["created_at", "kind", "tier", "detail", "actor"]
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ["raised_at", "patient", "level", "status", "tier", "acknowledged_by", "resolution"]
    list_filter = ["status", "level", "tier", "raised_at"]
    search_fields = ["pregnancy__patient__mrn", "pregnancy__patient__last_name"]
    readonly_fields = [f.name for f in Alert._meta.fields]
    date_hierarchy = "raised_at"
    inlines = [AlertEventInline]

    @admin.display(description="Patient")
    def patient(self, obj: Alert):
        return obj.pregnancy.patient.full_name

    def has_add_permission(self, request):
        # Alerts are raised by the scoring engine. One typed in by hand would
        # have no assessment behind it and no reason a clinician could check.
        return False

    def has_change_permission(self, request, obj=None):
        # Responding happens in the portal, where it is attributed and written
        # to the event history. Editing the row here would bypass both.
        return False

    def has_delete_permission(self, request, obj=None):
        # The record of who was told, and whether anyone answered, is the
        # entire point of this table.
        return False


@admin.register(AlertEvent)
class AlertEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "alert", "kind", "tier", "actor", "detail"]
    list_filter = ["kind", "created_at"]
    search_fields = ["detail", "alert__pregnancy__patient__last_name"]
    readonly_fields = [f.name for f in AlertEvent._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
