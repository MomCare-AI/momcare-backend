from django.contrib import admin

from momcare_platform.core.users.models import Role, User


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "description"]
    search_fields = ["code", "name"]


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ["email", "first_name", "last_name", "role", "organization", "is_active", "created_at"]
    list_filter = ["is_active", "is_staff", "role", "gender"]
    search_fields = ["email", "first_name", "last_name", "phone"]
    ordering = ["-created_at"]
    readonly_fields = ["created_at", "updated_at", "last_password_change"]
    exclude = ["password", "user_permissions", "groups", "locations"]
