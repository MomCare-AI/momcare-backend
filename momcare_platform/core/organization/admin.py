from django.contrib import admin, messages
from django.utils.html import format_html

from momcare_platform.core.organization.models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Review queue for hospital applications.

    No public API verifies Pakistani facility licences, so approval is a human
    decision: check the licence against the issuing provincial commission's
    register, call the hospital on a number sourced independently of the form,
    then record what was checked in ``review_note``.
    """

    list_display = [
        "name",
        "status_badge",
        "city",
        "license_no",
        "license_authority",
        "owner",
        "created_at",
    ]
    list_filter = ["status", "license_authority", "is_active", "country"]
    search_fields = ["name", "email", "phone", "license_no"]
    ordering = ["status", "-created_at"]
    readonly_fields = ["created_at", "updated_at", "reviewed_at", "reviewed_by"]
    actions = ["approve_hospitals", "reject_hospitals", "suspend_hospitals"]

    def get_actions(self, request):
        """Drop Django's bulk delete — tenants are soft-deleted, never erased.

        A hospital row anchors its users, patients and audit log; destroying it
        would take the audit trail with it. Use Reject or Suspend to block
        access, or ``deactivate()`` to retire a hospital.
        """
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_delete_permission(self, request, obj=None):
        return False
    fieldsets = (
        (
            "Review",
            {
                "fields": ("status", "reviewed_by", "reviewed_at", "review_note"),
                "description": (
                    "Verify the licence against the issuing commission's public register "
                    "(PHC / SHCC / KP / Balochistan / IHRA), and confirm by phoning a number "
                    "you sourced independently — not the one typed into the form. "
                    "Record what you checked in the note."
                ),
            },
        ),
        ("Licence evidence", {"fields": ("license_no", "license_authority", "license_document")}),
        ("Identity", {"fields": ("name", "owner", "logo")}),
        ("Contact", {"fields": ("email", "phone")}),
        ("Address", {"fields": ("address_line1", "address_line2", "city", "state", "postal_code", "country")}),
        ("Settings", {"fields": ("timezone", "date_format", "established_date")}),
        ("Deactivation", {"fields": ("is_active", "deactivated_at", "deactivation_reason")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj: Organization) -> str:
        colors = {
            Organization.STATUS_PENDING: "#b45309",
            Organization.STATUS_APPROVED: "#15803d",
            Organization.STATUS_REJECTED: "#b91c1c",
            Organization.STATUS_SUSPENDED: "#6b7280",
        }
        return format_html(
            '<b style="color:{}">{}</b>',
            colors.get(obj.status, "#000"),
            obj.get_status_display(),
        )

    def save_model(self, request, obj, form, change):
        """Stamp the reviewer when status is changed from the edit form.

        The bulk actions go through ``set_review_status``; this covers the other
        path so a decision is never recorded without who made it and when.
        """
        status_changed = change and "status" in form.changed_data
        if status_changed:
            # Save the rest of the form with the *old* status, then let the model
            # apply the new one — so the reviewer stamp and the applicant
            # notification happen here exactly as they do for the bulk actions.
            new_status = obj.status
            obj.status = form.initial["status"]

        super().save_model(request, obj, form, change)

        if status_changed:
            obj.set_review_status(new_status, by=request.user, note=obj.review_note)

    def _review(self, request, queryset, status: str, verb: str) -> None:
        count = 0
        for org in queryset:
            org.set_review_status(status, by=request.user)
            count += 1
        self.message_user(request, f"{count} hospital(s) {verb}.", messages.SUCCESS)

    @admin.action(description="Approve selected hospitals (grants tenant access)")
    def approve_hospitals(self, request, queryset):
        self._review(request, queryset, Organization.STATUS_APPROVED, "approved")

    @admin.action(description="Reject selected hospitals")
    def reject_hospitals(self, request, queryset):
        self._review(request, queryset, Organization.STATUS_REJECTED, "rejected")

    @admin.action(description="Suspend selected hospitals (revokes access)")
    def suspend_hospitals(self, request, queryset):
        self._review(request, queryset, Organization.STATUS_SUSPENDED, "suspended")
