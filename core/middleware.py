"""Middleware de desarrollo: acepta cualquier Origin/Referer para CSRF.

Django valida la cabecera ``Origin`` contra el host que ve el servidor. Cuando
la app se sirve a través de un proxy inverso (o con otro hostname), los dos no
coinciden y aparece "Origin checking failed ... does not match any trusted
origins". ``CSRF_TRUSTED_ORIGINS`` no admite un comodín global.

Este middleware, **sólo con DEBUG=True**, reescribe ``Origin`` (y ``Referer``)
al host de la petición. Así la comprobación de origen pasa, pero se mantiene la
verificación del token CSRF. Con DEBUG=False no hace nada.
"""

from django.conf import settings


class DevAnyOriginCsrfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEBUG and request.method not in (
            "GET",
            "HEAD",
            "OPTIONS",
            "TRACE",
        ):
            try:
                host = request.get_host()
            except Exception:
                host = None
            if host:
                origin = "%s://%s" % (
                    "https" if request.is_secure() else "http",
                    host,
                )
                if "HTTP_ORIGIN" in request.META:
                    request.META["HTTP_ORIGIN"] = origin
                elif request.is_secure() and "HTTP_REFERER" in request.META:
                    request.META["HTTP_REFERER"] = origin + request.path
        return self.get_response(request)
