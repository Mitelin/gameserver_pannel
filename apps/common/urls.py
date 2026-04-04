# apps/common/urls.py
from django.urls import path
from .views.health import HealthView

app_name = "common"

urlpatterns = [
    path("", HealthView.as_view(), name="health"),
]
