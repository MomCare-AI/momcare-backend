from django.contrib import admin

from momcare_platform.core.monitoring.models import (
    ClinicalTag,
    MonitoringNote,
    MonitoringSession,
    PatientStatus,
    StatusLabel,
)


@admin.register(ClinicalTag)
class ClinicalTagAdmin(admin.ModelAdmin):
    list_display = ["name", "color", "organization", "location", "created_at"]
    list_filter = ["organization", "location"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(MonitoringSession)
class MonitoringSessionAdmin(admin.ModelAdmin):
    list_display = ["patient", "pregnancy", "duration_seconds", "recorded_at", "added_by"]
    list_filter = ["recorded_at"]
    search_fields = ["patient__first_name", "patient__last_name", "patient__mrn"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "recorded_at"


@admin.register(MonitoringNote)
class MonitoringNoteAdmin(admin.ModelAdmin):
    list_display = ["patient", "pregnancy", "session", "recorded_at", "added_by"]
    list_filter = ["recorded_at", "left_voicemail", "two_way_communication", "tags"]
    search_fields = ["patient__first_name", "patient__last_name", "patient__mrn", "note"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "recorded_at"
    filter_horizontal = ["tags"]


@admin.register(StatusLabel)
class StatusLabelAdmin(admin.ModelAdmin):
    list_display = ["name", "color", "organization", "location", "created_at"]
    list_filter = ["organization", "location"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(PatientStatus)
class PatientStatusAdmin(admin.ModelAdmin):
    list_display = ["patient", "name", "color", "created_at", "added_by"]
    list_filter = ["created_at"]
    search_fields = ["patient__first_name", "patient__last_name", "patient__mrn", "name"]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"
