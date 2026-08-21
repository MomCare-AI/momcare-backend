from django.contrib import admin

from momcare_platform.core.patients.models import Consent, Patient, Pregnancy, PregnancyRiskFactors


class PregnancyInline(admin.TabularInline):
    model = Pregnancy
    extra = 0
    fields = ["status", "lmp", "edd", "edd_source", "gravida", "para", "assigned_staff"]
    show_change_link = True
    # Historical clinical fact — correctable, never removable.
    can_delete = False


class ConsentInline(admin.TabularInline):
    model = Consent
    extra = 0
    fields = ["status", "version", "method", "recorded_by", "recorded_at"]
    readonly_fields = ["recorded_at"]
    can_delete = False
    ordering = ["-recorded_at"]


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["mrn", "full_name", "hospital", "phone", "app_account", "is_active", "created_at"]
    list_filter = ["is_active", "blood_group", "location__organization"]
    search_fields = ["mrn", "first_name", "last_name", "phone", "cnic"]
    readonly_fields = ["mrn", "created_at", "updated_at"]
    inlines = [PregnancyInline, ConsentInline]
    fieldsets = (
        ("Identity", {"fields": ("mrn", "first_name", "last_name", "date_of_birth", "gender")}),
        ("Contact", {"fields": ("phone", "cnic", "blood_group")}),
        (
            "Emergency contact",
            {"fields": ("emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation")},
        ),
        ("Placement", {"fields": ("location", "user")}),
        ("Status", {"fields": ("is_active", "deactivated_at", "deactivation_reason")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Hospital", ordering="location__organization__name")
    def hospital(self, obj: Patient):
        return obj.location.organization

    @admin.display(boolean=True, description="App account")
    def app_account(self, obj: Patient) -> bool:
        return obj.has_app_account

    def has_delete_permission(self, request, obj=None):
        # Clinical records are deactivated, never erased.
        return False


@admin.register(Pregnancy)
class PregnancyAdmin(admin.ModelAdmin):
    list_display = ["patient", "status", "gestational_age_display", "edd", "edd_source", "assigned_staff"]
    list_filter = ["status", "edd_source"]
    search_fields = ["patient__mrn", "patient__first_name", "patient__last_name"]
    readonly_fields = ["gestational_age_display", "created_at", "updated_at"]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PregnancyRiskFactors)
class PregnancyRiskFactorsAdmin(admin.ModelAdmin):
    list_display = ["pregnancy", *PregnancyRiskFactors.FACTOR_FIELDS]
    list_filter = PregnancyRiskFactors.FACTOR_FIELDS
    search_fields = ["pregnancy__patient__mrn", "pregnancy__patient__last_name"]


@admin.register(Consent)
class ConsentAdmin(admin.ModelAdmin):
    list_display = ["patient", "status", "version", "method", "recorded_by", "recorded_at"]
    list_filter = ["status", "method", "version"]
    search_fields = ["patient__mrn", "patient__last_name"]
    readonly_fields = ["patient", "status", "version", "method", "recorded_by", "recorded_at", "note"]

    def has_add_permission(self, request):
        # Consent is recorded through the API, where the acting user is captured.
        return False

    def has_delete_permission(self, request, obj=None):
        # Append-only: what was agreed, and when, must survive intact.
        return False
