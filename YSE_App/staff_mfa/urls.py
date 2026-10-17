"""Two-factor URLs; included from YSE_App/urls.py only when STAFF_MFA_REQUIRED is on."""

from django.urls import path

from YSE_App.staff_mfa import views

urlpatterns = [
    path('mfa/verify/', views.verify, name='staff_mfa_verify'),
    path('mfa/setup/', views.setup, name='staff_mfa_setup'),
    path('mfa/backup-codes/', views.backup_codes, name='staff_mfa_backup_codes'),
]
