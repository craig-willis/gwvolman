#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import socket

API_VERSION = '2.1'
DEFAULT_USER = 1000
DEFAULT_GROUP = 100
ENABLE_WORKSPACES = True
MOUNTPOINTS = ['data', 'home']
if ENABLE_WORKSPACES:
    MOUNTPOINTS.append('workspace')

try:
    DEFAULT_GIRDER_API_URL = 'http://' + socket.gethostbyname('girder') + ':8080/api/v1'
except socket.gaierror:
    DEFAULT_GIRDER_API_URL = 'https://girder.dev.wholetale.org/api/v1'
GIRDER_API_URL = os.environ.get('GIRDER_API_URL', DEFAULT_GIRDER_API_URL)


class InstanceStatus(object):
    LAUNCHING = 0
    RUNNING = 1
    ERROR = 2


class DataONELocations:
    """
    An enumeration that describes the different DataONE
    endpoints.
    """
    # Production coordinating node
    prod_mn = 'https://knb.ecoinformatics.org/knb/d1/mn'
    # Development member node
    dev_mn = 'https://dev.nceas.ucsb.edu/knb/d1/mn'


class ExtraFileNames:
    """
    When creating data packages we'll have to create additional files, such as
     the zipped recipe, the tale.yml file, the metadata document, and possibly
      more. Keep their names store here so that they can easily be referenced and
      changed in a single place.
    """
    # Name for the tale config file
    tale_config = 'manifest.json'
    license_filename = 'LICENSE'
    environment_file = 'docker-environment.tar.gz'


"""
A dictionary for the descriptions of the manually added package files.
"""
file_descriptions = {
    ExtraFileNames.environment_file:
        'Holds the dockerfile and additional configurations for the '
        'underlying compute environment. This environment was used as the '
        'base image, and includes the the IDE that is used while running the Tale.',
    ExtraFileNames.tale_config:
        'A configuration file, holding information that is needed to '
        'reproduce the compute environment.',
    ExtraFileNames.license_filename:
        'The package\'s licensing information.'
}
