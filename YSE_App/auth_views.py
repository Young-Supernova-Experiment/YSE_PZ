"""Password reset with an optional fixed link domain (PASSWORD_RESET_DOMAIN)."""

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.views.generic.edit import FormView


class PasswordResetView(auth_views.PasswordResetView):
    """django.contrib.auth's view; with PASSWORD_RESET_DOMAIN set, the emailed link uses that host."""

    def form_valid(self, form):
        domain = (getattr(settings, 'PASSWORD_RESET_DOMAIN', '') or '').strip()
        if not domain:
            return super().form_valid(form)
        form.save(
            domain_override=domain,
            use_https=bool(getattr(settings, 'PASSWORD_RESET_USE_HTTPS', True)),
            token_generator=self.token_generator,
            from_email=self.from_email,
            email_template_name=self.email_template_name,
            subject_template_name=self.subject_template_name,
            request=self.request,
            html_email_template_name=self.html_email_template_name,
            extra_email_context=self.extra_email_context,
        )
        return FormView.form_valid(self, form)
