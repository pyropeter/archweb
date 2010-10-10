# -*- coding: utf-8 -*-
"""
mirrorresolv command

Usage: ./manage.py mirrorresolv
"""

from django.core.management.base import NoArgsCommand
from mirrors.models import MirrorUrl

import sys
import logging
from urlparse import urlparse
import socket

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s -> %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr)
logger = logging.getLogger()

class Command(NoArgsCommand):
    help = "Runs a check on all active mirror URLs to determine if they are reachable via IPv4 and/or v6."

    def handle_noargs(self, **options):
        v = int(options.get('verbosity', 0))
        if v == 0:
            logger.level = logging.ERROR
        elif v == 1:
            logger.level = logging.WARNING
        elif v == 2:
            logger.level = logging.DEBUG

        logger.debug("requesting list of mirror URLs")
        for mirrorurl in MirrorUrl.objects.filter(mirror__active=True):
            logger.info("resolving %3i (%s)"%(
                    mirrorurl.id, mirrorurl.url[:35]))
            families = [x[0] for x in socket.getaddrinfo(
                    urlparse(mirrorurl.url).hostname, None)]
            logger.debug("success")
            mirrorurl.has_ipv4 = socket.AF_INET in families
            mirrorurl.has_ipv6 = socket.AF_INET6 in families
            logger.debug("saving to database")
            mirrorurl.save()

# vim: set ts=4 sw=4 et:
