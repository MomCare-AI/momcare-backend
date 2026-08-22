from django.contrib import admin

from momcare_platform.core.monitoring.models import Device, RiskAssessment, VitalReading


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ["serial_number", "organization", "status", "wearer", "acquisition", "assigned_at"]
    list_filter = ["status", "acquisition", "organization"]
    search_fields = ["serial_number", "assigned_pregnancy__patient__mrn"]
    readonly_fields = ["assigned_at", "created_at", "updated_at"]

    @admin.display(description="Worn by")
    def wearer(self, obj: Device):
        return obj.assigned_pregnancy.patient.full_name if obj.assigned_pregnancy_id else "—"


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(admin.ModelAdmin):
    list_display = ["assessed_at", "patient", "level", "previous_level", "source", "acknowledged_by"]
    list_filter = ["level", "source", "assessed_at"]
    search_fields = ["pregnancy__patient__mrn", "pregnancy__patient__last_name"]
    readonly_fields = [f.name for f in RiskAssessment._meta.fields]
    date_hierarchy = "assessed_at"

    @admin.display(description="Patient")
    def patient(self, obj: RiskAssessment):
        return obj.pregnancy.patient.full_name

    def has_add_permission(self, request):
        # Assessments are produced by the scoring engine, never typed in.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VitalReading)
class VitalReadingAdmin(admin.ModelAdmin):
    list_display = ["recorded_at", "patient", "reading_type", "display_value", "source"]
    list_filter = ["reading_type", "source", "recorded_at"]
    search_fields = ["pregnancy__patient__mrn", "pregnancy__patient__last_name"]
    readonly_fields = [f.name for f in VitalReading._meta.fields]
    date_hierarchy = "recorded_at"

    @admin.display(description="Patient")
    def patient(self, obj: VitalReading):
        return obj.pregnancy.patient.full_name

    def has_add_permission(self, request):
        # Readings arrive through the API, which records their source and
        # device. Hand-adding one here would produce an observation with no
        # provenance.
        return False

    def has_change_permission(self, request, obj=None):
        # An observation of what happened at a moment in time is not editable;
        # a correction is a new reading.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
