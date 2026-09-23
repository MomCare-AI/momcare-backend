from django.contrib import admin, messages
from django.utils.html import format_html

from momcare_platform.core.organization.models import (
    AuditLog,
    Notification,
    Organization,
    OrganizationDeactivationRequest,
)
from momcare_platform.core.organization.services import (
    approve_deactivation_request,
    deactivate_organization,
    dismiss_deactivation_request,
    reactivate_organization,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Review queue for hospital applications.

    Approval is a human decision: verify the hospital's details, call it on a
    number sourced independently of the form, then record what was checked in
    ``review_note``.
    """

    list_display = [
        "name",
        "license_number",
        "status_badge",
        "city",
        "owner",
        "is_active",
        "created_at",
    ]
    list_filter = ["status", "is_active", "country"]
    search_fields = ["name", "email", "phone", "license_number"]
    ordering = ["status", "-created_at"]
    readonly_fields = ["created_at", "updated_at", "reviewed_at", "reviewed_by"]
    actions = [
        "approve_hospitals",
        "reject_hospitals",
        "suspend_hospitals",
        "deactivate_hospitals",
        "reactivate_hospitals",
    ]

    def get_actions(self, request, *args, **kwargs):
        """Drop Django's bulk delete — tenants are soft-deleted, never erased.

        A hospital row anchors its users, patients and audit log; destroying it
        would take the audit trail with it. Use Reject or Suspend to block
        access, or Deactivate to retire a hospital.

        ``*args, **kwargs`` rather than matching Django's exact signature here:
        newer Django versions added an ``action_location`` parameter, and
        accepting anything keeps this compatible across versions without
        depending on a type only django-stubs defines.
        """
        actions = super().get_actions(request, *args, **kwargs)
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
                    "Verify the hospital's details, and confirm by phoning a number you "
                    "sourced independently — not the one typed into the form. Record what "
                    "you checked in the note."
                ),
            },
        ),
        ("Licensing", {"fields": ("license_number", "license_image")}),
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

    @admin.action(description="Deactivate selected hospitals (retires platform access)")
    def deactivate_hospitals(self, request, queryset):
        count = 0
        for org in queryset:
            deactivate_organization(org, by=request.user, reason="Deactivated via admin.")
            count += 1
        self.message_user(request, f"{count} hospital(s) deactivated.", messages.SUCCESS)

    @admin.action(description="Reactivate selected hospitals")
    def reactivate_hospitals(self, request, queryset):
        count = 0
        for org in queryset:
            reactivate_organization(org)
            count += 1
        self.message_user(request, f"{count} hospital(s) reactivated.", messages.SUCCESS)


@admin.register(OrganizationDeactivationRequest)
class OrganizationDeactivationRequestAdmin(admin.ModelAdmin):
    """A hospital's own asks to close their account — reviewed the same way
    an application is: read the reason, decide, record it."""

    list_display = ["organization", "status_badge", "requested_by", "created_at", "reviewed_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["organization__name", "reason"]
    ordering = ["status", "-created_at"]
    readonly_fields = [
        "organization",
        "requested_by",
        "reason",
        "created_at",
        "updated_at",
        "reviewed_by",
        "reviewed_at",
    ]
    actions = ["approve_requests", "dismiss_requests"]

    def has_add_permission(self, request):
        # Only a hospital can create one, through its own portal.
        return False

    def has_delete_permission(self, request, obj=None):
        # The record of what was asked, and what was decided, is the point.
        return False

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj: OrganizationDeactivationRequest) -> str:
        colors = {
            OrganizationDeactivationRequest.STATUS_PENDING: "#b45309",
            OrganizationDeactivationRequest.STATUS_APPROVED: "#b91c1c",
            OrganizationDeactivationRequest.STATUS_DISMISSED: "#6b7280",
        }
        return format_html(
            '<b style="color:{}">{}</b>',
            colors.get(obj.status, "#000"),
            obj.get_status_display(),
        )

    @admin.action(description="Approve selected requests (deactivates the hospital)")
    def approve_requests(self, request, queryset):
        count = 0
        for deactivation_request in queryset.filter(status=OrganizationDeactivationRequest.STATUS_PENDING):
            approve_deactivation_request(deactivation_request, by=request.user)
            count += 1
        self.message_user(
            request, f"{count} request(s) approved; those hospitals are now deactivated.", messages.SUCCESS
        )

    @admin.action(description="Dismiss selected requests (hospital stays active)")
    def dismiss_requests(self, request, queryset):
        count = 0
        for deactivation_request in queryset.filter(status=OrganizationDeactivationRequest.STATUS_PENDING):
            dismiss_deactivation_request(deactivation_request, by=request.user)
            count += 1
        self.message_user(request, f"{count} request(s) dismissed.", messages.SUCCESS)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """A compliance trail, read-only by design.

    ``AuditLog.Meta.default_permissions`` already omits "change" and
    "delete" at the permission-object level; the explicit overrides below
    remove the corresponding admin UI too, so there is no button that would
    404 or silently no-op. Writing happens exactly once, from
    ``AuditLogMiddleware``, on every PHI-touching request — never from here.
    """

    list_display = ["timestamp", "user", "action", "resource", "resource_id", "ip_address"]
    list_filter = ["action", "resource", "timestamp"]
    search_fields = ["user__email", "resource_id", "endpoint"]
    date_hierarchy = "timestamp"
    ordering = ["-timestamp"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only, same reasoning as AuditLog above -- these are only ever
    written by ``organization.signals``, never by hand."""

    list_display = ["organization", "notification_type", "message", "is_read", "created_at"]
    list_filter = ["notification_type", "is_read", "created_at"]
    search_fields = ["organization__name", "message"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
