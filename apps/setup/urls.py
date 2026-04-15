from django.urls import path
from . import views

app_name = "setup"

urlpatterns = [
    path("",          views.setup_index,     name="index"),
    path("<int:step>/", views.setup_step_view, name="step"),
]
