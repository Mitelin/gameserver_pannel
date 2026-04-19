from django.urls import path
from . import views

app_name = "filemanager"

urlpatterns = [
    path("<slug:slug>/files/",          views.file_browser,   name="browser"),
    path("<slug:slug>/files/view/",     views.file_view,      name="view"),
    path("<slug:slug>/files/download/", views.file_download,  name="download"),
    path("<slug:slug>/files/delete/",   views.file_delete,    name="delete"),
    path("<slug:slug>/files/upload/",   views.file_upload,    name="upload"),
    path("<slug:slug>/files/mkdir/",    views.mkdir,          name="mkdir"),
    path("path-picker/",               views.path_picker_api, name="path_picker"),
]
