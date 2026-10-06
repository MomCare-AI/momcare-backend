"""Read-only admin. Care plans are clinical record: nothing is added by hand
here, nothing is editable, and nothing can be deleted -- staff edits go through
the API so each one is attributed and logged."""

from django.contrib import admin

from .models import (
    CarePlan,
    CarePlanAdjustment,
    CarePlanMedication,
    CarePlanNote,
    CarePlanSectionVersion,
    CareWeek,
    HospitalPreference,
    PlanCorrection,
    ReadingAdvice,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CarePlan)
class CarePlanAdmin(ReadOnlyAdmin):
    list_display = ["pregnancy", "month_number", "period_start", "period_end", "status", "last_outcome"]
    list_filter = ["status", "last_outcome"]


@admin.register(CarePlanSectionVersion)
class CarePlanSectionVersionAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "section", "model_name", "is_fallback", "created_at"]
    list_filter = ["section", "is_fallback"]


@admin.register(CarePlanAdjustment)
class CarePlanAdjustmentAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "section", "action", "item_key", "is_active", "created_at"]


@admin.register(CarePlanMedication)
class CarePlanMedicationAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "text", "is_active", "created_at"]


@admin.register(CarePlanNote)
class CarePlanNoteAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "text", "is_active", "created_at"]


@admin.register(PlanCorrection)
class PlanCorrectionAdmin(ReadOnlyAdmin):
    list_display = ["organization", "region", "trimester", "section", "item_key", "action", "edited_by", "created_at"]
    list_filter = ["section", "action", "region"]


@admin.register(HospitalPreference)
class HospitalPreferenceAdmin(ReadOnlyAdmin):
    list_display = ["organization", "region", "trimester", "section", "item_key", "status", "supporting_staff"]
    list_filter = ["status", "section", "region"]


@admin.register(CareWeek)
class CareWeekAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "week_number", "week_start", "week_end", "replans", "worse_readings"]


@admin.register(ReadingAdvice)
class ReadingAdviceAdmin(ReadOnlyAdmin):
    list_display = ["care_plan", "risk_level", "is_fallback", "created_at"]
    list_filter = ["risk_level", "is_fallback"]
