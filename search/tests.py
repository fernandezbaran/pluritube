"""Pruebas unitarias de la lógica de playlists (sin red ni yt-dlp).

Cubren los ayudantes puros de ``search.library`` que arreglan las playlists
Mix y los mensajes de error, además de los ayudantes de sesión.
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import library, player, thumbnails, views
from .player import PlayerError, unsign_stream


class NormalizePlaylistUrlTests(SimpleTestCase):
    def test_classic_mix_url_is_rewritten_to_watch(self):
        url = "https://www.youtube.com/playlist?list=RDdQw4w9WgXcQ"
        self.assertEqual(
            library._normalize_playlist_url(url),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ",
        )

    def test_mix_url_with_extra_params_is_rewritten(self):
        url = "https://www.youtube.com/playlist?list=RDLmMcPxyz123&foo=bar"
        self.assertEqual(
            library._normalize_playlist_url(url),
            "https://www.youtube.com/watch?v=LmMcPxyz123&list=RDLmMcPxyz123",
        )

    def test_regular_playlist_is_untouched(self):
        url = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
        self.assertEqual(library._normalize_playlist_url(url), url)

    def test_watch_url_is_untouched(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890"
        self.assertEqual(library._normalize_playlist_url(url), url)

    def test_short_or_non_matching_id_is_untouched(self):
        url = "https://www.youtube.com/playlist?list=RDshort"
        self.assertEqual(library._normalize_playlist_url(url), url)


class FriendlyPlaylistErrorTests(SimpleTestCase):
    def test_unviewable_mix_hint(self):
        message = "ERROR: [youtube:tab] RDxxx: YouTube said: This playlist type is unviewable."
        self.assertIn("Mix", library._friendly_playlist_error(message))

    def test_missing_playlist(self):
        message = "ERROR: [youtube:tab] PLxxx: YouTube said: The playlist does not exist."
        self.assertEqual(
            library._friendly_playlist_error(message),
            "La playlist no existe, es privada o ha sido eliminada.",
        )

    def test_http_error(self):
        message = "ERROR: [youtube:tab] PLxxx: Unable to download API page: HTTP Error 400"
        self.assertIn("rechazó", library._friendly_playlist_error(message))

    def test_unsupported_url(self):
        message = "ERROR: Unsupported URL: not-a-url"
        self.assertIn("compatible", library._friendly_playlist_error(message))

    def test_unknown_error_strips_ytdlp_prefixes(self):
        self.assertEqual(
            library._friendly_playlist_error("ERROR: [generic] abcdefghij: algo raro"),
            "algo raro",
        )

    def test_empty_message_has_default(self):
        self.assertEqual(
            library._friendly_playlist_error(""),
            "No se pudo leer la playlist.",
        )


class SessionPlaylistTests(SimpleTestCase):
    def setUp(self):
        self.session = {}

    def test_add_and_get_playlist(self):
        library.add_playlist(
            self.session,
            {"title": "Mi lista", "link": "https://example.com/p1"},
        )
        playlists = library.get_playlists(self.session)
        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]["title"], "Mi lista")
        self.assertEqual(playlists[0]["kind"], "playlist")

    def test_add_playlist_deduplicates_by_link(self):
        for _ in range(3):
            library.add_playlist(
                self.session,
                {"title": "Duplicada", "link": "https://example.com/same"},
            )
        self.assertEqual(len(library.get_playlists(self.session)), 1)

    def test_add_playlist_ignores_empty_link(self):
        library.add_playlist(self.session, {"title": "Sin enlace", "link": ""})
        self.assertEqual(library.get_playlists(self.session), [])

    def test_remove_playlist(self):
        library.add_playlist(self.session, {"link": "https://example.com/a"})
        library.add_playlist(self.session, {"link": "https://example.com/b"})
        library.remove_playlist(self.session, "https://example.com/a")
        links = {p["link"] for p in library.get_playlists(self.session)}
        self.assertEqual(links, {"https://example.com/b"})

    def test_playlist_links_returns_set_of_links(self):
        library.add_playlist(self.session, {"link": "https://example.com/a"})
        self.assertEqual(library.playlist_links(self.session), {"https://example.com/a"})


class _FakeResponse:
    """Respuesta mínima de ``requests`` para el proxy de miniaturas."""

    def __init__(self, content=b"image-bytes", content_type="image/jpeg", status=200):
        self._content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=0):
        yield self._content

    def close(self):
        pass


class ThumbnailProxyTests(SimpleTestCase):
    def test_cache_key_is_stable_and_unique(self):
        self.assertEqual(
            thumbnails.cache_key("https://a/1.jpg"),
            thumbnails.cache_key("https://a/1.jpg"),
        )
        self.assertNotEqual(
            thumbnails.cache_key("https://a/1.jpg"),
            thumbnails.cache_key("https://a/2.jpg"),
        )

    def test_rejects_private_host(self):
        with self.assertRaises(PlayerError):
            thumbnails.get_thumbnail("http://127.0.0.1/secret.png")

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(PlayerError):
            thumbnails.get_thumbnail("file:///etc/passwd")

    def test_downloads_and_caches(self):
        with TemporaryDirectory() as tmp, patch.object(
            thumbnails, "CACHE_DIR", Path(tmp)
        ), patch.object(
            thumbnails, "validate_source_url", lambda url: url
        ), patch.object(
            thumbnails.requests, "get", return_value=_FakeResponse(b"abc")
        ) as get:
            data, content_type = thumbnails.get_thumbnail("https://cdn.test/a.jpg")
            self.assertEqual(data, b"abc")
            self.assertEqual(content_type, "image/jpeg")

            # La segunda llamada sale del caché: no vuelve a descargar.
            data2, _ = thumbnails.get_thumbnail("https://cdn.test/a.jpg")
            self.assertEqual(data2, b"abc")
            self.assertEqual(get.call_count, 1)

    def test_non_image_is_rejected(self):
        with TemporaryDirectory() as tmp, patch.object(
            thumbnails, "CACHE_DIR", Path(tmp)
        ), patch.object(
            thumbnails, "validate_source_url", lambda url: url
        ), patch.object(
            thumbnails.requests,
            "get",
            return_value=_FakeResponse(b"<html>", content_type="text/html"),
        ):
            with self.assertRaises(PlayerError):
                thumbnails.get_thumbnail("https://cdn.test/page")

    def test_expired_cache_is_refetched(self):
        with TemporaryDirectory() as tmp, patch.object(
            thumbnails, "CACHE_DIR", Path(tmp)
        ), patch.object(
            thumbnails, "CACHE_TTL", 0
        ), patch.object(
            thumbnails, "validate_source_url", lambda url: url
        ), patch.object(
            thumbnails.requests, "get", return_value=_FakeResponse(b"abc")
        ) as get:
            thumbnails.get_thumbnail("https://cdn.test/a.jpg")
            thumbnails.get_thumbnail("https://cdn.test/a.jpg")
            self.assertEqual(get.call_count, 2)

    def test_prune_removes_oldest(self):
        with TemporaryDirectory() as tmp, patch.object(
            thumbnails, "CACHE_DIR", Path(tmp)
        ), patch.object(thumbnails, "CACHE_MAX_FILES", 2):
            base = Path(tmp)
            for index in range(3):
                data = base / f"key{index}"
                data.write_bytes(b"x")
                (base / f"key{index}.json").write_text("{}")
                os.utime(data, (1000 + index, 1000 + index))
            thumbnails._prune()
            remaining = sorted(
                entry.name
                for entry in base.iterdir()
                if not entry.name.endswith(".json")
            )
            self.assertEqual(remaining, ["key1", "key2"])


class ChannelNameTests(SimpleTestCase):
    def test_youtube_channel_uses_browse_id(self):
        video = {
            "ownerText": {
                "runs": [
                    {
                        "text": "Canal",
                        "navigationEndpoint": {
                            "browseEndpoint": {
                                "browseId": "UCabc",
                                "canonicalBaseUrl": "/@canal",
                            }
                        },
                    }
                ]
            }
        }
        self.assertEqual(
            views._youtube_channel(video),
            ("Canal", "https://www.youtube.com/channel/UCabc", "yt:UCabc"),
        )

    def test_youtube_channel_falls_back_to_canonical(self):
        video = {
            "longBylineText": {
                "runs": [
                    {
                        "text": "Otro",
                        "navigationEndpoint": {
                            "browseEndpoint": {"canonicalBaseUrl": "/@otro"}
                        },
                    }
                ]
            }
        }
        name, link, key = views._youtube_channel(video)
        self.assertEqual(name, "Otro")
        self.assertEqual(link, "https://www.youtube.com/@otro")
        self.assertEqual(key, "yt-url:www.youtube.com/@otro")

    def test_youtube_channel_absent(self):
        self.assertEqual(views._youtube_channel({}), ("", "", ""))

    def test_sepia_channel(self):
        item = {
            "channel": {
                "displayName": "Music",
                "url": "https://pt.test/video-channels/music",
            }
        }
        self.assertEqual(
            views._sepia_channel(item),
            {
                "channel": "Music",
                "channel_link": "https://pt.test/video-channels/music",
                "channel_key": "pt:pt.test/video-channels/music",
            },
        )

    def test_sepia_channel_rebuilds_url_from_host(self):
        item = {"channel": {"name": "music", "host": "pt.test"}}
        out = views._sepia_channel(item)
        self.assertEqual(out["channel_link"], "https://pt.test/video-channels/music")
        self.assertEqual(out["channel_key"], "pt:pt.test/video-channels/music")

    def test_sepia_channel_without_channel(self):
        self.assertEqual(views._sepia_channel({})["channel"], "")


class ChannelKeyTests(SimpleTestCase):
    def test_youtube_channel_url(self):
        self.assertEqual(
            library.channel_key_from_url("https://www.youtube.com/channel/UCxyz"),
            "yt:UCxyz",
        )

    def test_youtube_handle_url(self):
        self.assertEqual(
            library.channel_key_from_url("https://www.youtube.com/@foo"),
            "yt-url:www.youtube.com/@foo",
        )

    def test_peertube_channel_url(self):
        self.assertEqual(
            library.channel_key_from_url("https://pt.test/video-channels/music"),
            "pt:pt.test/video-channels/music",
        )

    def test_empty_url(self):
        self.assertEqual(library.channel_key_from_url(""), "")
        self.assertEqual(library.channel_key_from_url(None), "")


class ImportConfigTests(SimpleTestCase):
    def setUp(self):
        self.session = {}

    def test_imports_three_sections(self):
        counts = library.import_config(
            self.session,
            {
                "favorites": [{"title": "F", "link": "https://a/1"}],
                "subscriptions": [{"feed_url": "https://a/feed.xml"}],
                "playlists": [{"title": "P", "link": "https://a/pl"}],
            },
        )
        self.assertEqual(
            counts, {"favorites": 1, "subscriptions": 1, "playlists": 1}
        )

    def test_import_deduplicates(self):
        payload = {
            "favorites": [{"link": "https://a/1"}],
            "subscriptions": [{"feed_url": "https://a/feed.xml"}],
            "playlists": [{"link": "https://a/pl"}],
        }
        library.import_config(self.session, payload)
        counts = library.import_config(self.session, payload)
        self.assertEqual(
            counts, {"favorites": 0, "subscriptions": 0, "playlists": 0}
        )

    def test_ignores_non_http_and_bad_items(self):
        counts = library.import_config(
            self.session,
            {
                "favorites": [{"link": "ftp://nope"}, "not-a-dict"],
                "subscriptions": [{"feed_url": "javascript:alert(1)"}],
                "playlists": [{"link": ""}],
            },
        )
        self.assertEqual(
            counts, {"favorites": 0, "subscriptions": 0, "playlists": 0}
        )

    def test_subscription_channel_key_backfilled(self):
        library.import_config(
            self.session,
            {
                "subscriptions": [
                    {
                        "feed_url": "https://pt.test/feeds/videos.xml",
                        "channel_url": "https://pt.test/video-channels/music",
                    }
                ]
            },
        )
        subs = library.get_subscriptions(self.session)
        self.assertEqual(subs[0]["channel_key"], "pt:pt.test/video-channels/music")

    def test_subscription_keys(self):
        library.add_subscription(
            self.session,
            {"feed_url": "https://a/feed.xml", "channel_key": "yt:UC1"},
        )
        self.assertEqual(library.subscription_keys(self.session), {"yt:UC1"})


class HlsRewriteTests(SimpleTestCase):
    PLAYLIST = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            '#EXT-X-MAP:URI="init-480-fragmented.mp4",BYTERANGE="1378@0"',
            "#EXTINF:10.4,",
            "#EXT-X-BYTERANGE:1200749@1378",
            "seg-480-fragmented.mp4",
        ]
    )

    def _out(self):
        return views._rewrite_hls_playlist(
            self.PLAYLIST, "https://cdn.test/hls/480.m3u8", {"User-Agent": "test"}
        ).splitlines()

    def test_tags_without_uri_are_kept(self):
        out = self._out()
        self.assertEqual(out[0], "#EXTM3U")
        self.assertEqual(out[1], "#EXT-X-VERSION:7")
        self.assertEqual(out[3], "#EXTINF:10.4,")

    def test_segment_uri_is_proxied(self):
        out = self._out()
        self.assertTrue(out[5].startswith("/api/hls/?t="))
        payload = unsign_stream(
            parse_qs(urlparse(out[5]).query)["t"][0]
        )
        self.assertEqual(
            payload["url"], "https://cdn.test/hls/seg-480-fragmented.mp4"
        )

    def test_map_uri_is_proxied(self):
        out = self._out()
        self.assertIn('URI="/api/hls/?t=', out[2])
        token = out[2].split('URI="')[1].split('"')[0]
        payload = unsign_stream(parse_qs(urlparse(token).query)["t"][0])
        self.assertEqual(payload["url"], "https://cdn.test/hls/init-480-fragmented.mp4")


class NormalizedContentTypeTests(SimpleTestCase):
    def test_octet_stream_becomes_mp4(self):
        self.assertEqual(
            views._normalized_content_type("application/octet-stream"), "video/mp4"
        )
        self.assertEqual(
            views._normalized_content_type("binary/octet-stream"), "video/mp4"
        )

    def test_missing_becomes_mp4(self):
        self.assertEqual(views._normalized_content_type(None), "video/mp4")

    def test_real_type_is_kept(self):
        self.assertEqual(
            views._normalized_content_type("video/mp4; charset=utf-8"), "video/mp4"
        )
        self.assertEqual(views._normalized_content_type("video/webm"), "video/webm")


class ExtractMediaKindTests(SimpleTestCase):
    @patch.object(player, "_run_ytdlp")
    def test_progressive_stays_file(self, run):
        run.return_value = {
            "url": "https://cdn.test/web-videos/a-720.mp4",
            "protocol": "https",
            "title": "A",
        }
        media = player.extract_media("https://pt.test/videos/watch/a")
        self.assertEqual(media["kind"], "file")
        self.assertEqual(run.call_count, 1)

    @patch.object(player, "_run_ytdlp")
    def test_fragmented_falls_back_to_hls(self, run):
        run.side_effect = [
            {
                "url": "https://cdn.test/streaming-playlists/hls/a/x-480-fragmented.mp4",
                "protocol": "https",
                "title": "A",
            },
            {
                "url": "https://cdn.test/streaming-playlists/hls/a/x.m3u8",
                "protocol": "m3u8_native",
                "title": "A",
            },
        ]
        media = player.extract_media("https://pt.test/videos/watch/a")
        self.assertEqual(media["kind"], "hls")
        self.assertEqual(
            media["url"], "https://cdn.test/streaming-playlists/hls/a/x.m3u8"
        )
        self.assertEqual(run.call_count, 2)

    @patch.object(player, "_run_ytdlp")
    def test_m3u8_protocol_is_hls(self, run):
        run.return_value = {
            "url": "https://cdn.test/x.m3u8",
            "protocol": "m3u8_native",
            "title": "X",
        }
        media = player.extract_media("https://pt.test/videos/watch/x")
        self.assertEqual(media["kind"], "hls")

    def test_looks_like_fragmented_mp4(self):
        self.assertTrue(player._looks_like_fragmented_mp4("https://x/y-480-fragmented.mp4"))
        self.assertTrue(
            player._looks_like_fragmented_mp4("https://x/streaming-playlists/hls/y/z")
        )
        self.assertFalse(player._looks_like_fragmented_mp4("https://x/web-videos/a.mp4"))
        self.assertFalse(player._looks_like_fragmented_mp4(""))


class PageBaseTests(SimpleTestCase):
    def test_removes_page_and_keeps_filters(self):
        request = SimpleNamespace(GET=QueryDict("filter=youtube&channel=x&page=3"))
        self.assertEqual(views._page_base(request), "filter=youtube&channel=x&")

    def test_empty_when_only_page(self):
        request = SimpleNamespace(GET=QueryDict("page=2"))
        self.assertEqual(views._page_base(request), "")

    def test_excludes_several_keys(self):
        request = SimpleNamespace(GET=QueryDict("url=u&page=2&spage=4"))
        self.assertEqual(views._page_base(request, "page", "spage"), "url=u&")


class PaginationViewTests(TestCase):
    """Paginación sin JS de favoritos, suscripciones y playlists."""

    def _set_session(self, key, value):
        session = self.client.session
        session[key] = value
        session.save()

    def _favorite(self, index):
        return {
            "title": f"Vídeo {index}",
            "link": f"https://videos.test/{index}",
            "thumbnail": "",
            "source": "YouTube",
            "color": "#ff0000",
        }

    def test_favorites_paginate(self):
        total = views.RESULTS_PER_PAGE * 3 + 4
        self._set_session("favorites", [self._favorite(i) for i in range(total)])
        response = self.client.get(reverse("search:favorites"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), views.RESULTS_PER_PAGE)
        self.assertEqual(response.context["page_obj"].paginator.count, total)

    def test_favorites_last_and_out_of_range_page(self):
        self._set_session("favorites", [self._favorite(i) for i in range(12)])
        page = self.client.get(reverse("search:favorites"), {"page": 2})
        self.assertEqual(len(page.context["page_obj"]), 2)
        # Fuera de rango: Django devuelve la última página.
        last = self.client.get(reverse("search:favorites"), {"page": 99})
        self.assertEqual(last.context["page_obj"].number, 2)

    def _subscription(self):
        return {
            "feed_url": "https://feeds.test/a.xml",
            "title": "Canal",
            "source": "YouTube",
            "channel_url": "https://www.youtube.com/channel/UCx",
            "channel_key": "yt:UCx",
        }

    def _feed_entry(self, index):
        return {
            "title": f"Feed {index}",
            "link": f"https://feeds.test/watch/{index}",
            "thumbnail": "",
            "description": "",
            "source": "Canal",
            "color": "#ff0000",
            "published": "2026-01-01T00:00:00+00:00",
        }

    @patch.object(views.library, "fetch_feed")
    def test_subscriptions_paginate_and_keep_filter(self, fetch_feed):
        total = views.RESULTS_PER_PAGE * 2 + 3
        fetch_feed.return_value = {
            "title": "Canal",
            "entries": [self._feed_entry(i) for i in range(total)],
        }
        self._set_session("subscriptions", [self._subscription()])
        response = self.client.get(
            reverse("search:subscriptions"), {"page": 3, "filter": "youtube"}
        )
        self.assertEqual(len(response.context["page_obj"]), 3)
        self.assertEqual(response.context["visible_count"], total)
        self.assertEqual(response.context["page_base"], "filter=youtube&")
        # "Reproducir todo" conserva la lista filtrada completa.
        self.assertEqual(len(response.context["playlist_data"]["entries"]), total)

    @patch.object(views.library, "extract_playlist")
    def test_playlist_entries_paginate(self, extract_playlist):
        total = views.RESULTS_PER_PAGE + 5
        extract_playlist.return_value = {
            "title": "Lista",
            "entries": [
                {
                    "title": f"Entrada {i}",
                    "link": f"https://videos.test/{i}",
                    "thumbnail": "",
                    "description": "",
                    "source": "Playlist",
                    "color": "#6c71c4",
                }
                for i in range(total)
            ],
        }
        with patch.object(views, "validate_source_url", lambda url: url):
            response = self.client.get(
                reverse("search:playlist"),
                {"url": "https://videos.test/pl", "page": 2},
            )
        self.assertEqual(len(response.context["page_obj"]), 5)
        self.assertEqual(response.context["page_obj"].number, 2)

    def test_saved_playlists_paginate_with_spage(self):
        self._set_session(
            "playlists",
            [
                {"title": f"Lista {i}", "link": f"https://videos.test/pl/{i}"}
                for i in range(views.RESULTS_PER_PAGE + 1)
            ],
        )
        response = self.client.get(reverse("search:playlist"), {"spage": 2})
        self.assertEqual(len(response.context["saved_page"]), 1)
        self.assertEqual(response.context["saved_page_base"], "")
