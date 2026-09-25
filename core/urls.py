from django.urls import path

from .views import HospitalLogoView, HospitalSettingsView


urlpatterns = [
    path('settings/', HospitalSettingsView.as_view(), name='hospital-settings'),
    path('settings/logo/', HospitalLogoView.as_view(), name='hospital-logo'),
]
