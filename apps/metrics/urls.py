# apps/metrics/urls.py
from django.urls import path
from .views import MetricsAPIView

app_name = "metrics"

urlpatterns = [
    path("<slug:slug>/metrics/", MetricsAPIView.as_view(), name="api"),
]
