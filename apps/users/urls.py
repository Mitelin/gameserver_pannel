from django.urls import path
from . import views

app_name = "users"

urlpatterns = [
    path("",            views.user_list,   name="list"),
    path("new/",        views.user_create, name="create"),
    path("<int:user_id>/", views.user_edit, name="edit"),
]
