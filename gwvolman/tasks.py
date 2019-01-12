"""A set of WT related Girder tasks."""
import os
import shutil
import socket
import json
import time
import tempfile
import textwrap
import docker
import subprocess
from docker.errors import DockerException
import girder_client

import logging
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse
from girder_worker.utils import girder_job
from girder_worker.app import app
# from girder_worker.plugins.docker.executor import _pull_image
from .utils import \
    HOSTDIR, REGISTRY_USER, REGISTRY_PASS, \
    new_user, _safe_mkdir, _get_api_key, \
    _get_container_config, _launch_container, _get_user_and_instance, \
    DEPLOYMENT
from .publish import publish_tale
from .constants import GIRDER_API_URL, InstanceStatus, ENABLE_WORKSPACES, \
    DEFAULT_USER, DEFAULT_GROUP, MOUNTPOINTS


@girder_job(title='Create Tale Data Volume')
@app.task(bind=True)
def create_volume(self, instanceId: str):
    """Create a mountpoint and compose WT-fs."""
    user, instance = _get_user_and_instance(self.girder_client, instanceId)
    tale = self.girder_client.get('/tale/{taleId}'.format(**instance))

    vol_name = "%s_%s_%s" % (tale['_id'], user['login'], new_user(6))
    cli = docker.from_env(version='1.28')

    try:
        volume = cli.volumes.create(name=vol_name, driver='local')
    except DockerException as dex:
        logging.error('Error pulling Docker image blah')
        logging.exception(dex)
    logging.info("Volume: %s created", volume.name)
    mountpoint = volume.attrs['Mountpoint']
    logging.info("Mountpoint: %s", mountpoint)

    try:
        self.girder_client.downloadFolderRecursive(
            tale['narrativeId'], HOSTDIR + mountpoint)
    except KeyError:
        pass  # no narrativeId
    except girder_client.HttpError:
        logging.warn("Narrative folder not found for tale: %s",
                     str(tale['_id']))
        pass

    os.chown(HOSTDIR + mountpoint, DEFAULT_USER, DEFAULT_GROUP)
    for root, dirs, files in os.walk(HOSTDIR + mountpoint):
        for obj in dirs + files:
            os.chown(os.path.join(root, obj), DEFAULT_USER, DEFAULT_GROUP)

    # Before calling girderfs and "escaping" container, we need to make
    # sure that shared objects we use are available on the host
    # TODO: this assumes overlayfs
    mounts = ''.join(open(HOSTDIR + '/proc/1/mounts').readlines())
    if 'overlay /usr/local' not in mounts:
        cont = cli.containers.get(socket.gethostname())
        libdir = cont.attrs['GraphDriver']['Data']['MergedDir']
        subprocess.call('mount --bind {}/usr/local /usr/local'.format(libdir),
                        shell=True)

    homeDir = self.girder_client.loadOrCreateFolder(
        'Home', user['_id'], 'user')
    data_dir = os.path.join(mountpoint, 'data')
    _safe_mkdir(HOSTDIR + data_dir)
    home_dir = os.path.join(mountpoint, 'home')
    _safe_mkdir(HOSTDIR + home_dir)
    if ENABLE_WORKSPACES:
        work_dir = os.path.join(mountpoint, 'workspace')
        _safe_mkdir(HOSTDIR + work_dir)
        if not os.path.isdir(work_dir):
            os.makedirs(work_dir)

    # FUSE is silly and needs to have mirror inside container
    for directory in (data_dir, home_dir):
        if not os.path.isdir(directory):
            os.makedirs(directory)
    api_key = _get_api_key(self.girder_client)

    if tale.get('dataSet') is not None:
        session = self.girder_client.post(
            '/dm/session', parameters={'taleId': tale['_id']})
    elif tale.get('folderId'):  # old format, keep it for now
        data_set = [
            {'itemId': folder['_id'], 'mountPath': '/' + folder['name']}
            for folder in self.girder_client.listFolder(tale['folderId'])
        ]
        data_set += [
            {'itemId': item['_id'], 'mountPath': '/' + item['name']}
            for item in self.girder_client.listItem(tale['folderId'])
        ]
        session = self.girder_client.post(
            '/dm/session', parameters={'dataSet': json.dumps(data_set)})
    else:
        session = {'_id': None}

    if session['_id'] is not None:
        cmd = "girderfs --hostns -c wt_dms --api-url {} --api-key {} {} {}"
        cmd = cmd.format(GIRDER_API_URL, api_key, data_dir, session['_id'])
        logging.info("Calling: %s", cmd)
        subprocess.call(cmd, shell=True)
    #  webdav relies on mount.c module, don't use hostns for now
    cmd = 'girderfs -c wt_home --api-url '
    cmd += '{} --api-key {} {} {}'.format(
        GIRDER_API_URL, api_key, home_dir, homeDir['_id'])
    logging.info("Calling: %s", cmd)
    subprocess.call(cmd, shell=True)

    if ENABLE_WORKSPACES:
        cmd = 'girderfs -c wt_work --api-url '
        cmd += '{} --api-key {} {} {}'.format(
            GIRDER_API_URL, api_key, work_dir, tale['_id'])
        logging.info("Calling: %s", cmd)
        subprocess.call(cmd, shell=True)

    return dict(
        nodeId=cli.info()['Swarm']['NodeID'],
        mountPoint=mountpoint,
        volumeName=volume.name,
        sessionId=session['_id'],
        instanceId=instanceId,
    )


@girder_job(title='Spawn Instance')
@app.task(bind=True)
def launch_container(self, payload):
    """Launch a container using a Tale object."""
    user, instance = _get_user_and_instance(
        self.girder_client, payload['instanceId'])
    tale = self.girder_client.get('/tale/{taleId}'.format(**instance))

    # _pull_image() #FIXME
    container_config = _get_container_config(self.girder_client, tale)
    service, attrs = _launch_container(
        payload['volumeName'], payload['nodeId'],
        container_config=container_config)

    tic = time.time()
    timeout = 30.0

    # wait until task is started
    while time.time() - tic < timeout:
        try:
            started = service.tasks()[0]['Status']['State'] == 'running'
        except IndexError:
            started = False
        if started:
            break
        time.sleep(0.2)

    payload.update(attrs)
    payload['name'] = service.name
    return payload


@girder_job(title='Update Instance')
@app.task(bind=True)
def update_container(self, instanceId, **kwargs):
    user, instance = _get_user_and_instance(self.girder_client, instanceId)

    cli = docker.from_env(version='1.28')
    if 'containerInfo' not in instance:
        return
    containerInfo = instance['containerInfo']  # VALIDATE
    try:
        service = cli.services.get(containerInfo['name'])
    except docker.errors.NotFound:
        logging.info("Service not present [%s].", containerInfo['name'])
        return

    # Assume containers launched from gwvolman come from its configured registry
    repoLoc = urlparse(DEPLOYMENT.registry_url).netloc
    digest = repoLoc + '/' + kwargs['image']

    try:
        # NOTE: Only "image" passed currently, but this can be easily extended
        logging.info("Restarting container [%s].", service.name)
        service.update(image=digest)
        logging.info("Restart command has been sent to Container [%s].", service.name)
    except Exception as e:
        logging.error("Unable to send restart command to container [%s]: %s", service.id, e)

    return {'image_digest': digest}


@girder_job(title='Shutdown Instance')
@app.task(bind=True)
def shutdown_container(self, instanceId):
    """Shutdown a running Tale."""
    user, instance = _get_user_and_instance(self.girder_client, instanceId)

    cli = docker.from_env(version='1.28')
    if 'containerInfo' not in instance:
        return
    containerInfo = instance['containerInfo']  # VALIDATE
    try:
        service = cli.services.get(containerInfo['name'])
    except docker.errors.NotFound:
        logging.info("Service not present [%s].",
                     containerInfo['name'])
        return

    try:
        logging.info("Releasing container [%s].", service.name)
        service.remove()
        logging.info("Container [%s] has been released.", service.name)
    except Exception as e:
        logging.error("Unable to release container [%s]: %s", service.id, e)


@girder_job(title='Remove Tale Data Volume')
@app.task(bind=True)
def remove_volume(self, instanceId):
    """Unmount WT-fs and remove mountpoint."""
    user, instance = _get_user_and_instance(self.girder_client, instanceId)

    if 'containerInfo' not in instance:
        return
    containerInfo = instance['containerInfo']  # VALIDATE

    cli = docker.from_env(version='1.28')
    for suffix in MOUNTPOINTS:
        dest = os.path.join(containerInfo['mountPoint'], suffix)
        logging.info("Unmounting %s", dest)
        subprocess.call("umount %s" % dest, shell=True)

    try:
        self.girder_client.delete('/dm/session/{sessionId}'.format(**instance))
    except Exception as e:
        logging.error("Unable to remove session. %s", e)
        pass

    try:
        volume = cli.volumes.get(containerInfo['volumeName'])
    except docker.errors.NotFound:
        logging.info("Volume not present [%s].", containerInfo['volumeName'])
        return
    try:
        logging.info("Removing volume: %s", volume.id)
        volume.remove()
    except Exception as e:
        logging.error("Unable to remove volume [%s]: %s", volume.id, e)
        pass


@girder_job(title='Build Tale Image')
@app.task
def build_tale_image(tale_id, girder_token):
    """Build docker image from Tale object and push to a registry."""

    logging.info('Building image for Tale %s', tale_id)

    # Download the workspace folder to the temp directory
    temp_dir = tempfile.mkdtemp(dir='/host/tmp')
    try:
        logging.info('Downloading workspace folder to %s (%s)', temp_dir, tale_id)
        gc = girder_client.GirderClient(apiUrl=GIRDER_API_URL)
        gc.token = str(girder_token)
        tale = gc.get('/tale/{}'.format(tale_id))
        gc.downloadFolderRecursive(tale['folderId'], temp_dir)
    except Exception as e:
        raise ValueError('Error authenticating with Girder {}'.format(e))
    except KeyError:
        logger.info('KeyError')
        pass  # no workspace folderId
    except girder_client.HttpError:
        logging.warn("Workspace folder not found for tale: %s",
                     str(tale['_id']))
        pass


    apicli = docker.APIClient(base_url='unix://var/run/docker.sock')
    apicli.login(username=REGISTRY_USER, password=REGISTRY_PASS,
                 registry=DEPLOYMENT.registry_url)

    cli = docker.from_env(version='1.28')
    cli.login(username=REGISTRY_USER, password=REGISTRY_PASS,
              registry=DEPLOYMENT.registry_url)

    tag = urlparse(DEPLOYMENT.registry_url).netloc + '/' + tale_id

    # Run repo2docker on the workspace using a shared tmp directory
    logging.info('Building image (%s)', tale_id)
    r2d_cmd='jupyter-repo2docker --image-name {} --no-run --user-id=1000 --user-name=jovyan {}'.format(tag, temp_dir)
    container = cli.containers.run(
        image='jupyter/repo2docker:0.7.0', 
        command=r2d_cmd, 
        environment=['DOCKER_HOST=unix:///var/run/docker.sock'],
        privileged=True,
        detach=True,
        remove=True,
        volumes= { 
            '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'},
            '/tmp': {'bind': '/host/tmp', 'mode': 'ro'}
        }
    )

    for line in container.logs(stream=True):
        logging.info(line.decode('utf-8'))
    
    # Push the image
    for line in apicli.push(tag, stream=True):
        print(line)

    #shutil.rmtree(temp_dir, ignore_errors=True)

    # Get the image attributes
    image = cli.images.get(tag)
    digest = next((_ for _ in image.attrs['RepoDigests']
                   if _.startswith(urlparse(DEPLOYMENT.registry_url).netloc)), None)
    return digest


@girder_job(title='Publish Tale')
@app.task
def publish(item_ids,
            tale,
            dataone_node,
            dataone_auth_token,
            girder_token,
            userId,
            prov_info,
            license_id):
    """
    Publish a Tale to DataONE.

    :param item_ids: A list of item ids that are in the package
    :param tale: The tale id
    :param dataone_node: The DataONE member node endpoint
    :param dataone_auth_token: The user's DataONE JWT
    :param girder_token: The user's girder token
    :param userId: The user's ID
    :param prov_info: Additional information included in the tale yaml
    :param license_id: The spdx of the license used
    :type item_ids: list
    :type tale: str
    :type dataone_node: str
    :type dataone_auth_token: str
    :type girder_token: str
    :type userId: str
    :type prov_info: dict
    :type license_id: str
    """
    res = publish_tale(item_ids,
                       tale,
                       dataone_node,
                       dataone_auth_token,
                       girder_token,
                       userId,
                       prov_info,
                       license_id)
    return res


@girder_job(title='Import Tale')
@app.task(bind=True)
def import_tale(self, lookup_kwargs, tale_kwargs, spawn=True):
    """Create a Tale provided a url for an external data and an image Id.

    Currently, this task only handles importing raw data. In the future, it
    should also allow importing serialized Tales.
    """
    if spawn:
        total = 4
    else:
        total = 3

    self.job_manager.updateProgress(
        message='Gathering basic info about the dataset', total=total,
        current=1)
    dataId = lookup_kwargs.pop('dataId')
    try:
        parameters = dict(dataId=json.dumps(dataId))
        parameters.update(lookup_kwargs)
        dataMap = self.girder_client.get(
            '/repository/lookup', parameters=parameters)
    except girder_client.HttpError as resp:
        try:
            message = json.loads(resp.responseText).get('message', '')
        except json.JSONDecodeError:
            message = str(resp)
        errormsg = 'Unable to register \"{}\". Server returned {}: {}'
        errormsg = errormsg.format(dataId[0], resp.status, message)
        raise ValueError(errormsg)

    if not dataMap:
        errormsg = 'Unable to register \"{}\". Source is not supported'
        errormsg = errormsg.format(dataId[0])
        raise ValueError(errormsg)

    self.job_manager.updateProgress(
        message='Registering the dataset in Whole Tale', total=total,
        current=2)
    self.girder_client.post(
        '/dataset/register', parameters={'dataMap': json.dumps(dataMap)})

    # Get resulting folder/item by name
    catalog_path = '/collection/WholeTale Catalog/WholeTale Catalog'
    catalog = self.girder_client.get(
        '/resource/lookup', parameters={'path': catalog_path})
    folders = self.girder_client.get(
        '/folder', parameters={'name': dataMap[0]['name'],
                               'parentId': catalog['_id'],
                               'parentType': 'folder'}
    )
    try:
        resource = folders[0]
    except IndexError:
        items = self.girder_client.get(
            '/item', parameters={'folderId': catalog['_id'],
                                 'name': dataMap[0]['name']})
        try:
            resource = items[0]
        except IndexError:
            errormsg = 'Registration failed. Aborting!'
            raise ValueError(errormsg)

    # Try to come up with a good name for the dataset
    long_name = resource['name']
    long_name = long_name.replace('-', ' ').replace('_', ' ')
    shortened_name = textwrap.shorten(text=long_name, width=30)

    user = self.girder_client.get('/user/me')
    payload = {
        'authors': user['firstName'] + ' ' + user['lastName'],
        'title': 'A Tale for \"{}\"'.format(shortened_name),
        'dataSet': [
            {
                'mountPath': '/' + resource['name'],
                'itemId': resource['_id'],
                '_modelType': resource['_modelType']
            }
        ],
        'public': False,
        'published': False
    }

    # allow to override title, etc. MUST contain imageId
    payload.update(tale_kwargs)
    tale = self.girder_client.post('/tale', json=payload)

    if spawn:
        self.job_manager.updateProgress(
            message='Creating a Tale container', total=total, current=3)
        try:
            instance = self.girder_client.post(
                '/instance', parameters={'taleId': tale['_id']})
        except girder_client.HttpError as resp:
            try:
                message = json.loads(resp.responseText).get('message', '')
            except json.JSONDecodeError:
                message = str(resp)
            errormsg = 'Unable to create instance. Server returned {}: {}'
            errormsg = errormsg.format(resp.status, message)
            raise ValueError(errormsg)

        while instance['status'] == InstanceStatus.LAUNCHING:
            # TODO: Timeout? Raise error?
            time.sleep(1)
            instance = self.girder_client.get(
                '/instance/{_id}'.format(**instance))
    else:
        instance = None

    self.job_manager.updateProgress(
        message='Tale is ready!', total=total, current=total)
    # TODO: maybe filter results?
    return {'tale': tale, 'instance': instance}
