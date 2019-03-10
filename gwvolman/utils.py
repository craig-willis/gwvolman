# -*- coding: utf-8 -*-
# Copyright (c) 2016, Data Exploration Lab
# Distributed under the terms of the Modified BSD License.

"""A set of helper routines for WT related tasks."""

from collections import namedtuple
import os
import random
import re
import string
import uuid
import logging
import jwt
import hashlib
import math
import xml.etree.cElementTree as eTree
try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen

try:
    from urllib.request import Request
except ImportError:
    from urllib2 import Request

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse
import docker

from .constants import \
    DataONELocations, MOUNTPOINTS

DOCKER_URL = os.environ.get("DOCKER_URL", "unix://var/run/docker.sock")
HOSTDIR = os.environ.get("HOSTDIR", "/host")
MAX_FILE_SIZE = os.environ.get("MAX_FILE_SIZE", 200)
DOMAIN = os.environ.get('DOMAIN', 'dev.wholetale.org')
TRAEFIK_ENTRYPOINT = os.environ.get("TRAEFIK_ENTRYPOINT", "http")
REGISTRY_USER = os.environ.get('REGISTRY_USER', 'fido')
REGISTRY_PASS = os.environ.get('REGISTRY_PASS')
DATAONE_URL=os.environ.get('DATAONE_URL', 'https://cn-stage-2.test.dataone.org/cn')
MOUNTS = {}
RETRIES = 5
container_name_pattern = re.compile('tmp\.([^.]+)\.(.+)\Z')

PooledContainer = namedtuple('PooledContainer', ['id', 'path', 'host'])
ContainerConfig = namedtuple('ContainerConfig', [
    'image', 'command', 'mem_limit', 'cpu_shares',
    'container_port', 'container_user', 'target_mount',
    'url_path', 'environment'
])

SIZE_NOTATION_RE = re.compile("^(\d+)([kmg]?b?)$", re.IGNORECASE)
SIZE_TABLE = {
    '': 1, 'b': 1,
    'k': 1024, 'kb': 1024,
    'm': 1024 ** 2, 'mb': 1024 ** 2,
    'g': 1024 ** 3, 'gb': 1024 ** 3
}


def size_notation_to_bytes(size):
    if isinstance(size, int):
        return size
    match = SIZE_NOTATION_RE.match(size)
    if match:
        val, suffix = match.groups()
        return int(val) * SIZE_TABLE[suffix.lower()]
    raise ValueError


class Deployment(object):
    """Container for WT-specific docker stack deployment configuration.

    This class allows to read and store configuration of services in a WT
    deployment. It's meant to be used as a singleton across gwvolman.
    """

    _dashboard_url = None
    _girder_url = None
    _registry_url = None
    _traefik_network = None

    def __init__(self):
        self.docker_client = docker.from_env(version='1.28')

    @property
    def traefik_network(self):
        """str: Name of the overlay network used by traefik for ingress."""
        if self._traefik_network is None:
            service = self.docker_client.services.get('wt_dashboard')
            self._traefik_network = \
                service.attrs['Spec']['Labels']['traefik.docker.network']
        return self._traefik_network

    @property
    def dashboard_url(self):
        """str: Dashboard's public url."""
        if self._dashboard_url is None:
            self._dashboard_url = self.get_host_from_traefik_rule('wt_dashboard')
        return self._dashboard_url

    @property
    def girder_url(self):
        """str: Girder's public url."""
        if self._girder_url is None:
            self._girder_url = self.get_host_from_traefik_rule('wt_girder')
        return self._girder_url

    @property
    def registry_url(self):
        """str: Docker Registry's public url."""
        if self._registry_url is None:
            self._registry_url = self.get_host_from_traefik_rule('wt_registry')
        return self._registry_url

    def get_host_from_traefik_rule(self, service_name):
        """Infer service's hostname from traefik frontend rule label."""
        service = self.docker_client.services.get(service_name)
        rule = service.attrs['Spec']['Labels']['traefik.frontend.rule']
        return 'https://' + rule.split(':')[-1].split(',')[0].strip()


DEPLOYMENT = Deployment()


def sample_with_replacement(a, size):
    """Get a random path."""
    return "".join([random.SystemRandom().choice(a) for x in range(size)])


def new_user(size):
    """Get a random path."""
    return sample_with_replacement(string.ascii_letters + string.digits, size)


def _safe_mkdir(dest):
    try:
        os.mkdir(dest)
    except OSError as e:
        if e.errno != 17:
            raise
        logging.warn("Failed to mkdir {}".format(dest))
        pass


def _get_api_key(gc):
    api_key = None
    for key in gc.get('/api_key'):
        if key['name'] == 'tmpnb' and key['active']:
            api_key = key['key']

    if api_key is None:
        api_key = gc.post('/api_key',
                          data={'name': 'tmpnb', 'active': True})['key']
    return api_key


def _get_user_and_instance(girder_client, instanceId):
    user = girder_client.get('/user/me')
    if user is None:
        logging.warn("Bad gider token")
        raise ValueError
    instance = girder_client.get('/instance/' + instanceId)
    return user, instance


def get_env_with_csp(config):
    '''Ensure that environment in container config has CSP_HOSTS setting.

    This method handles 3 cases:
        * No 'environment' in config -> return ['CSP_HOSTS=...']
        * 'environment' in config, but no 'CSP_HOSTS=...' -> append
        * 'environment' in config and has 'CSP_HOSTS=...' -> replace

    '''
    csp = "CSP_HOSTS='self' {}".format(DEPLOYMENT.dashboard_url)
    try:
        env = config['environment']
        original_csp = next((_ for _ in env if _.startswith('CSP_HOSTS')), None)
        if original_csp:
            env[env.index(original_csp)] = csp  # replace
        else:
            env.append(csp)
    except KeyError:
        env = [csp]
    return env


def _get_container_config(gc, tale):
    if tale is None:
        container_config = {}  # settings['container_config']
    else:
        image = gc.get('/image/%s' % tale['imageId'])
        tale_config = image['config'] or {}
        if tale['config']:
            tale_config.update(tale['config'])

        try:
            mem_limit = size_notation_to_bytes(tale_config.get('memLimit', '2g'))
        except (ValueError, TypeError):
            mem_limit = 2 * 1024 ** 3
        container_config = ContainerConfig(
            command=tale_config.get('command'),
            container_port=tale_config.get('port'),
            container_user=tale_config.get('user'),
            cpu_shares=tale_config.get('cpuShares'),
            environment=get_env_with_csp(tale_config),
            image=urlparse(DEPLOYMENT.registry_url).netloc + '/' + tale['imageId'],
            mem_limit=mem_limit,
            target_mount=tale_config.get('targetMount'),
            url_path=tale_config.get('urlPath')
        )
    return container_config


def _launch_container(volumeName, nodeId, container_config):

    token = uuid.uuid4().hex
    # command
    if container_config.command:
        rendered_command = \
            container_config.command.format(
                base_path='', port=container_config.container_port,
                ip='0.0.0.0', token=token)
    else:
        rendered_command = None

    if container_config.url_path:
        rendered_url_path = \
            container_config.url_path.format(token=token)
    else:
        rendered_url_path = ''

    logging.info('config = ' + str(container_config))
    logging.info('command = ' + str(rendered_command))
    cli = docker.from_env(version='1.28')
    cli.login(username=REGISTRY_USER, password=REGISTRY_PASS,
              registry=DEPLOYMENT.registry_url)
    # Fails with: 'starting container failed: error setting
    #              label on mount source ...: read-only file system'
    # mounts = [
    #     docker.types.Mount(type='volume', source=volumeName, no_copy=True,
    #                        target=container_config.target_mount)
    # ]

    # FIXME: get mountPoint
    source_mount = '/var/lib/docker/volumes/{}/_data'.format(volumeName)
    mounts = []
    for path in MOUNTPOINTS:
        source = os.path.join(source_mount, path)
        target = os.path.join(container_config.target_mount, path)
        mounts.append(
            docker.types.Mount(type='bind', source=source, target=target)
        )
    host = 'tmp-{}'.format(new_user(12).lower())

    # https://github.com/containous/traefik/issues/2582#issuecomment-354107053
    endpoint_spec = docker.types.EndpointSpec(mode="vip")

    service = cli.services.create(
        container_config.image,
        command=rendered_command,
        labels={
            'traefik.port': str(container_config.container_port),
            'traefik.enable': 'true',
            'traefik.frontend.rule': 'Host:{}.{}'.format(host, DOMAIN),
            'traefik.docker.network': DEPLOYMENT.traefik_network,
            'traefik.frontend.passHostHeader': 'true',
            'traefik.frontend.entryPoints': TRAEFIK_ENTRYPOINT
        },
        env=container_config.environment,
        mode=docker.types.ServiceMode('replicated', replicas=1),
        networks=[DEPLOYMENT.traefik_network],
        name=host,
        mounts=mounts,
        endpoint_spec=endpoint_spec,
        constraints=['node.id == {}'.format(nodeId)],
        resources=docker.types.Resources(mem_limit=container_config.mem_limit)
    )

    # Wait for the server to launch within the container before adding it
    # to the pool or serving it to a user.
    # _wait_for_server(host_ip, host_port, path) # FIXME

    url = '{proto}://{host}.{domain}/{path}'.format(
        proto=TRAEFIK_ENTRYPOINT, host=host, domain=DOMAIN,
        path=rendered_url_path)

    return service, {'url': url}


def get_file_item(item_id, gc):
    """
    Gets the file out of an item.

    :param item_id: The item that has the file inside
    :param gc: The girder client
    :type: item_id: str
    :return: The file object or None
    :rtype: girder.models.file
    """
    file_generator = gc.listFile(item_id)
    try:
        return next(file_generator)
    except StopIteration as e:
        return None


def from_dataone(gc, item_id):
    """
    Checks if a url has dataone in it
    :param url: The url in question
    :return: True if it does, False otherwise
    """
    item = gc.getItem(item_id)
    folder = gc.getFolder(item['folderId'])
    try:
        return folder['meta']['provider'] == 'DataONE'
    except KeyError:
        return False


def check_pid(pid):
    """
    Check that a pid is of type str. Pids are generated as uuid4, and this
    check is done to make sure the programmer has converted it to a str before
    attempting to use it with the DataONE client.

    :param pid: The pid that is being checked
    :type pid: str, int
    :return: Returns the pid as a str, or just the pid if it was already a str
    :rtype: str
    """

    if not isinstance(pid, str):
        return str(pid)
    else:
        return pid


def get_remote_url(item_id, gc):
    """
    Checks if a file has a link url and returns the url if it does. This is less
     restrictive than thecget_dataone_url in that we aren't restricting the link
      to a particular domain.

    :param item_id: The id of the item
    :param gc: The girder client
    :return: The url that points to the object
    :rtype: str or None
    """

    file = get_file_item(item_id, gc)
    if file is None:
        file_error = 'Failed to find the file with ID {}'.format(item_id)
        logging.warning(file_error)
        raise ValueError(file_error)
    url = file.get('linkUrl')
    if url is not None:
        return url


def get_dataone_package_url(member_node, pid):
    """
    Given a repository url and a pid, construct a url that should
     be the package's landing page.

    :param member_node: The member node that the package is on
    :param pid: The package pid
    :return: The package landing page
    """
    if member_node in DataONELocations.prod_mn:
        return str('https://search.dataone.org/view/'+pid)
    elif member_node in DataONELocations.dev_mn:
        return str('https://dev.nceas.ucsb.edu/view/'+pid)


def extract_user_id(jwt_token):
    """
    Takes a JWT and extracts the 'userId` field. This is used
    as the package's owner and contact.
    :param jwt_token: The decoded JWT
    :type jwt_token: str
    :return: The ORCID ID
    :rtype: str, None if failure
    """
    jwt_token = jwt.decode(jwt_token, verify=False)
    user_id = jwt_token.get('userId')
    return user_id


def extract_user_name(jwt_token):
    """
    Takes a JWT and extracts the 'userId` field. This is used
    as the package's owner and contact.
    :param jwt_token: The decoded JWT
    :type jwt_token: str
    :return: The ORCID ID
    :rtype: str, None if failure
    """
    jwt_token = jwt.decode(jwt_token, verify=False)
    user_id = jwt_token.get('fullName')
    return user_id


def is_orcid_id(user_id):
    """
    Checks whether a string is a link to an ORCID account
    :param user_id: The string that may contain the ORCID account
    :type user_id: str
    :return: True/False if it is or isn't
    :rtype: bool
    """
    return bool(user_id.find('orcid.org'))


def esc(value):
    """
    Escape a string so it can be used in a Solr query string
    :param value: The string that will be escaped
    :type value: str
    :return: The escaped string
    :rtype: str
    """
    return urlparse.quote_plus(value)


def strip_html_tags(html_string):
    """
    Removes HTML tags from a string
    :param html_string: The string with HTML
    :type html_string: str
    :return: The string without HTML
    :rtype: str
    """
    return re.sub('<[^<]+?>', '', html_string)


def get_directory(user_id):
    """
    Returns the directory that should be used in the EML

    :param user_id: The user ID
    :type user_id: str
    :return: The directory name
    :rtype: str
    """
    if is_orcid_id(user_id):
        return "https://orcid.org"
    return "https://cilogon.org"


def make_url_https(url):
    """
    Given an http url, return it as https

    :param url: The http url
    :type url: str
    :return: The url as https
    :rtype: str
    """
    parsed = urlparse(url)
    return parsed._replace(scheme="https").geturl()


def make_url_http(url):
    """
    Given an https url, make it http
     :param url: The http url
    :type url: str
    :return: The url as https
    :rtype: str
    """
    parsed = urlparse(url)
    return parsed._replace(scheme="http").geturl()


def get_resource_map_user(user_id):
    """
    :param user_id: The user ORCID
    :type user_id: str
    :return: An http version of the user
    :rtype: str
    """
    if is_orcid_id(user_id):
        return make_url_http(user_id)
    return user_id


def get_file_md5(file_object, gc):
    """
    Computes the md5 of a file on the Girder filesystem.

    :param file_object: The file object that will be hashed
    :param gc: The girder client
    :type file_object: girder.models.file
    :return: Returns an updated md5 object. Returns None if it fails
    :rtype: md5
    """

    file = gc.downloadFileAsIterator(file_object['_id'])
    try:
        md5 = compute_md5(file)
    except Exception as e:
        logging.warning('Error: {}'.format(e))
        raise ValueError('Failed to download and md5 a remote file. {}'.format(e))
    return md5


def compute_md5(file):
    """
    Takes an file handle and computes the md5 of it. This uses duck typing
    to allow for any file handle that supports .read. Note that it is left to the
    caller to close the file handle and to handle any exceptions

    :param file: An open file handle that can be read
    :return: Returns an updated md5 object. Returns None if it fails
    :rtype: md5
    """
    md5 = hashlib.md5()
    while True:
        buf = file.read(8192)
        if not buf:
            break
        md5.update(buf)
    return md5


def get_item_identifier(item_id, gc):
    """
    Returns the identifier field in an item's meta field
    :param item_id: The item's ID
    :param gc: The Girder Client
    :type item_id: str
    :return: The item's identifier
    """
    item = gc.getItem(item_id)
    config = item.get('meta')
    if config:
        return config.get('identifier')


def filter_items(item_ids, gc):
    """
    Take a list of item ids and determine whether it:
       1. Exists on the local file system
       2. Exists on DataONE
       3. Is linked to a remote location other than DataONE
    :param item_ids: A list of items to be processed
    :param gc: The girder client
    :type item_ids: list
    :return: A dictionary of lists for each file location
    For example,
     {'dataone': ['uuid:123456', 'doi.10x501'],
     'remote_objects: ['url1', 'url2'],
     local: [file_obj1, file_obj2]}
    :rtype: dict
    """

    # Holds item_ids for DataONE objects
    dataone_objects = list()
    # Hold the DataONE pids
    dataone_pids = list()
    # Holds item_ids for files not in DataONE
    remote_objects = list()
    # Holds file dicts for local objects
    local_objects = list()
    # Holds item_ids for local files
    local_items = list()

    for item_id in item_ids:
        # Check if it points do a dataone objbect
        url = get_remote_url(item_id, gc)
        if url:
            if from_dataone(gc, item_id):
                dataone_objects.append(item_id)
                dataone_pids.append(get_item_identifier(item_id, gc))
                continue

            """
            If there is a url, and it's not pointing to a DataONE resource, then assume
            it's pointing to an external object
            """
            remote_objects.append(item_id)
            continue

        # If the file wasn't linked to a remote location, then it must exist locally. This
        # is a list of girder.models.File objects
        local_objects.append(get_file_item(item_id, gc))
        local_items.append(item_id)

    return {'dataone': dataone_objects,
            'dataone_pids': dataone_pids,
            'remote': remote_objects,
            'local_files': local_objects,
            'local_items': local_items}


def generate_dataone_guid():
    """
    DataONE requires that UUIDs are prepended with `urn:uuid:`. This method
    returns a DataONE compliant guid.
    :return: A DataONE compliant guid
    :rtype: str
    """
    return 'urn:uuid:'+str(uuid.uuid4())


def generate_size_progress_message(name, size_bytes):
    """
    Generates a message for the user about which file is being uploaded to a
    remote repository during publishing. For UX reasons, we convert Bytes
    to an appropriate derivative type.
    This was adapted from the following post at Stack Overflow
    https://stackoverflow.com/questions/5194057/better-way-to-convert-file-sizes-in-python

    :param name: Name of the file
    :param size_bytes: Size of the file in Bytes
    :return: The message that the user will see
    :rtype: str
    """

    size_name = ("Bytes", "KB", "MB", "GB", "TB", "PB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    progress_message = "Uploading {}  Size: {} {}".format(name,
                                                          s,
                                                          size_name[i])
    return progress_message


def retrieve_supported_mimetypes():
    """
    Returns a list of DataONE supported mimetypes. The endpoint returns
    XML, which is parsed with ElementTree.
    :return: A list of mimetypes
    :rtype: list
    """
    response = urlopen(DATAONE_URL+'/v2/formats')
    e = eTree.ElementTree(eTree.fromstring(response.read()))
    root = e.getroot()
    mime_types = set()

    for element in root.iter('mediaType'):
        mime_types.add(element.attrib['name'])

    return mime_types


def get_dataone_mimetype(supported_types, mimetype):
    """

    :param supported_types:
    :param mimetype:
    :return:
    """
    logging.info('Checking mimetype for: ')
    logging.info(mimetype)
    if mimetype not in supported_types:
        logging.info('Not Supported')
        return 'application/octet-stream'
    return mimetype