"""Admin action on Users: reset two-factor sign-in (superusers only, #512).

Installed from YSE_App/admin.py only when STAFF_MFA_REQUIRED is on. It extends
whichever admin class is registered for the user model (django.contrib.auth's
UserAdmin by default) rather than replacing it.
"""

from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from YSE_App import staff_mfa

ACTION_NAME = 'reset_two_factor'


def install_user_admin_action(site=admin.site):
    user_model = get_user_model()
    current = site._registry.get(user_model)
    base = type(current) if current is not None else UserAdmin

    class StaffMFAUserAdmin(base):
        actions = list(getattr(base, 'actions', None) or []) + [ACTION_NAME]

        def get_actions(self, request):
            actions = super().get_actions(request)
            if not request.user.is_superuser:
                actions.pop(ACTION_NAME, None)
            return actions

        @admin.action(description='Reset two-factor sign-in (user sets up a new authenticator)')
        def reset_two_factor(self, request, queryset):
            if not request.user.is_superuser:
                self.message_user(request, 'Only superusers can reset two-factor sign-in.', messages.ERROR)
                return
            users = list(queryset)
            for user in users:
                staff_mfa.reset_devices(user)
            self.message_user(request, 'Two-factor sign-in reset for %d account(s).' % len(users), messages.SUCCESS)

    StaffMFAUserAdmin.__name__ = 'StaffMFA' + base.__name__
    if current is not None:
        site.unregister(user_model)
    site.register(user_model, StaffMFAUserAdmin)
    return StaffMFAUserAdmin
