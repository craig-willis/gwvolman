import io
import tempfile
import logging
import json
from sys import getsizeof

try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen
from shutil import copyfileobj
import os
import girder_client


from d1_client.mnclient_2_0 import MemberNodeClient_2_0
from d1_common.types.exceptions import DataONEException

from .utils import \
    check_pid, \
    get_file_item, \
    extract_user_id, \
    filter_items, \
    get_dataone_package_url, \
    extract_user_name, \
    get_resource_map_user, \
    generate_dataone_guid, \
    generate_size_progress_message

from .dataone_metadata import \
    generate_system_metadata, \
    create_minimum_eml, \
    create_resource_map

from .constants import \
    ExtraFileNames, \
    license_files, \
    GIRDER_API_URL


def create_upload_eml(tale,
                      client,
                      user,
                      item_ids,
                      license_id,
                      user_id,
                      file_sizes,
                      gc):
    """
    Creates the EML metadata document along with an additional metadata document
    and uploads them both to DataONE. A pid is created for the EML document, and is
    returned so that the resource map can reference it at a later time.

    :param tale: The tale that is being described
    :param client: The client to DataONE
    :param user: The user that is requesting this action
    :param item_ids: The ids of the items that have been uploaded to DataONE
    :param license_id: The ID of the license
    :param user_id: The user that owns this resource
    :param file_sizes: We need to sometimes account for non-data files
     (like tale.yml) .The size needs to be in the EML record so pass them
      in here. The size should be described in bytes
    :param gc: The girder client
    :type tale: wholetale.models.tale
    :type client: MemberNodeClient_2_0
    :type user: girder.models.user
    :type item_ids: list
    :type license_id: str
    :type user_id: str
    :type file_sizes: dict
    :return: pid of the EML document
    :rtype: str
    """

    # Create the EML metadata
    eml_pid = generate_dataone_guid()
    eml_doc = create_minimum_eml(tale,
                                 user,
                                 item_ids,
                                 eml_pid,
                                 file_sizes,
                                 license_id,
                                 user_id,
                                 gc)
    # Create the metadata describing the EML document
    meta = generate_system_metadata(pid=eml_pid,
                                    format_id='eml://ecoinformatics.org/eml-2.1.1',
                                    file_object=eml_doc,
                                    name='metadata.xml',
                                    rights_holder=user_id)
    # meta is type d1_common.types.generated.dataoneTypes_v2_0.SystemMetadata
    # Upload the EML document with its metadata
    upload_file(client=client,
                pid=eml_pid,
                file_object=io.BytesIO(eml_doc),
                system_metadata=meta)
    return eml_pid


def create_dataone_client(mn_base_url, auth_token):
    """
    Creates and returns a member node client

    :param mn_base_url: The url of the member node endpoint
    :param auth_token: The auth token for the user that is using the client
    Should be of the form {"headers": { "Authorization": "Bearer <TOKEN>}}
    :type mn_base_url: str
    :type auth_token: dict
    :return: A client for communicating with a DataONE node
    :rtype: MemberNodeClient_2_0
    """
    return MemberNodeClient_2_0(mn_base_url, **auth_token)


def upload_file(client, pid, file_object, system_metadata):
    """
    Uploads two files to a DataONE member node. The first is an object, which is just a data file.
    The second is a metadata file describing the file object.

    :param client: A client for communicating with a member node
    :param pid: The pid of the data object
    :param file_object: The file object that will be uploaded to the member node
    :param system_metadata: The metadata object describing the file object
    :type client: MemberNodeClient_2_0
    :type pid: str
    :type file_object: str
    :type system_metadata: d1_common.types.generated.dataoneTypes_v2_0.SystemMetadata
    """

    pid = check_pid(pid)
    try:
        client.create(pid, file_object, system_metadata)
    except DataONEException as e:
        return 'Error uploading file to DataONE. {0}'.format(str(e))


def create_upload_resmap(res_pid,
                         eml_pid,
                         obj_pids,
                         client,
                         rights_holder):
    """
    Creates a resource map describing a package and uploads it to DataONE. The
    resource map can be thought of as the glue that holds a package together.

    In order to do this, the following steps are taken.
        1. Create the resource map
        2. Create the metadata document describing the resource map
        3. Upload the pair to DataONE

    :param res_pid: The pid for the resource map
    :param eml_pid: The pid for the metadata document
    :param obj_pids: A list of the pids for each object that was uploaded to DataONE;
     A list of pids that the resource map is documenting.
    :param client: The client to the DataONE member node
    :param rights_holder: The owner of this object
    :type res_pid: str
    :type eml_pid: str
    :type obj_pids: list
    :type client: MemberNodeClient_2_0
    :type rights_holder: str
    :return: None
    """

    res_map = create_resource_map(res_pid, eml_pid, obj_pids)
    # To view the contents of res_map, call d1_common.xml.serialize_to_transport()
    meta = generate_system_metadata(res_pid,
                                    format_id='http://www.openarchives.org/ore/terms',
                                    file_object=res_map,
                                    name=str(),
                                    rights_holder=rights_holder)

    upload_file(client=client,
                pid=res_pid,
                file_object=io.BytesIO(res_map),
                system_metadata=meta)


def upload_license_file(client, license_id, rights_holder):
    """
    Upload a license file to DataONE.

    :param client: The client that interfaces DataONE
    :param license_id: The ID of the license (see `ExtraFileNames` in constants)
    :param rights_holder: The owner of this object
    :type client: MemberNodeClient_2_0
    :type license_id: str
    :type rights_holder: str
    :return: The pid and size of the license file
    """
    # Holds the license text
    license_text = str()
    package_directory = os.path.dirname(os.path.abspath(__file__))
    root_directory = os.path.dirname(package_directory)
    # Path to the license file
    license_path = os.path.join(root_directory, 'gwvolman', 'licenses',
                                license_files[license_id])
    try:
        license_length = os.path.getsize(license_path)
        with open(license_path) as f:
            license_text = f.read()
    except IOError as e:
        logging.warning(e)
        raise ValueError('There was an error processing the license.')

    # Create a pid for the file
    pid = generate_dataone_guid()
    # Create system metadata for the file
    meta = generate_system_metadata(pid=pid,
                                    format_id='text/plain',
                                    file_object=license_text,
                                    name=ExtraFileNames.license_filename,
                                    rights_holder=rights_holder)
    # Upload the file
    upload_file(client=client, pid=pid, file_object=license_text, system_metadata=meta)

    # Return the pid and length of the file
    return pid, license_length


def upload_manifest(tale_id, rights_holder, dataone_client, gc):
    """

    :return:
    """
    manifest = gc.get('/tale/{}/manifest'.format(tale_id))
    manifest_pid = generate_dataone_guid()
    manifest_size = getsizeof(manifest)
    meta = generate_system_metadata(manifest_pid,
                                    format_id='application/json',
                                    file_object=io.BytesIO(str(manifest)),
                                    name='manifest.json',
                                    is_file=True,
                                    rights_holder=rights_holder,
                                    size=manifest_size)

    upload_file(client=dataone_client,
                pid=manifest_pid,
                file_object=manifest,
                system_metadata=meta)

    return manifest_pid, manifest_size


def create_upload_object_metadata(client, file_object, rights_holder, gc):
    """
    Takes a file that exists on the filesystem and
        1. Creates metadata describing it
        2. Uploads the file_object with the metadata to DataONE
        3. Returns a pid that is assigned to file_object so that it can
            be added to the resource map later.

    :param client: The client to the DataONE member node
    :param file_object: The file object that will be uploaded
    :param rights_holder: The owner of this object
    :param gc: The girder client
    :type client: MemberNodeClient_2_0
    :type file_object: girder.models.file
    :type rights_holder: str
    :return: The pid of the object
    :rtype: str
    """

    # PID for the metadata object
    pid = generate_dataone_guid()
    with tempfile.NamedTemporaryFile() as temp_file:
        gc.downloadFile(str(file_object['_id']), temp_file)
        temp_file.seek(0)
        meta = generate_system_metadata(pid,
                                        format_id=file_object['mimeType'],
                                        file_object=temp_file,
                                        name=file_object['name'],
                                        is_file=True,
                                        rights_holder=rights_holder,
                                        size=file_object['size'])
        temp_file.seek(0)
        upload_file(client=client,
                    pid=pid,
                    file_object=temp_file.read(),
                    system_metadata=meta)
    return pid


def create_upload_repository(tale, client, rights_holder, gc):
    """
    Downloads the repository that's pointed to by the recipe and uploads it to the
    node that `client` points to.

    DEVNOTE: This is going to be depreciated by repo2docker

    :param tale: The Tale that is being registered
    :param client: The interface to the member node
    :param rights_holder: The owner of this object
    :param gc: The girder client
    :type tale: girder.models.tale
    :type client: MemberNodeClient_2_0
    :type rights_holder: str
    :return:
    """
    try:
        image = gc.get('/image/{}'.format(tale['imageId']))
        recipe = gc.get('/recipe/{}'.format(image['recipeId']))
        download_url = recipe['url'] + '/tarball/' + recipe['commitId']

        with tempfile.NamedTemporaryFile() as temp_file:
            src = urlopen(download_url)
            try:
                # Copy the response into the temporary file
                copyfileobj(src, temp_file)
            except IOError as e:
                logging.warning(e)
                raise ValueError('Error copying environment file to disk.')

            # Create a pid for the file
            pid = generate_dataone_guid()
            # Create system metadata for the file
            temp_file.seek(0)
            meta = generate_system_metadata(pid=pid,
                                            format_id='application/x-gzip',
                                            file_object=temp_file.read(),
                                            name=ExtraFileNames.environment_file,
                                            rights_holder=rights_holder)
            temp_file.seek(0)
            upload_file(client=client,
                        pid=pid,
                        file_object=io.BytesIO(temp_file.read()),
                        system_metadata=meta)

            size = os.path.getsize(temp_file.name)
        return pid, size

    except IOError as e:
        logging.warning('Failed to process repository'.format(e))
    return None, 0


def create_upload_remote_file(client, rights_holder, item_id, gc):
    """
    Downloads a remote file and then uploads it to the repository pointed to by
    the client.
    :param client: The DataONE client
    :param rights_holder: The owner of the object on DataONE
    :param item_id: The ID of the item that's being processed
    :param gc: The Girder Client
    :return: str, None
    """
    file = get_file_item(item_id, gc)
    try:
        download_url = file.get('linkUrl')
        if download_url is not None:
            with tempfile.NamedTemporaryFile() as temp_file:
                src = urlopen(download_url)
                try:
                    # Copy the response into the temporary file
                    copyfileobj(src, temp_file)

                except IOError as e:
                    logging.warning(e)
                    # We should stop if we can't upload the repository
                    raise ValueError('Error copying environment file to disk.')
            # Create a pid for the file
                pid = generate_dataone_guid()
            # Create system metadata for the file
                temp_file.seek(0)
                meta = generate_system_metadata(pid=pid,
                                                format_id=file['mimeType'],
                                                file_object=temp_file.read(),
                                                name=file['name'],
                                                rights_holder=rights_holder)
                temp_file.seek(0)
                upload_file(client=client,
                            pid=pid,
                            file_object=io.BytesIO(temp_file.read()),
                            system_metadata=meta)

            return pid

    except IOError as e:
        logging.warning(e)
        raise ValueError('Failed to process repository.')
    return None


def publish_tale(job_manager,
                 item_ids,
                 tale_id,
                 dataone_node,
                 dataone_auth_token,
                 girder_token,
                 girder_id,
                 prov_info,
                 license_id):
    """
    Acts as the main function for publishing a Tale to DataONE.
    :param job_manager: Helper object that allows you to set the job progress
    :param item_ids: A list of item ids that are in the package
    :param tale_id: The tale Id
    :param dataone_node: The DataONE member node endpoint
    :param dataone_auth_token: The user's DataONE JWT
    :param girder_token: The user's girder token
    :param girder_id: The user's ID
    :param prov_info: Additional information included in the tale yaml
    :param license_id: The spdx of the license used
    :type item_ids: list
    :type tale_id: str
    :type dataone_node: str
    :type dataone_auth_token: str
    :type girder_token: str
    :type girder_id: str
    :type prov_info: dict
    :type license_id: str
    :return: The pid of the package's resource map
    :rtype: str
    """

    # If there aren't any files, exit
    if not len(item_ids):
        raise ValueError('There are no files in the Tale.')

    # Tracks the current progress level
    current_progress = 5
    job_manager.updateProgress(message='Establishing external connections',
                               total=100,
                               current=current_progress)
    try:
        gc = girder_client.GirderClient(apiUrl=GIRDER_API_URL)
        gc.token = str(girder_token)
    except Exception as e:
        logging.warning(e)
        raise ValueError('Error authenticating with Girder.')

    tale = gc.get('tale/{}/'.format(tale_id))
    if not len(tale):
        raise ValueError('Failed to retrieve Tale.')
    user = gc.getUser(girder_id)

    # create_dataone_client can throw DataONEException
    try:
        """
        Create a client object that is used to interface DataONE. This can interact with a
         particular member node by specifying `repository`. The auth_token is the jwt token from
         DataONE. Close the connection between uploads otherwise some uploads will fail.
        """
        client = create_dataone_client(dataone_node, {
            "headers": {
                "Authorization": "Bearer " + dataone_auth_token,
                "Connection": "close"},
            "user_agent": "safari"})
    except DataONEException as e:
        logging.warning(e)
        # We'll want to exit if we can't create the client
        raise ValueError('Failed to establish connection with DataONE.')

    user_id = extract_user_id(dataone_auth_token)
    full_orcid_name = extract_user_name(dataone_auth_token)
    if not all([user_id, full_orcid_name]):
        # Exit if we can't get the userId from the auth_token
        raise ValueError('Failed to process your DataONE credentials. Please '
                         'ensure you are logged into DataONE.')

    """
    Sort all of the input files based on where they are located,
        1. HTTP resource
        2. DataONE resource
        3. Local filesystem object
    """
    current_progress += 10
    job_manager.updateProgress(message='Processing files',
                               total=100,
                               current=current_progress)
    filtered_items = filter_items(item_ids, gc)
    # Get the total number of files for progress updating
    file_count = len(filtered_items['remote']) + len(filtered_items['local_files'])
    # Check if we have files to upload
    file_progress_progression = 0
    if file_count > 0:
        file_progress_progression = 40/file_count
    else:
        current_progress += 40
    """
    Iterate through the list of objects that are local (ie files without a `linkUrl`
     and upload them to the member node. The call to create_upload_object_metadata will
     return a pid that describes the object (not the metadata object). We'll save
     this pid so that we can pass it to the resource map.
    """
    local_file_pids = list()
    current_progress += 5
    for file in filtered_items['local_files']:
        local_file_pids.append(create_upload_object_metadata(client, file, user_id, gc))
        current_progress += file_progress_progression
        job_manager.updateProgress(message='Uploading {}   Size: {} MB'.format(
            file['name'],
            file['size']/1000000),
            total=100, current=current_progress)

    """
    Iterate through the objects that exist on remote sources (ie an http object), upload
    the file to DataONE, and save the pid so that we can reference it in the resource map.
    """
    remote_file_pids = list()
    for item_id in filtered_items['remote']:
        file = get_file_item(item_id, gc)
        remote_file_pids.append(create_upload_remote_file(client,
                                                          user_id,
                                                          item_id,
                                                          gc))
        current_progress += file_progress_progression
        job_manager.updateProgress(message=generate_size_progress_message(
            file['name'],
            file['size']),
            total=100, current=current_progress)

    """
    Create the tale manifest file. Save the size so that is can be referenced in the EML.
    The pid is also saved so that we can put it in the resource map.
    """
    job_manager.updateProgress(message='Generating tale metadata',
                               total=100,
                               current=current_progress)
    tale_manifest_pid, tale_manifest_length = upload_manifest(str(tale['_id']),
                                                              user_id,
                                                              client,
                                                              gc)

    """
    Upload the license file. Save the size for the EML record, and the pid for
    the resource map.
    """
    current_progress += 5
    job_manager.updateProgress(message='Generating licence '
                                       'information.',
                                       total=100,
                                       current=current_progress)
    license_pid, license_size = upload_license_file(client, license_id, user_id)

    """
    Upload the repository
    """
    current_progress += 10
    job_manager.updateProgress(message='Uploading computing environment information.',
                               total=100,
                               current=current_progress)
    repository_pid, repository_size = create_upload_repository(tale, client, user_id, gc)

    # Create a dictionary that holds the miscellaneous files' sizes for the EML document
    file_sizes = {'tale_manifest': tale_manifest_length,
                  'license': license_size,
                  'repository': repository_size}

    """
    Get all of the items, except the ones that were transferred from an external
    source
    """
    current_progress += 10
    job_manager.updateProgress(message='Generating EML record.',
                               total=100,
                               current=current_progress)
    eml_items = filtered_items.get('dataone') + \
        filtered_items.get('local_items') + filtered_items.get('remote')
    eml_items = filter(None, eml_items)
    eml_items = list(eml_items)
    eml_pid = create_upload_eml(tale,
                                client,
                                user,
                                eml_items,
                                license_id,
                                user_id,
                                file_sizes,
                                gc)

    """
    Once all objects are uploaded, create and upload the resource map. This file describes
    the object relations (ie the package). This should be the last file that is uploaded.
    Also filter out any pids that are None, which would have resulted from an error. This
    prevents referencing objects that failed to upload.
    """

    upload_object_pids = list(local_file_pids +
                              filtered_items['dataone_pids'] +
                              remote_file_pids +
                              [tale_manifest_pid, license_pid, repository_pid])
    upload_object_pids = list(filter(None, upload_object_pids))
    resmap_pid = generate_dataone_guid()
    current_progress += 10
    job_manager.updateProgress(message='Uploading metadata records.',
                               total=100,
                               current=current_progress)

    # The resource map needs to use the non-https ORCID

    create_upload_resmap(resmap_pid,
                         eml_pid,
                         upload_object_pids,
                         client,
                         get_resource_map_user(user_id))
    package_url = get_dataone_package_url(dataone_node, eml_pid)
    current_progress = 100
    job_manager.updateProgress(message='Your Tale has successfully been published '
                                       'to DataONE.',
                               total=100,
                               current=current_progress)
    # Update the tale now that it has been published
    tale['published'] = True
    tale['publishedURI'] = package_url
    tale['doi'] = eml_pid

    gc.put('tale/{}'.format(tale['_id']), data=json.dumps(tale))
