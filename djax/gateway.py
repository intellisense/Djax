"""
Network ops for Djax.  Uses the Sharrock client.
"""
from django.conf import settings
from django.template.defaultfilters import slugify
import logging
from dateutil import parser

from pax.client import AxilentConnection
from pax.content import ContentClient
from pax.library import LibraryClient

log = logging.getLogger('djax')

# ============
# = Settings =
# ============
_endpoint = 'https://www.axilent.net'
if hasattr(settings,'AXILENT_ENDPOINT') and settings.AXILENT_ENDPOINT:
    _endpoint = settings.AXILENT_ENDPOINT

_api_version = 'beta3'
if hasattr(settings,'AXILENT_API_VERSION') and settings.AXILENT_API_VERSION:
    _api_version = settings.AXILENT_API_VERSION

if not hasattr(settings,'AXILENT_API_KEY') or not settings.AXILENT_API_KEY:
    raise ValueError('You must set the AXILENT_API_KEY in Django settings.')

_api_key = settings.AXILENT_API_KEY

_library_api_key = settings.AXILENT_LIBRARY_API_KEY if hasattr(settings,'AXILENT_LIBRARY_API_KEY') else None

# ===========
# = Clients =
# ===========
cx = AxilentConnection(_api_key,_api_version,_endpoint)
library_cx = AxilentConnection(_library_api_key,_api_version,_endpoint) if _library_api_key else None

content_client = ContentClient(cx)
library_client = LibraryClient(library_cx) if library_cx else None
library_project = settings.AXILENT_LIBRARY_PROJECT is hasattr(settings,'AXILENT_LIBRARY_PROJECT') else None