from django.urls import path

from . import views

app_name = "search"

urlpatterns = [
    path("", views.search_view, name="search"),
    path("favorites/", views.favorites_view, name="favorites"),
    path("subscriptions/", views.subscriptions_view, name="subscriptions"),
    path("playlist/", views.playlist_view, name="playlist"),
    path("playlists/save/", views.save_playlist_view, name="save_playlist"),
    path("export/", views.export_view, name="export"),
    path("import/", views.import_view, name="import"),
    path("api/resolve/", views.resolve_view, name="resolve"),
    path("api/playlist/", views.playlist_api_view, name="playlist_api"),
    path("api/stream/", views.stream_view, name="stream"),
    path("api/hls/", views.hls_view, name="hls"),
    path("api/thumb/", views.thumbnail_view, name="thumbnail"),
]
