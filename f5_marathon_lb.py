#!/usr/bin/env python

"""Overview:
  The marathon-lb is a replacement for the haproxy-marathon-bridge.
  It reads the Marathon task information and dynamically generates
  haproxy configuration details.

  To gather the task information, the marathon-lb needs to know where
  to find Marathon. The service configuration details are stored in labels.

  Every service port in Marathon can be configured independently.


Features:
  - Virtual host aliases for services
  - Soft restart of haproxy
  - SSL Termination
  - (Optional): real-time update from Marathon events


Configuration:
  Service configuration lives in Marathon via labels.
  The marathon-lb just needs to know where to find marathon.
  To run in listening mode you must also specify the address + port at
  which the marathon-lb can be reached by marathon.


Usage:
  $ marathon-lb.py --marathon http://marathon1:8080 \
        --haproxy-config /etc/haproxy/haproxy.cfg

  The user that executes marathon-lb must have the permission to reload
  haproxy.


Operational Notes:
  - When a node in listening mode fails, remove the callback url for that
    node in marathon.
  - If run in listening mode, DNS isn't re-resolved. Restart the process
    periodically to force re-resolution if desired.
  - To avoid configuring itself as a backend when run via Marathon,
    services with appID matching FRAMEWORK_NAME env var will be skipped.
"""

from logging.handlers import SysLogHandler
from operator import attrgetter
from shutil import move
from tempfile import mkstemp
from textwrap import dedent
from wsgiref.simple_server import make_server
from sseclient import SSEClient
from six.moves.urllib import parse
from itertools import cycle
from common import *
from f5.bigip import BigIP
import icontrol

import argparse
import json
import logging
import os
import os.path
import stat
import re
import requests
import shlex
import subprocess
import sys
import socket
import time
import dateutil.parser
import math
import threading


class ConfigTemplater(object):
    HAPROXY_HEAD = dedent('''\
    global
      daemon
      log /dev/log local0
      log /dev/log local1 notice
      maxconn 50000
      tune.ssl.default-dh-param 2048
      ssl-default-bind-options no-sslv3 no-tls-tickets force-tlsv12
      ssl-default-bind-ciphers AES128+EECDH:AES128+EDH
      server-state-file global
      server-state-base /var/state/haproxy/
      lua-load /marathon-lb/getpids.lua
    defaults
      load-server-state-from-file global
      log               global
      retries                   3
      backlog               10000
      maxconn               10000
      timeout connect          3s
      timeout client          30s
      timeout server          30s
      timeout tunnel        3600s
      timeout http-keep-alive  1s
      timeout http-request    15s
      timeout queue           30s
      timeout tarpit          60s
      option            redispatch
      option            http-server-close
      option            dontlognull
    listen stats
      bind 0.0.0.0:9090
      balance
      mode http
      stats enable
      monitor-uri /_haproxy_health_check
      acl getpid path /_haproxy_getpids
      http-request use-service lua.getpids if getpid
    ''')

    HAPROXY_HTTP_FRONTEND_HEAD = dedent('''
    frontend marathon_http_in
      bind *:80
      mode http
    ''')

    HAPROXY_HTTP_FRONTEND_APPID_HEAD = dedent('''
    frontend marathon_http_appid_in
      bind *:9091
      mode http
    ''')

    # TODO(lloesche): make certificate path dynamic and allow multiple certs
    HAPROXY_HTTPS_FRONTEND_HEAD = dedent('''
    frontend marathon_https_in
      bind *:443 ssl {sslCerts}
      mode http
    ''')

    HAPROXY_FRONTEND_HEAD = dedent('''
    frontend {backend}
      bind {bindAddr}:{servicePort}{sslCert}{bindOptions}
      mode {mode}
    ''')

    HAPROXY_BACKEND_HEAD = dedent('''
    backend {backend}
      balance {balance}
      mode {mode}
    ''')

    HAPROXY_BACKEND_REDIRECT_HTTP_TO_HTTPS = '''\
  bind {bindAddr}:80
  redirect scheme https if !{{ ssl_fc }}
'''

    HAPROXY_HTTP_FRONTEND_ACL = '''\
  acl host_{cleanedUpHostname} hdr(host) -i {hostname}
  use_backend {backend} if host_{cleanedUpHostname}
'''

    HAPROXY_HTTP_FRONTEND_ACL_ONLY = '''\
  acl host_{cleanedUpHostname} hdr(host) -i {hostname}
'''

    HAPROXY_HTTP_FRONTEND_ROUTING_ONLY = '''\
  use_backend {backend} if host_{cleanedUpHostname}
'''

    HAPROXY_HTTP_FRONTEND_APPID_ACL = '''\
  acl app_{cleanedUpAppId} hdr(x-marathon-app-id) -i {appId}
  use_backend {backend} if app_{cleanedUpAppId}
'''

    HAPROXY_HTTPS_FRONTEND_ACL = '''\
  use_backend {backend} if {{ ssl_fc_sni {hostname} }}
'''

    HAPROXY_BACKEND_HTTP_OPTIONS = '''\
  option forwardfor
  http-request set-header X-Forwarded-Port %[dst_port]
  http-request add-header X-Forwarded-Proto https if { ssl_fc }
'''

    HAPROXY_BACKEND_HTTP_HEALTHCHECK_OPTIONS = '''\
  option  httpchk GET {healthCheckPath}
  timeout check {healthCheckTimeoutSeconds}s
'''

    HAPROXY_BACKEND_TCP_HEALTHCHECK_OPTIONS = ''

    HAPROXY_BACKEND_STICKY_OPTIONS = '''\
  cookie mesosphere_server_id insert indirect nocache
'''

    HAPROXY_BACKEND_SERVER_OPTIONS = '''\
  server {serverName} {host_ipv4}:{port}{cookieOptions}{healthCheckOptions}\
{otherOptions}
'''

    HAPROXY_BACKEND_SERVER_HTTP_HEALTHCHECK_OPTIONS = '''\
  check inter {healthCheckIntervalSeconds}s fall {healthCheckFalls}\
{healthCheckPortOptions}
'''
    HAPROXY_BACKEND_SERVER_TCP_HEALTHCHECK_OPTIONS = ''

    HAPROXY_FRONTEND_BACKEND_GLUE = '''\
  use_backend {backend}
'''

    def __init__(self, directory='templates'):
        self.__template_directory = directory
        self.__load_templates()

    def __load_templates(self):
        '''Loads template files if they exist, othwerwise it sets defaults'''
        variables = [
            'HAPROXY_HEAD',
            'HAPROXY_HTTP_FRONTEND_HEAD',
            'HAPROXY_HTTP_FRONTEND_APPID_HEAD',
            'HAPROXY_HTTPS_FRONTEND_HEAD',
            'HAPROXY_FRONTEND_HEAD',
            'HAPROXY_BACKEND_REDIRECT_HTTP_TO_HTTPS',
            'HAPROXY_BACKEND_HEAD',
            'HAPROXY_HTTP_FRONTEND_ACL',
            'HAPROXY_HTTP_FRONTEND_ACL_ONLY',
            'HAPROXY_HTTP_FRONTEND_ROUTING_ONLY',
            'HAPROXY_HTTP_FRONTEND_APPID_ACL',
            'HAPROXY_HTTPS_FRONTEND_ACL',
            'HAPROXY_BACKEND_HTTP_OPTIONS',
            'HAPROXY_BACKEND_HTTP_HEALTHCHECK_OPTIONS',
            'HAPROXY_BACKEND_TCP_HEALTHCHECK_OPTIONS',
            'HAPROXY_BACKEND_STICKY_OPTIONS',
            'HAPROXY_BACKEND_SERVER_OPTIONS',
            'HAPROXY_BACKEND_SERVER_HTTP_HEALTHCHECK_OPTIONS',
            'HAPROXY_BACKEND_SERVER_TCP_HEALTHCHECK_OPTIONS',
            'HAPROXY_FRONTEND_BACKEND_GLUE',
        ]

        for variable in variables:
            try:
                filename = os.path.join(self.__template_directory, variable)
                with open(filename) as f:
                    logger.info('overriding %s from %s', variable, filename)
                    setattr(self, variable, f.read())
            except IOError:
                logger.debug("setting default value for %s", variable)
                try:
                    setattr(self, variable, getattr(self.__class__, variable))
                except AttributeError:
                    logger.exception('default not found, aborting.')
                    raise

    @property
    def haproxy_head(self):
        return self.HAPROXY_HEAD

    @property
    def haproxy_http_frontend_head(self):
        return self.HAPROXY_HTTP_FRONTEND_HEAD

    @property
    def haproxy_http_frontend_appid_head(self):
        return self.HAPROXY_HTTP_FRONTEND_APPID_HEAD

    @property
    def haproxy_https_frontend_head(self):
        return self.HAPROXY_HTTPS_FRONTEND_HEAD

    def haproxy_frontend_head(self, app):
        if 'HAPROXY_{0}_FRONTEND_HEAD' in app.labels:
            return app.labels['HAPROXY_{0}_FRONTEND_HEAD']
        return self.HAPROXY_FRONTEND_HEAD

    def haproxy_backend_redirect_http_to_https(self, app):
        if 'HAPROXY_{0}_BACKEND_REDIRECT_HTTP_TO_HTTPS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_REDIRECT_HTTP_TO_HTTPS']
        return self.HAPROXY_BACKEND_REDIRECT_HTTP_TO_HTTPS

    def haproxy_backend_head(self, app):
        if 'HAPROXY_{0}_BACKEND_HEAD' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_HEAD']
        return self.HAPROXY_BACKEND_HEAD

    def haproxy_http_frontend_acl(self, app):
        if 'HAPROXY_{0}_HTTP_FRONTEND_ACL' in app.labels:
            return app.labels['HAPROXY_{0}_HTTP_FRONTEND_ACL']
        return self.HAPROXY_HTTP_FRONTEND_ACL

    def haproxy_http_frontend_acl_only(self, app):
        if 'HAPROXY_{0}_HTTP_FRONTEND_ACL_ONLY' in app.labels:
            return app.labels['HAPROXY_{0}_HTTP_FRONTEND_ACL_ONLY']
        return self.HAPROXY_HTTP_FRONTEND_ACL_ONLY

    def haproxy_http_frontend_routing_only(self, app):
        if 'HAPROXY_{0}_HTTP_FRONTEND_ROUTING_ONLY' in app.labels:
            return app.labels['HAPROXY_{0}_HTTP_FRONTEND_ROUTING_ONLY']
        return self.HAPROXY_HTTP_FRONTEND_ROUTING_ONLY

    def haproxy_http_frontend_appid_acl(self, app):
        if 'HAPROXY_{0}_HTTP_FRONTEND_APPID_ACL' in app.labels:
            return app.labels['HAPROXY_{0}_HTTP_FRONTEND_APPID_ACL']
        return self.HAPROXY_HTTP_FRONTEND_APPID_ACL

    def haproxy_https_frontend_acl(self, app):
        if 'HAPROXY_{0}_HTTPS_FRONTEND_ACL' in app.labels:
            return app.labels['HAPROXY_{0}_HTTPS_FRONTEND_ACL']
        return self.HAPROXY_HTTPS_FRONTEND_ACL

    def haproxy_backend_http_options(self, app):
        if 'HAPROXY_{0}_BACKEND_HTTP_OPTIONS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_HTTP_OPTIONS']
        return self.HAPROXY_BACKEND_HTTP_OPTIONS

    def haproxy_backend_http_healthcheck_options(self, app):
        if 'HAPROXY_{0}_BACKEND_HTTP_HEALTHCHECK_OPTIONS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_HTTP_HEALTHCHECK_OPTIONS']
        return self.HAPROXY_BACKEND_HTTP_HEALTHCHECK_OPTIONS

    def haproxy_backend_tcp_healthcheck_options(self, app):
        if 'HAPROXY_{0}_BACKEND_TCP_HEALTHCHECK_OPTIONS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_TCP_HEALTHCHECK_OPTIONS']
        return self.HAPROXY_BACKEND_TCP_HEALTHCHECK_OPTIONS

    def haproxy_backend_sticky_options(self, app):
        if 'HAPROXY_{0}_BACKEND_STICKY_OPTIONS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_STICKY_OPTIONS']
        return self.HAPROXY_BACKEND_STICKY_OPTIONS

    def haproxy_backend_server_options(self, app):
        if 'HAPROXY_{0}_BACKEND_SERVER_OPTIONS' in app.labels:
            return app.labels['HAPROXY_{0}_BACKEND_SERVER_OPTIONS']
        return self.HAPROXY_BACKEND_SERVER_OPTIONS

    def haproxy_backend_server_http_healthcheck_options(self, app):
        if 'HAPROXY_{0}_BACKEND_SERVER_HTTP_HEALTHCHECK_OPTIONS' in \
                app.labels:
            return self.__blank_prefix_or_empty(
                app.labels['HAPROXY_{0}_BACKEND' +
                           '_SERVER_HTTP_HEALTHCHECK_OPTIONS']
                .strip())
        return self.__blank_prefix_or_empty(
            self.HAPROXY_BACKEND_SERVER_HTTP_HEALTHCHECK_OPTIONS.strip())

    def haproxy_backend_server_tcp_healthcheck_options(self, app):
        if 'HAPROXY_{0}_BACKEND_SERVER_TCP_HEALTHCHECK_OPTIONS' in app.labels:
            return self.__blank_prefix_or_empty(
                app.labels['HAPROXY_{0}_BACKEND_'
                           'SERVER_TCP_HEALTHCHECK_OPTIONS']
                .strip())
        return self.__blank_prefix_or_empty(
            self.HAPROXY_BACKEND_SERVER_TCP_HEALTHCHECK_OPTIONS.strip())

    def haproxy_frontend_backend_glue(self, app):
        if 'HAPROXY_{0}_FRONTEND_BACKEND_GLUE' in app.labels:
            return app.labels['HAPROXY_{0}_FRONTEND_BACKEND_GLUE']
        return self.HAPROXY_FRONTEND_BACKEND_GLUE

    def __blank_prefix_or_empty(self, s):
        if s:
            return ' ' + s
        else:
            return s


def string_to_bool(s):
    return s.lower() in ["true", "t", "yes", "y"]


def set_hostname(x, k, v):
    x.hostname = v


def set_sticky(x, k, v):
    x.sticky = string_to_bool(v)


def set_redirect_http_to_https(x, k, v):
    x.redirectHttpToHttps = string_to_bool(v)


def set_sslCert(x, k, v):
    x.sslCert = v


def set_bindOptions(x, k, v):
    x.bindOptions = v


def set_bindAddr(x, k, v):
    x.bindAddr = v

def set_port(x, k, v):
    x.servicePort = int(v)


def set_mode(x, k, v):
    x.mode = v


def set_balance(x, k, v):
    x.balance = v


def set_label(x, k, v):
    x.labels[k] = v


label_keys = {
    'HAPROXY_{0}_VHOST': set_hostname,
    'HAPROXY_{0}_STICKY': set_sticky,
    'HAPROXY_{0}_REDIRECT_TO_HTTPS': set_redirect_http_to_https,
    'HAPROXY_{0}_SSL_CERT': set_sslCert,
    'HAPROXY_{0}_BIND_OPTIONS': set_bindOptions,
    'HAPROXY_{0}_BIND_ADDR': set_bindAddr,
    'HAPROXY_{0}_PORT': set_port,
    'HAPROXY_{0}_MODE': set_mode,
    'HAPROXY_{0}_BALANCE': set_balance,
    'HAPROXY_{0}_FRONTEND_HEAD': set_label,
    'HAPROXY_{0}_BACKEND_REDIRECT_HTTP_TO_HTTPS': set_label,
    'HAPROXY_{0}_BACKEND_HEAD': set_label,
    'HAPROXY_{0}_HTTP_FRONTEND_ACL': set_label,
    'HAPROXY_{0}_HTTPS_FRONTEND_ACL': set_label,
    'HAPROXY_{0}_HTTP_FRONTEND_APPID_ACL': set_label,
    'HAPROXY_{0}_BACKEND_HTTP_OPTIONS': set_label,
    'HAPROXY_{0}_BACKEND_TCP_HEALTHCHECK_OPTIONS': set_label,
    'HAPROXY_{0}_BACKEND_HTTP_HEALTHCHECK_OPTIONS': set_label,
    'HAPROXY_{0}_BACKEND_STICKY_OPTIONS': set_label,
    'HAPROXY_{0}_FRONTEND_BACKEND_GLUE': set_label,
    'HAPROXY_{0}_BACKEND_SERVER_TCP_HEALTHCHECK_OPTIONS': set_label,
    'HAPROXY_{0}_BACKEND_SERVER_HTTP_HEALTHCHECK_OPTIONS': set_label,
    'HAPROXY_{0}_BACKEND_SERVER_OPTIONS': set_label,
}
print(label_keys)


#logging.basicConfig(
#        level=logging.DEBUG,
#        format="%(levelname) -8s %(asctime)s m:%(module)s f:%(funcName)s l:%(lineno)d: %(message)s"
#        )
logger = logging.getLogger('marathon_lb')


class MarathonBackend(object):

    def __init__(self, host, port, draining):
        self.host = host
        self.port = port
        self.draining = draining

    def __hash__(self):
        return hash((self.host, self.port))

    def __repr__(self):
        return "MarathonBackend(%r, %r)" % (self.host, self.port)


class MarathonService(object):

    def __init__(self, appId, servicePort, healthCheck):
        self.appId = appId
        self.servicePort = servicePort
        self.backends = set()
        self.hostname = None
        self.sticky = False
        self.redirectHttpToHttps = False
        self.sslCert = None
        self.bindOptions = None
        self.bindAddr = '*'
        self.groups = frozenset()
        self.mode = 'tcp'
        self.balance = 'roundrobin'
        self.healthCheck = healthCheck
        self.labels = {}
        if healthCheck:
            if healthCheck['protocol'] == 'HTTP':
                self.mode = 'http'

    def add_backend(self, host, port, draining):
        self.backends.add(MarathonBackend(host, port, draining))

    def __hash__(self):
        return hash(self.servicePort)

    def __eq__(self, other):
        return self.servicePort == other.servicePort

    def __repr__(self):
        return "MarathonService(%r, %r)" % (self.appId, self.servicePort)


class MarathonApp(object):

    def __init__(self, marathon, appId, app):
        self.app = app
        self.groups = frozenset()
        self.appId = appId

        # port -> MarathonService
        self.services = dict()

    def __hash__(self):
        return hash(self.appId)

    def __eq__(self, other):
        return self.appId == other.appId


class Marathon(object):

    def __init__(self, hosts, health_check, auth):
        # TODO(cmaloney): Support getting master list from zookeeper
        self.__hosts = hosts
        self.__health_check = health_check
        self.__auth = auth
        self.__cycle_hosts = cycle(self.__hosts)

    def api_req_raw(self, method, path, auth, body=None, **kwargs):
        for host in self.__hosts:
            path_str = os.path.join(host, 'v2')

            for path_elem in path:
                path_str = path_str + "/" + path_elem
            response = requests.request(
                method,
                path_str,
                auth=auth,
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                **kwargs
            )

            logger.debug("%s %s", method, response.url)
            if response.status_code == 200:
                break
        if 'message' in response.json():
            response.reason = "%s (%s)" % (
                response.reason,
                response.json()['message'])
        response.raise_for_status()
        return response

    def api_req(self, method, path, **kwargs):
        return self.api_req_raw(method, path, self.__auth, **kwargs).json()

    def create(self, app_json):
        return self.api_req('POST', ['apps'], app_json)

    def get_app(self, appid):
        logger.info('fetching app %s', appid)
        return self.api_req('GET', ['apps', appid])["app"]

    # Lists all running apps.
    def list(self):
        logger.info('fetching apps')
        return self.api_req('GET', ['apps'],
                            params={'embed': 'apps.tasks'})["apps"]

    def health_check(self):
        return self.__health_check

    def tasks(self):
        logger.info('fetching tasks')
        return self.api_req('GET', ['tasks'])["tasks"]

    def add_subscriber(self, callbackUrl):
        return self.api_req(
                'POST',
                ['eventSubscriptions'],
                params={'callbackUrl': callbackUrl})

    def remove_subscriber(self, callbackUrl):
        return self.api_req(
                'DELETE',
                ['eventSubscriptions'],
                params={'callbackUrl': callbackUrl})

    def get_event_stream(self):
        url = self.host+"/v2/events"
        logger.info(
            "SSE Active, trying fetch events from from {0}".format(url))
        return SSEClient(url, auth=self.__auth)

    @property
    def host(self):
        return next(self.__cycle_hosts)


def has_group(groups, app_groups):
    # All groups / wildcard match
    if '*' in groups:
        return True

    # empty group only
    if len(groups) == 0 and len(app_groups) == 0:
        raise Exception("No groups specified")

    # Contains matching groups
    if (len(frozenset(app_groups) & groups)):
        return True

    return False

ip_cache = dict()


def resolve_ip(host):
    cached_ip = ip_cache.get(host, None)
    if cached_ip:
        return cached_ip
    else:
        try:
            logger.debug("trying to resolve ip address for host %s", host)
            ip = socket.gethostbyname(host)
            ip_cache[host] = ip
            return ip
        except socket.gaierror:
            return None


def config(apps, groups, bind_http_https, ssl_certs, templater):
    logger.info("generating config")
    f5 = {}
    config = templater.haproxy_head
    groups = frozenset(groups)
    _ssl_certs = ssl_certs or "/etc/ssl/mesosphere.com.pem"
    _ssl_certs = _ssl_certs.split(",")

    if bind_http_https:
        http_frontends = templater.haproxy_http_frontend_head
        https_frontends = templater.haproxy_https_frontend_head.format(
            sslCerts=" ".join(map(lambda cert: "crt " + cert, _ssl_certs))
        )

    frontends = str()
    backends = str()
    http_appid_frontends = templater.haproxy_http_frontend_appid_head
    apps_with_http_appid_backend = []

    for app in sorted(apps, key=attrgetter('appId', 'servicePort')):
        f5_service = {
                'virtual': {},
                'nodes': {},
                'health': {}
                }
        # App only applies if we have it's group
        if not has_group(groups, app.groups):
            print("doesn't have group")
            continue

        logger.debug("configuring app %s", app.appId)
        backend = app.appId[1:].replace('/', '_') + '_' + str(app.servicePort)

        frontend_name = "%s_%s_%d" % ((app.appId).lstrip('/'), app.bindAddr, app.servicePort)
        logger.debug("frontend at %s:%d with backend %s",
                     app.bindAddr, app.servicePort, backend)

        # if the app has a hostname set force mode to http
        # otherwise recent versions of haproxy refuse to start
        if app.hostname:
            app.mode = 'http'

        frontend_head = templater.haproxy_frontend_head(app)
        frontends += frontend_head.format(
            bindAddr=app.bindAddr,
            backend=backend,
            servicePort=app.servicePort,
            mode=app.mode,
            sslCert=' ssl crt ' + app.sslCert if app.sslCert else '',
            bindOptions=' ' + app.bindOptions if app.bindOptions else ''
        )

        f5_service['virtual'].update({
            'id': (app.appId).lstrip('/'),
            'name': frontend_name,
            'destination': app.bindAddr,
            'port': app.servicePort,
            'protocol': app.mode
            })

        if app.redirectHttpToHttps:
            logger.debug("rule to redirect http to https traffic")
            haproxy_backend_redirect_http_to_https = \
                templater.haproxy_backend_redirect_http_to_https(app)
            frontends += haproxy_backend_redirect_http_to_https.format(
                bindAddr=app.bindAddr)

        backend_head = templater.haproxy_backend_head(app)
        backends += backend_head.format(
            backend=backend,
            balance=app.balance,
            mode=app.mode
        )

        # if a hostname is set we add the app to the vhost section
        # of our haproxy config
        # TODO(lloesche): Check if the hostname is already defined by another
        # service
        if bind_http_https and app.hostname:
            p_fe, s_fe = generateHttpVhostAcl(templater, app, backend)
            http_frontends += p_fe
            https_frontends += s_fe

        # if app mode is http, we add the app to the second http frontend
        # selecting apps by http header X-Marathon-App-Id
        if app.mode == 'http' and \
                app.appId not in apps_with_http_appid_backend:
            logger.debug("adding virtual host for app with id %s", app.appId)
            # remember appids to prevent multiple entries for the same app
            apps_with_http_appid_backend += [app.appId]
            cleanedUpAppId = re.sub(r'[^a-zA-Z0-9\-]', '_', app.appId)

            http_appid_frontend_acl = templater \
                .haproxy_http_frontend_appid_acl(app)
            http_appid_frontends += http_appid_frontend_acl.format(
                cleanedUpAppId=cleanedUpAppId,
                hostname=app.hostname,
                appId=app.appId,
                backend=backend
            )

        if app.mode == 'http':
            backends += templater.haproxy_backend_http_options(app)

        if app.healthCheck:
            print "______ HEALTHCHECK VIRT _________"
            print app.healthCheck
            f5_service['health'] = app.healthCheck
            f5_service['health']['name'] = "%s_%s" % (frontend_name, app.healthCheck['protocol'])
            print "______ /HEALTHCHECK VIRT _________"

            health_check_options = None
            if app.mode == 'tcp':
                health_check_options = templater \
                    .haproxy_backend_tcp_healthcheck_options(app)
            elif app.mode == 'http':
                health_check_options = templater \
                    .haproxy_backend_http_healthcheck_options(app)
            if health_check_options:
                healthCheckPort = app.healthCheck.get('port')
                backends += health_check_options.format(
                    healthCheck=app.healthCheck,
                    healthCheckPortIndex=app.healthCheck.get('portIndex'),
                    healthCheckPort=healthCheckPort,
                    healthCheckProtocol=app.healthCheck['protocol'],
                    healthCheckPath=app.healthCheck.get('path', '/'),
                    healthCheckTimeoutSeconds=app.healthCheck[
                        'timeoutSeconds'],
                    healthCheckIntervalSeconds=app.healthCheck[
                        'intervalSeconds'],
                    healthCheckIgnoreHttp1xx=app.healthCheck['ignoreHttp1xx'],
                    healthCheckGracePeriodSeconds=app.healthCheck[
                        'gracePeriodSeconds'],
                    healthCheckMaxConsecutiveFailures=app.healthCheck[
                        'maxConsecutiveFailures'],
                    healthCheckFalls=app.healthCheck[
                        'maxConsecutiveFailures'] + 1,
                    healthCheckPortOptions=' port ' +
                    str(healthCheckPort) if healthCheckPort else ''
                )

        if app.sticky:
            logger.debug("turning on sticky sessions")
            backends += templater.haproxy_backend_sticky_options(app)

        frontend_backend_glue = templater.haproxy_frontend_backend_glue(app)
        frontends += frontend_backend_glue.format(backend=backend)

        key_func = attrgetter('host', 'port')
        for backendServer in sorted(app.backends, key=key_func):
            logger.debug(
                "backend server at %s:%d",
                backendServer.host,
                backendServer.port)
            serverName = re.sub(
                r'[^a-zA-Z0-9\-]', '_',
                backendServer.host + '_' + str(backendServer.port))

            f5_node_name = backendServer.host + ':' + str(backendServer.port)
            f5_service['nodes'].update({f5_node_name: {
                'name_old': serverName,
                'name': backendServer.host + ':' + str(backendServer.port),
                'host': backendServer.host,
                'port': backendServer.port
                }})

            healthCheckOptions = None
            if app.healthCheck:
                print "______ HEALTHCHECK REAL _________"
                print app.healthCheck
                #f5_service['health'] = app.healthCheck
                print "______ /HEALTHCHECK REAL _________"
                server_health_check_options = None
                if app.mode == 'tcp':
                    server_health_check_options = templater \
                        .haproxy_backend_server_tcp_healthcheck_options(app)
                elif app.mode == 'http':
                    server_health_check_options = templater \
                        .haproxy_backend_server_http_healthcheck_options(app)
                if server_health_check_options:
                    healthCheckPort = app.healthCheck.get('port')
                    healthCheckOptions = server_health_check_options.format(
                        healthCheck=app.healthCheck,
                        healthCheckPortIndex=app.healthCheck.get('portIndex'),
                        healthCheckPort=healthCheckPort,
                        healthCheckProtocol=app.healthCheck['protocol'],
                        healthCheckPath=app.healthCheck.get('path', '/'),
                        healthCheckTimeoutSeconds=app.healthCheck[
                            'timeoutSeconds'],
                        healthCheckIntervalSeconds=app.healthCheck[
                            'intervalSeconds'],
                        healthCheckIgnoreHttp1xx=app.healthCheck[
                            'ignoreHttp1xx'],
                        healthCheckGracePeriodSeconds=app.healthCheck[
                            'gracePeriodSeconds'],
                        healthCheckMaxConsecutiveFailures=app.healthCheck[
                            'maxConsecutiveFailures'],
                        healthCheckFalls=app.healthCheck[
                            'maxConsecutiveFailures'] + 1,
                        healthCheckPortOptions=' port ' +
                        str(healthCheckPort) if healthCheckPort else ''
                    )
            ipv4 = resolve_ip(backendServer.host)

            if ipv4 is not None:
                backend_server_options = templater \
                    .haproxy_backend_server_options(app)
                backends += backend_server_options.format(
                    host=backendServer.host,
                    host_ipv4=ipv4,
                    port=backendServer.port,
                    serverName=serverName,
                    cookieOptions=' check cookie ' +
                    serverName if app.sticky else '',
                    healthCheckOptions=healthCheckOptions
                    if healthCheckOptions else '',
                    otherOptions=' disabled' if backendServer.draining else ''
                )
            else:
                logger.warning("Could not resolve ip for host %s, "
                               "ignoring this backend",
                               backendServer.host)

        f5.update({frontend_name: f5_service})

    if bind_http_https:
        config += http_frontends
    config += http_appid_frontends
    if bind_http_https:
        config += https_frontends
    config += frontends
    config += backends

    print(json.dumps(f5))

    #return config
    print config
    return f5


def generateHttpVhostAcl(templater, app, backend):
    # If the hostname contains the delimiter ',', then the marathon app is
    # requesting multiple hostname matches for the same backend, and we need
    # to use alternate templates from the default one-acl/one-use_backend.
    staging_http_frontends = ""
    staging_https_frontends = ""

    if "," in app.hostname:
        logger.debug(
            "vhost label specifies multiple hosts: %s", app.hostname)
        vhosts = app.hostname.split(',')
        acl_name = re.sub(r'[^a-zA-Z0-9\-]', '_', vhosts[0])

        for vhost_hostname in vhosts:
            logger.debug("processing vhost %s", vhost_hostname)
            http_frontend_acl = templater.haproxy_http_frontend_acl_only(app)
            staging_http_frontends += http_frontend_acl.format(
                cleanedUpHostname=acl_name,
                hostname=vhost_hostname
            )

            # Tack on the SSL ACL as well
            https_frontend_acl = templater.haproxy_https_frontend_acl(app)
            staging_https_frontends += https_frontend_acl.format(
                cleanedUpHostname=acl_name,
                hostname=vhost_hostname,
                appId=app.appId,
                backend=backend
            )

        # We've added the http acl lines, now route them to the same backend
        http_frontend_route = templater.haproxy_http_frontend_routing_only(app)
        staging_http_frontends += http_frontend_route.format(
            cleanedUpHostname=acl_name,
            backend=backend
        )

    else:
        # A single hostname in the VHOST label
        logger.debug(
            "adding virtual host for app with hostname %s", app.hostname)
        acl_name = re.sub(r'[^a-zA-Z0-9\-]', '_', app.hostname)

        http_frontend_acl = templater.haproxy_http_frontend_acl(app)
        staging_http_frontends += http_frontend_acl.format(
            cleanedUpHostname=acl_name,
            hostname=app.hostname,
            appId=app.appId,
            backend=backend
        )

        https_frontend_acl = templater.haproxy_https_frontend_acl(app)
        staging_https_frontends += https_frontend_acl.format(
            cleanedUpHostname=acl_name,
            hostname=app.hostname,
            appId=app.appId,
            backend=backend
        )

    return (staging_http_frontends, staging_https_frontends)


def writeConfigAndValidate(config, config_file):
    # Test run, print to stdout and exit
    if args.dry:
        print(config)
        sys.exit()
    # Write config to a temporary location
    fd, haproxyTempConfigFile = mkstemp()
    logger.debug("writing config to temp file %s", haproxyTempConfigFile)
    with os.fdopen(fd, 'w') as haproxyTempConfig:
        haproxyTempConfig.write(config)

    # Ensure new config is created with the same
    # permissions the old file had or use defaults
    # if config file doesn't exist yet
    perms = 0o644
    if os.path.isfile(config_file):
        perms = stat.S_IMODE(os.lstat(config_file).st_mode)
    os.chmod(haproxyTempConfigFile, perms)

    # Check that config is valid
    cmd = ['haproxy', '-f', haproxyTempConfigFile, '-c']
    logger.debug("checking config with command: " + str(cmd))
    returncode = subprocess.call(args=cmd)
    if returncode == 0:
        # Move into place
        logger.debug("moving temp file %s to %s",
                     haproxyTempConfigFile,
                     config_file)
        move(haproxyTempConfigFile, config_file)
        return True
    else:
        logger.error("haproxy returned non-zero when checking config")
        return False

def f5_go(config, config_file):
    logger.debug(config)
    
    # get f5 config
    f5_config = str()
    try:
        logger.debug("reading config from %s", config_file)
        with open(config_file, "r") as f:
            f5_config = f.read()
    except IOError:
        logger.warning("couldn't open config file for reading")

    try:
        f5_config = json.loads(f5_config)
    except:
        logger.error("config file not json")

    # set partition if none defined
    if 'partition' in f5_config:
        partition = f5_config['partition']
    else:
        partition = 'mesos'

    # get f5 connection
    try:
        bigip = BigIP(
                f5_config['host'], 
                f5_config['username'],
                f5_config['password']
                )
    except:
        logger.error('exception')

    logger.debug(bigip)

    marathon_virtual_list = [x for x in config.keys() if '*' not in x]
    marathon_pool_list = [x for x in config.keys() if '*' not in x]

    # this is kinda kludgey, but just iterate over virt name and append protocol
    # to get "marathon_healthcheck_list"
    marathon_healthcheck_list = []
    for v in marathon_virtual_list:
        if 'health' in config[v]:
            n = "%s_%s" % (v, config[v]['health']['protocol'])
            marathon_healthcheck_list.append(n)

    f5_pool_list = get_pool_list(bigip, partition)
    f5_virtual_list = get_virtual_list(bigip, partition)
    f5_healthcheck_list = get_healthcheck_list(bigip, partition)

    logger.debug("f5_pool_list = %s" % (','.join(f5_pool_list)))
    logger.debug("f5_virtual_list = %s" % (','.join(f5_virtual_list)))
    logger.debug("f5_healthcheck_list = %s" % (','.join(f5_healthcheck_list)))
    logger.debug("marathon_pool_list = %s" % (','.join(marathon_pool_list)))
    logger.debug("marathon_virtual_list = %s" % (','.join(marathon_virtual_list)))

    # virtual delete
    virt_delete = list(set(f5_virtual_list) - set(marathon_virtual_list))
    logger.debug("virts to delete = %s" % (','.join(virt_delete)))
    for virt in virt_delete:
        virtual_delete(bigip, partition, virt)

    # pool delete
    pool_delete_list = list(set(f5_pool_list) - set(marathon_pool_list))
    logger.debug("pools to delete = %s" % (','.join(pool_delete_list)))
    for pool in pool_delete_list:
        print "++++++++++++"
        print pool
        print "++++++++++++"
        pool_delete(bigip, partition, pool)
    
    # healthcheck delete
    health_delete = list(set(f5_healthcheck_list) - set(marathon_virtual_list))
    logger.debug("healthchecks to delete = %s" % (','.join(health_delete)))
    for hc in health_delete:
        healthcheck_delete(bigip, partition, hc, config[hc]['health']['protocol'])

    # pool add
    pool_add = list(set(marathon_pool_list) - set(f5_pool_list))
    logger.debug("pools to add = %s" % (','.join(pool_add)))
    for pool in pool_add:
        pool_create(bigip, partition, pool, config[pool])

    
    # virtual add
    virt_add = list(set(marathon_virtual_list) - set(f5_virtual_list))
    logger.debug("virts to add = %s" % (','.join(virt_add)))
    for virt in virt_add:
        virtual_create(bigip, partition, virt, config[virt])

    # healthcheck config needs to happen before pool config because the pool
    # is where we add the healthcheck
    # healthcheck add
    # use the name of the virt for the healthcheck
    healthcheck_add = list(set(marathon_virtual_list) - set(f5_healthcheck_list))
    logger.debug("healthchecks to add = %s" % (','.join(healthcheck_add)))
    for hc in healthcheck_add:
        healthcheck_create(bigip, partition, hc, config[hc]['health'])
    

    # healthcheck intersection
    healthcheck_intersect = list(set.intersection(set(marathon_virtual_list), set(f5_healthcheck_list)))
    logger.debug("healthchecks to update = %s" % (','.join(healthcheck_intersect)))
    for hc in healthcheck_intersect:
        healthcheck_update(bigip, partition, hc, config[hc]['health'])

    # pool intersection
    pool_intersect = list(set.intersection(set(marathon_pool_list), set(f5_pool_list)))
    logger.debug("pools to update = %s" % (','.join(pool_intersect)))
    for pool in pool_intersect:
        pool_update(bigip, partition, pool, config[pool])
    
    # virt intersection
    virt_intersect = list(set.intersection(set(marathon_virtual_list), set(f5_virtual_list)))
    logger.debug("virts to update = %s" % (','.join(virt_intersect)))
    for virt in virt_intersect:
        virtual_update(bigip, partition, virt, config[virt])


    # add/update/remove pool members
    # need to iterate over pool_add and pool_intersect
    # (note that remove a pool also removes members, so don't have to worry
    # about those)
    for pool in list(set(pool_add + pool_intersect)):
        #print pool

        f5_member_list = get_pool_member_list(bigip, partition, pool)
        marathon_member_list = (config[pool]['nodes']).keys()

        member_delete_list = list(set(f5_member_list) - set(marathon_member_list))
        logger.debug("members to delete = %s" % (','.join(member_delete_list)))
        for member in member_delete_list:
            member_delete(bigip, partition, pool, member)

        member_add = list(set(marathon_member_list) - set(f5_member_list))
        logger.debug("members to add = %s" % (','.join(member_add)))
        for member in member_add:
            member_create(bigip, partition, pool, member, config[pool]['nodes'][member])

        # since we're only specifying hostname and port for members, 'member_update' will never
        # actually get called.  changing either of these properties will result in a new
        # member being created and the old one being deleted.
        # i'm leaving this here though in case we add other properties to members
        member_update_list = list(set.intersection(set(marathon_member_list), set(f5_member_list)))
        logger.debug("members to update = %s" % (','.join(member_update_list)))
        for member in member_update_list:
            member_update(bigip, partition, pool, member, config[pool]['nodes'][member])
        

def get_pool_member_list(bigip, partition, pool):
    member_list = []
    p = get_pool(bigip, partition, pool)
    members = p.members_s.get_collection()
    for member in members:
        member_list.append(member.name)
    
    return member_list


def get_pool_list(bigip, partition):
    pool_list = []
    pools = bigip.ltm.pools.get_collection()
    print pools
    for pool in pools:
        print pool.__dict__
        if pool.partition == partition:
            print "pool is in mesos partition"
            pool_list.append(pool.name)
    print "-----------"
    print pool_list
    print "-----------"
    return pool_list

def get_virtual_list(bigip, partition):
    virtual_list = []
    virtuals = bigip.ltm.virtuals.get_collection()
    for virtual in virtuals:
        if virtual.partition == partition:
            virtual_list.append(virtual.name)

    return virtual_list

def get_healthcheck_list(bigip, partition):
    # will need to handle HTTP and TCP

    healthcheck_list = []

    # HTTP
    healthchecks = bigip.ltm.monitor.https.get_collection()
    for hc in healthchecks:
        if hc.partition == partition:
            healthcheck_list.append(hc.name)
   
    # TCP
    healthchecks = bigip.ltm.monitor.tcps.get_collection()
    for hc in healthchecks:
        if hc.partition == partition:
            healthcheck_list.append(hc.name)

    return healthcheck_list

def pool_create(bigip, partition, pool, data):
    # TODO: do we even need 'data' here?
    print("creating pool %s" % pool)
    p = bigip.ltm.pools.pool
    p.create(
        name=pool,
        partition=partition
        )

def pool_delete(bigip, partition, pool):
    print("deleting pool %s" % pool)
    p = get_pool(bigip, partition, pool)
    p.delete()

def pool_update(bigip, partition, pool, data):
    # getting 'data' here, but not used currently
    # in fact, this update function does nothing currently.
    # if we end up supporting more pool-specific options (not really sure what)
    # then we will need this.  data should be changed or massaged to be
    # a list of k,v pairs for the update call

    #loadBalancingMode options: 
    # var: F5_{n}_BALANCE
    #   round-robin, 
    #   least-connections-member,
    #   ratio-member
    #   observed-member
    #   ratio-node
    #   ...

    print data
    virtual = data['virtual']
    print virtual
    pool = get_pool(bigip, partition, pool)
    if 'health' in data:
        pool.monitor = virtual['name']
    pool.update(
            state=None
            )

class Healthcheck(object):

    def __init__(self, data):
        self.path = None
        self.timeout = None
        self._name = data['name']
        self.protocol = data['protocol']
        self.maxConsecutiveFailures = data['maxConsecutiveFailures']
        self.intervalSeconds = data['intervalSeconds']
        self.timeoutSeconds = data['timeoutSeconds']

    @property
    def name(self):
        return "%s_%s" % (self._name, self.protocol)

    @property
    def get_timeout(self):
        timeout = ((self.maxConsecutiveFailures - 1) * self.intervalSeconds) + self.timeoutSeconds + 1
        return timeout


def healthcheck_delete(bigip, partition, hc, type):
    print("deleting healthcheck %s" % hc)
    hc = get_healthcheck(bigip, partition, hc, type)
    hc.delete()

def healthcheck_timeout_calculate(data):
    # calculate timeout
    # see the f5 monitor docs for explanation of settings: https://goo.gl/JJWUIg
    # formula to match up marathon settings with f5 settings
    # ( ( maxConsecutiveFailures - 1) * intervalSeconds ) + timeoutSeconds + 1
    timeout = ((data['maxConsecutiveFailures'] - 1) * data['intervalSeconds']) + data['timeoutSeconds'] + 1
    return timeout

def healthcheck_update(bigip, partition, hc, data):

    # get healthcheck object
    hc = get_healthcheck(bigip, partition, hc, data['protocol'])
    
    timeout = healthcheck_timeout_calculate(data)
    
    # f5 docs: https://goo.gl/ALrf37
    send_string = 'GET /'
    if 'path' in data:
        # i expected to have to jump through some hoops to get the "\r\n" literal
        # into the f5 config, but this seems to work.
        # when configuring the f5 directly, you have to include the "\r\n"
        # literal at the end of the GET.  from my testing, this is getting
        # added automatically.  I'm not sure what layer is adding it (iControl
        # itself?).  anyway, this works for now, but i could see this being
        # fragile
        send_string = 'GET %s' % data['path']

    if data['protocol'] == "HTTP":
        hc.update(
                interval=data['intervalSeconds'],
                timeout=timeout,
                sendString=send_string,
                )

    if data['protocol'] == "TCP":
        hc.update(
                interval=data['intervalSeconds'],
                timeout=timeout,
                )

def healthcheck_create(bigip, partition, hc, data):

    timeout = healthcheck_timeout_calculate(data)

    # NOTE: there is no concept of a grace period in F5, so this setting 
    # (gracePeriodSeconds) will be ignored

    # f5 docs: https://goo.gl/ALrf37
    send_string = 'GET /'
    if 'path' in data:
        # i expected to have to jump through some hoops to get the "\r\n" literal
        # into the f5 config, but this seems to work.
        # when configuring the f5 directly, you have to include the "\r\n"
        # literal at the end of the GET.  from my testing, this is getting
        # added automatically.  I'm not sure what layer is adding it (iControl
        # itself?).  anyway, this works for now, but i could see this being
        # fragile
        send_string = 'GET %s' % data['path']

    if data['protocol'] == "HTTP":
        h = bigip.ltm.monitor.https
        http1 = h.http
        print http1
        http1.create(
                name=hc,
                partition=partition,
                interval=data['intervalSeconds'],
                timeout=timeout,
                sendString=send_string,
                )

    if data['protocol'] == "TCP":
        h = bigip.ltm.monitor.tcps
        tcp1 = h.tcp
        print tcp1
        tcp1.create(
                name=hc,
                partition=partition,
                interval=data['intervalSeconds'],
                timeout=timeout,
                )


def member_create(bigip, partition, pool, member, data):
    # getting 'data' here, but not used currently
    p = get_pool(bigip, partition, pool)
    member = p.members_s.member.create(
            name=member,
            partition=partition
            )

def member_delete(bigip, partition, pool, member):
    member = get_member(bigip, partition, pool, member)
    member.delete()


def member_update(bigip, partition, pool, member, data):
    # getting 'data' here, but not used currently
    # in fact, this update function does nothing currently.
    # if we end up supporting more member-specific options, like ratio
    # then we will need this.  data should be changed or massaged to be
    # a list of k,v pairs for the update call ("ratio": 2)
    member = get_member(bigip, partition, pool, member)
    #member.update(
    #        state=None
    #        )

def get_virtual(bigip, partition, virtual):
    # return virtual object
    v = bigip.ltm.virtuals.virtual.load(
            name=virtual,
            partition=partition
            )
    return v

def get_pool(bigip, partition, pool):
    # return pool object
    p = bigip.ltm.pools.pool.load(
            name=pool,
            partition=partition
            )
    return p

def get_healthcheck(bigip, partition, hc, type):
    # return hc object
    if type == 'HTTP':
        hc = bigip.ltm.monitor.https.http.load(
                name=hc,
                partition=partition
                )
    elif type == 'TCP':
        hc = bigip.ltm.monitor.tcps.tcp.load(
                name=hc,
                partition=partition
                )

    return hc

def get_member(bigip, partition, pool, member):
    p = get_pool(bigip, partition, pool)
    m = p.members_s.member.load(
            name=member,
            partition=partition
            )
    return m

def virtual_create(bigip, partition, virtual, data):
    print("creating virt %s" % virtual)
    data = data['virtual']
    v = bigip.ltm.virtuals.virtual
    destination = "/%s/%s:%d" % (
            partition, 
            data['destination'], 
            data['port']
            )
    pool = "/%s/%s" % (partition, virtual)
    v.create(
            name=virtual,
            partition=partition,
            ipProtocol=get_protocol(data['protocol']),
            port=data['port'],
            destination=destination,
            pool=pool,
            sourceAddressTranslation={'type': 'automap'}
            )

def virtual_delete(bigip, partition, virtual):
    print("deleting virtual %s" % virtual)
    v = get_virtual(bigip, partition, virtual)
    v.delete()

def virtual_update(bigip, partition, virtual, data):
    data = data['virtual']
    destination = "/%s/%s:%d" % (
            partition, 
            data['destination'], 
            data['port']
            )
    pool = "/%s/%s" % (partition, virtual)
    v = get_virtual(bigip, partition, virtual)
    v.update(
            name=virtual,
            partition=partition,
            ipProtocol=get_protocol(data['protocol']),
            port=data['port'],
            destination=destination,
            pool=pool,
            sourceAddressTranslation={'type': 'automap'}
            )


def get_protocol(protocol):
    if str(protocol).lower() == 'tcp':
        return 'tcp'
    if str(protocol).lower() == 'http':
        return 'tcp'
    if str(protocol).lower() == 'udp':
        return 'udp'
    else:
        return 'tcp'



def get_health_check(app, portIndex):
    for check in app['healthChecks']:
        if check.get('port'):
            return check
        if check.get('portIndex') == portIndex:
            return check
    return None


def get_apps(marathon):
    apps = marathon.list()
    logger.debug("got apps %s", [app["id"] for app in apps])

    marathon_apps = []
    # This process requires 2 passes: the first is to gather apps belonging
    # to a deployment group.
    processed_apps = []
    deployment_groups = {}
    for app in apps:
        deployment_group = None
        if 'HAPROXY_DEPLOYMENT_GROUP' in app['labels']:
            deployment_group = app['labels']['HAPROXY_DEPLOYMENT_GROUP']
            # mutate the app id to match deployment group
            if deployment_group[0] != '/':
                deployment_group = '/' + deployment_group
            app['id'] = deployment_group
        else:
            processed_apps.append(app)
            continue
        if deployment_group in deployment_groups:
            # merge the groups, with the oldest taking precedence
            prev = deployment_groups[deployment_group]
            cur = app

            # TODO(brenden): do something more intelligent when the label is
            # missing.
            if 'HAPROXY_DEPLOYMENT_STARTED_AT' in prev['labels']:
                prev_date = dateutil.parser.parse(
                    prev['labels']['HAPROXY_DEPLOYMENT_STARTED_AT'])
            else:
                prev_date = ''
            if 'HAPROXY_DEPLOYMENT_STARTED_AT' in cur['labels']:
                cur_date = dateutil.parser.parse(
                    cur['labels']['HAPROXY_DEPLOYMENT_STARTED_AT'])
            else:
                cur_date = ''

            old = new = None
            if prev_date < cur_date:
                old = prev
                new = cur
            else:
                new = prev
                old = cur

            target_instances = \
                int(new['labels']['HAPROXY_DEPLOYMENT_TARGET_INSTANCES'])

            # mark N tasks from old app as draining, where N is the
            # number of instances in the new app
            old_tasks = sorted(old['tasks'],
                               key=lambda task: task['host'] +
                               ":" + str(task['ports']))

            healthy_new_instances = 0
            if len(app['healthChecks']) > 0:
                for task in new['tasks']:
                    if 'healthCheckResults' not in task:
                        continue
                    alive = True
                    for result in task['healthCheckResults']:
                        if not result['alive']:
                            alive = False
                    if alive:
                        healthy_new_instances += 1
            else:
                healthy_new_instances = new['instances']

            maximum_drainable = \
                max(0, (healthy_new_instances + old['instances']) -
                    target_instances)

            for i in range(0, min(len(old_tasks),
                                  healthy_new_instances,
                                  maximum_drainable)):
                old_tasks[i]['draining'] = True

            # merge tasks from new app into old app
            merged = old
            old_tasks.extend(new['tasks'])
            merged['tasks'] = old_tasks

            deployment_groups[deployment_group] = merged
        else:
            deployment_groups[deployment_group] = app

    processed_apps.extend(deployment_groups.values())

    for app in processed_apps:
        print(">>>> in processed_apps")
        print(">>>> appid = %s" % app['id'])
        appId = app['id']
        if appId[1:] == os.environ.get("FRAMEWORK_NAME"):
            continue

        marathon_app = MarathonApp(marathon, appId, app)

        if 'HAPROXY_GROUP' in marathon_app.app['labels']:
            marathon_app.groups = \
                marathon_app.app['labels']['HAPROXY_GROUP'].split(',')
        marathon_apps.append(marathon_app)

        service_ports = app['ports']
        print(service_ports)
        for i in range(len(service_ports)):
            servicePort = service_ports[i]
            service = MarathonService(
                        appId, servicePort, get_health_check(app, i))

            for key_unformatted in label_keys:
                key = key_unformatted.format(i)
                print(">>> key = %s" % key)
                print(marathon_app.app['labels'])
                if key in marathon_app.app['labels']:
                    print("xxxxx here")
                    func = label_keys[key_unformatted]
                    func(service,
                         key_unformatted,
                         marathon_app.app['labels'][key])

            marathon_app.services[servicePort] = service

        for task in app['tasks']:
            # Marathon 0.7.6 bug workaround
            if len(task['host']) == 0:
                logger.warning("Ignoring Marathon task without host " +
                               task['id'])
                continue

            if marathon.health_check() and 'healthChecks' in app and \
               len(app['healthChecks']) > 0:
                if 'healthCheckResults' not in task:
                    continue
                alive = True
                for result in task['healthCheckResults']:
                    if not result['alive']:
                        alive = False
                if not alive:
                    continue

            task_ports = task['ports']
            draining = False
            if 'draining' in task:
                draining = task['draining']

            # if different versions of app have different number of ports,
            # try to match as many ports as possible
            number_of_defined_ports = min(len(task_ports), len(service_ports))

            for i in range(number_of_defined_ports):
                task_port = task_ports[i]
                service_port = service_ports[i]
                service = marathon_app.services.get(service_port, None)
                if service:
                    service.groups = marathon_app.groups
                    service.add_backend(task['host'],
                                        task_port,
                                        draining)

    # Convert into a list for easier consumption
    apps_list = []
    for marathon_app in marathon_apps:
        for service in list(marathon_app.services.values()):
            if service.backends:
                apps_list.append(service)

    print("apps list...")
    print(apps_list)

    return apps_list



def regenerate_config_f5(apps, config_file, groups, bind_http_https,
                      ssl_certs, templater):
    logger.info("in regenerate_config_f5()")
    print(apps)
    for app in apps:
        print(app.__hash__())
    f5_go(config(apps, groups, bind_http_https,
                                ssl_certs, templater), config_file)

class MarathonEventProcessor(object):

    def __init__(self, marathon, config_file, groups,
                 bind_http_https, ssl_certs):
        self.__marathon = marathon
        # appId -> MarathonApp
        self.__apps = dict()
        self.__config_file = config_file
        self.__groups = groups
        self.__templater = ConfigTemplater()
        self.__bind_http_https = bind_http_https
        self.__ssl_certs = ssl_certs

        self.__condition = threading.Condition()
        self.__thread = threading.Thread(target=self.do_reset)
        self.__pending_reset = False
        self.__thread.start()

        # Fetch the base data
        self.reset_from_tasks()

    def do_reset(self):
        with self.__condition:
            while True:
                self.__condition.acquire()
                if not self.__pending_reset:
                    self.__condition.wait()
                self.__pending_reset = False
                self.__condition.release()

                try:
                    start_time = time.time()

                    self.__apps = get_apps(self.__marathon)
                    regenerate_config_f5(self.__apps,
                                      self.__config_file,
                                      self.__groups,
                                      self.__bind_http_https,
                                      self.__ssl_certs,
                                      self.__templater)

                    logger.debug("updating tasks finished, took %s seconds",
                                 time.time() - start_time)
                except requests.exceptions.ConnectionError as e:
                    logger.error("Connection error({0}): {1}".format(
                        e.errno, e.strerror))
                except:
                    print("Unexpected error:", sys.exc_info()[0])

    def reset_from_tasks(self):
        self.__condition.acquire()
        self.__pending_reset = True
        self.__condition.notify()
        self.__condition.release()

    def handle_event(self, event):
        if event['eventType'] == 'status_update_event' or \
                event['eventType'] == 'health_status_changed_event' or \
                event['eventType'] == 'api_post_event':
            self.reset_from_tasks()


def get_arg_parser():
    parser = argparse.ArgumentParser(
        description="Marathon HAProxy Load Balancer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--longhelp",
                        help="Print out configuration details",
                        action="store_true"
                        )
    parser.add_argument("--marathon", "-m",
                        nargs="+",
                        help="[required] Marathon endpoint, eg. -m " +
                             "http://marathon1:8080 -m http://marathon2:8080"
                        )
    parser.add_argument("--listening", "-l",
                        help="The address this script listens on for " +
                        "marathon events"
                        )
    parser.add_argument("--callback-url", "-u",
                        help="The HTTP address that Marathon can call this " +
                             "script back at (http://lb1:8080)"
                        )
    parser.add_argument("--haproxy-config",
                        help="Location of haproxy configuration",
                        default="/etc/haproxy/haproxy.cfg"
                        )
    parser.add_argument("--group",
                        help="[required] Only generate config for apps which"
                        " list the specified names. Use '*' to match all"
                        " groups",
                        action="append",
                        default=list())
    parser.add_argument("--command", "-c",
                        help="If set, run this command to reload haproxy.",
                        default=None)
    parser.add_argument("--sse", "-s",
                        help="Use Server Sent Events instead of HTTP "
                        "Callbacks",
                        action="store_true")
    parser.add_argument("--health-check", "-H",
                        help="If set, respect Marathon's health check "
                        "statuses before adding the app instance into "
                        "the backend pool.",
                        action="store_true")
    parser.add_argument("--dont-bind-http-https",
                        help="Don't bind to HTTP and HTTPS frontends.",
                        action="store_true")
    parser.add_argument("--ssl-certs",
                        help="List of SSL certificates separated by comma"
                             "for frontend marathon_https_in"
                             "Ex: /etc/ssl/site1.co.pem,/etc/ssl/site2.co.pem",
                        default="/etc/ssl/mesosphere.com.pem")
    parser.add_argument("--dry", "-d",
                        help="Only print configuration to console",
                        action="store_true")
    parser = set_logging_args(parser)
    parser = set_marathon_auth_args(parser)
    return parser


def run_server(marathon, listen_addr, callback_url, config_file, groups,
               bind_http_https, ssl_certs):
    processor = MarathonEventProcessor(marathon,
                                       config_file,
                                       groups,
                                       bind_http_https,
                                       ssl_certs)
    marathon.add_subscriber(callback_url)

    # TODO(cmaloney): Switch to a sane http server
    # TODO(cmaloney): Good exception catching, etc
    def wsgi_app(env, start_response):
        length = int(env['CONTENT_LENGTH'])
        data = env['wsgi.input'].read(length)
        processor.handle_event(json.loads(data.decode('utf-8')))
        # TODO(cmaloney): Make this have a simple useful webui for debugging /
        # monitoring
        start_response('200 OK', [('Content-Type', 'text/html')])

        return ["Got it\n".encode('utf-8')]

    listen_uri = parse.urlparse(listen_addr)
    httpd = make_server(listen_uri.hostname, listen_uri.port, wsgi_app)
    httpd.serve_forever()


def clear_callbacks(marathon, callback_url):
    logger.info("Cleanup, removing subscription to {0}".format(callback_url))
    marathon.remove_subscriber(callback_url)


def process_sse_events(marathon, config_file, groups,
                       bind_http_https, ssl_certs):
    processor = MarathonEventProcessor(marathon,
                                       config_file,
                                       groups,
                                       bind_http_https,
                                       ssl_certs)
    events = marathon.get_event_stream()
    for event in events:
        try:
            # logger.info("received event: {0}".format(event))
            # marathon might also send empty messages as keepalive...
            if (event.data.strip() != ''):
                # marathon sometimes sends more than one json per event
                # e.g. {}\r\n{}\r\n\r\n
                for real_event_data in re.split(r'\r\n', event.data):
                    data = json.loads(real_event_data)
                    logger.info(
                        "received event of type {0}".format(data['eventType']))
                    if data['eventType'] == 'event_stream_detached':
                        # Need to force reload and re-attach to stream
                        processor.reset_from_tasks()
                        return
                    processor.handle_event(data)
            else:
                logger.info("skipping empty message")
        except:
            print(event.data)
            print("Unexpected error:", sys.exc_info()[0])
            raise


if __name__ == '__main__':
    # Process arguments
    arg_parser = get_arg_parser()
    args = arg_parser.parse_args()

    # Print the long help text if flag is set
    if args.longhelp:
        print(__doc__)
        sys.exit()
    # otherwise make sure that a Marathon URL was specified
    else:
        if args.marathon is None:
            arg_parser.error('argument --marathon/-m is required')
        if args.sse and args.listening:
            arg_parser.error(
                'cannot use --listening and --sse at the same time')
        if len(args.group) == 0:
            arg_parser.error('argument --group is required: please' +
                             'specify at least one group name')

    # Set request retries
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount('http://', a)

    # Setup logging
    setup_logging(logger, args.syslog_socket, args.log_format)

    # Marathon API connector
    marathon = Marathon(args.marathon,
                        args.health_check,
                        get_marathon_auth_params(args))

    # If in listening mode, spawn a webserver waiting for events. Otherwise
    # just write the config.
    if args.listening:
        callback_url = args.callback_url or args.listening
        try:
            run_server(marathon, args.listening, callback_url,
                       args.haproxy_config, args.group,
                       not args.dont_bind_http_https, args.ssl_certs)
        finally:
            clear_callbacks(marathon, callback_url)
    elif args.sse:
        while True:
            try:
                process_sse_events(marathon,
                                   args.haproxy_config,
                                   args.group,
                                   not args.dont_bind_http_https,
                                   args.ssl_certs)
            except:
                logger.exception("Caught exception")
                logger.error("Reconnecting...")
            time.sleep(1)
    else:
        # Generate base config
        regenerate_config_f5(get_apps(marathon), args.haproxy_config, args.group,
                          not args.dont_bind_http_https,
                          args.ssl_certs, ConfigTemplater())
