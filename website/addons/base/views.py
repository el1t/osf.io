import json
import uuid
import httplib
import functools

import furl
from flask import request
from flask import redirect
from flask import make_response
from modularodm.exceptions import NoResultsFound

from framework.auth import Auth
from framework.sessions import session
from framework.sentry import log_exception
from framework.exceptions import HTTPError
from framework.auth.decorators import must_be_logged_in, must_be_signed

from website import mails
from website import settings
from website.files.models import FileNode
from website.project import decorators
from website.addons.base import exceptions
from website.addons.base import StorageAddonBase
from website.models import User, Node, NodeLog
from website.util import rubeus
from website.profile.utils import get_gravatar
from website.project.decorators import must_be_valid_project, must_be_contributor_or_public
from website.project.utils import serialize_node


@decorators.must_have_permission('write')
@decorators.must_not_be_registration
def disable_addon(auth, **kwargs):
    node = kwargs['node'] or kwargs['project']

    addon_name = kwargs.get('addon')
    if addon_name is None:
        raise HTTPError(httplib.BAD_REQUEST)

    deleted = node.delete_addon(addon_name, auth)

    return {'deleted': deleted}


@must_be_logged_in
def get_addon_user_config(**kwargs):

    user = kwargs['auth'].user

    addon_name = kwargs.get('addon')
    if addon_name is None:
        raise HTTPError(httplib.BAD_REQUEST)

    addon = user.get_addon(addon_name)
    if addon is None:
        raise HTTPError(httplib.BAD_REQUEST)

    return addon.to_json(user)


def check_file_guid(guid):

    guid_url = '/{0}/'.format(guid._id)
    if not request.path.startswith(guid_url):
        url_split = request.url.split(guid.file_url)
        try:
            guid_url += url_split[1].lstrip('/')
        except IndexError:
            pass
        return guid_url
    return None

permission_map = {
    'create_folder': 'write',
    'revisions': 'read',
    'metadata': 'read',
    'download': 'read',
    'upload': 'write',
    'delete': 'write',
    'copy': 'write',
    'move': 'write',
    'copyto': 'write',
    'moveto': 'write',
    'copyfrom': 'read',
    'movefrom': 'write',
}


def check_access(node, user, action, key=None):
    """Verify that user can perform requested action on resource. Raise appropriate
    error code if action cannot proceed.
    """
    permission = permission_map.get(action, None)
    if permission is None:
        raise HTTPError(httplib.BAD_REQUEST)
    if node.has_permission(user, permission):
        return True
    if permission == 'read':
        if node.is_public or key in node.private_link_keys_active:
            return True
    # Users attempting to register projects with components might not have
    # `write` permissions for all components. This will result in a 403 for
    # all `copyto` actions as well as `copyfrom` actions if the component
    # in question is not public. To get around this, we have to recursively
    # check the node's parent node to determine if they have `write`
    # permissions up the stack.
    # TODO(hrybacki): is there a way to tell if this is for a registration?
    # All nodes being registered that receive the `copyto` action will have
    # `node.is_registration` == True. However, we have no way of telling if
    # `copyfrom` actions are originating from a node being registered.
    if action == 'copyfrom' or (action == 'copyto' and node.is_registration):
        parent = node.parent_node
        while parent:
            if parent.has_permission(user, 'write'):
                return True
            parent = parent.parent_node

    code = httplib.FORBIDDEN if user else httplib.UNAUTHORIZED
    raise HTTPError(code)


def make_auth(user):
    if user is not None:
        return {
            'id': user._id,
            'email': '{}@osf.io'.format(user._id),
            'name': user.fullname,
        }
    return {}


def restrict_addrs(*addrs):
    def wrapper(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            remote = request.remote_addr
            if remote not in addrs:
                raise HTTPError(httplib.FORBIDDEN)
            return func(*args, **kwargs)
        return wrapped
    return wrapper


restrict_waterbutler = restrict_addrs(*settings.WATERBUTLER_ADDRS)


@restrict_waterbutler
def get_auth(**kwargs):
    try:
        action = request.args['action']
        node_id = request.args['nid']
        provider_name = request.args['provider']
    except KeyError:
        raise HTTPError(httplib.BAD_REQUEST)

    cookie = request.args.get('cookie')
    view_only = request.args.get('view_only')

    if 'auth_user_id' in session.data:
        user = User.load(session.data['auth_user_id'])
    elif cookie:
        user = User.from_cookie(cookie)
    else:
        user = None

    node = Node.load(node_id)
    if not node:
        raise HTTPError(httplib.NOT_FOUND)

    check_access(node, user, action, key=view_only)

    provider_settings = node.get_addon(provider_name)
    if not provider_settings:
        raise HTTPError(httplib.BAD_REQUEST)

    try:
        credentials = provider_settings.serialize_waterbutler_credentials()
        settings = provider_settings.serialize_waterbutler_settings()
    except exceptions.AddonError:
        log_exception()
        raise HTTPError(httplib.BAD_REQUEST)

    return {
        'auth': make_auth(user),
        'credentials': credentials,
        'settings': settings,
        'callback_url': node.api_url_for(
            ('create_waterbutler_log' if not node.is_registration else 'registration_callbacks'),
            _absolute=True,
        ),
    }


LOG_ACTION_MAP = {
    'move': NodeLog.FILE_MOVED,
    'copy': NodeLog.FILE_COPIED,
    'create': NodeLog.FILE_ADDED,
    'update': NodeLog.FILE_UPDATED,
    'delete': NodeLog.FILE_REMOVED,
    'create_folder': NodeLog.FOLDER_CREATED,
}


@must_be_signed
@restrict_waterbutler
@must_be_valid_project
def create_waterbutler_log(payload, **kwargs):
    try:
        auth = payload['auth']
        action = LOG_ACTION_MAP[payload['action']]
    except KeyError:
        raise HTTPError(httplib.BAD_REQUEST)

    user = User.load(auth['id'])
    if user is None:
        raise HTTPError(httplib.BAD_REQUEST)

    auth = Auth(user=user)
    node = kwargs['node'] or kwargs['project']

    if action in (NodeLog.FILE_MOVED, NodeLog.FILE_COPIED):
        for bundle in ('source', 'destination'):
            for key in ('provider', 'materialized', 'name', 'nid'):
                if key not in payload[bundle]:
                    raise HTTPError(httplib.BAD_REQUEST)

        destination_node = node  # For clarity
        source_node = Node.load(payload['source']['nid'])

        source = source_node.get_addon(payload['source']['provider'])
        destination = node.get_addon(payload['destination']['provider'])

        payload['source'].update({
            'materialized': payload['source']['materialized'].lstrip('/'),
            'addon': source.config.full_name,
            'url': source_node.web_url_for(
                'addon_view_or_download_file',
                path=payload['source']['path'].lstrip('/'),
                provider=payload['source']['provider']
            ),
            'node': {
                '_id': source_node._id,
                'url': source_node.url,
                'title': source_node.title,
            }
        })

        payload['destination'].update({
            'materialized': payload['destination']['materialized'].lstrip('/'),
            'addon': destination.config.full_name,
            'url': destination_node.web_url_for(
                'addon_view_or_download_file',
                path=payload['destination']['path'].lstrip('/'),
                provider=payload['destination']['provider']
            ),
            'node': {
                '_id': destination_node._id,
                'url': destination_node.url,
                'title': destination_node.title,
            }
        })

        payload.update({
            'node': destination_node._id,
            'project': destination_node.parent_id,
        })

        if not payload.get('errors'):
            destination_node.add_log(
                action=action,
                auth=auth,
                params=payload
            )

        if payload.get('email') is True or payload.get('errors'):
            mails.send_mail(
                user.username,
                mails.FILE_OPERATION_FAILED if payload.get('errors')
                else mails.FILE_OPERATION_SUCCESS,
                action=payload['action'],
                source_node=source_node,
                destination_node=destination_node,
                source_path=payload['source']['path'],
                destination_path=payload['source']['path'],
                source_addon=payload['source']['addon'],
                destination_addon=payload['destination']['addon'],
            )
    else:
        try:
            metadata = payload['metadata']
            node_addon = node.get_addon(payload['provider'])
        except KeyError:
            raise HTTPError(httplib.BAD_REQUEST)

        if node_addon is None:
            raise HTTPError(httplib.BAD_REQUEST)

        metadata['path'] = metadata['path'].lstrip('/')

        node_addon.create_waterbutler_log(auth, action, metadata)

    return {'status': 'success'}


@must_be_valid_project
def addon_view_or_download_file_legacy(**kwargs):
    query_params = request.args.to_dict()
    node = kwargs.get('node') or kwargs['project']

    action = query_params.pop('action', 'view')
    provider = kwargs.get('provider', 'osfstorage')

    if kwargs.get('path'):
        path = kwargs['path']
    elif kwargs.get('fid'):
        path = kwargs['fid']

    if 'download' in request.path or request.path.startswith('/api/v1/'):
        action = 'download'

    if kwargs.get('vid'):
        query_params['version'] = kwargs['vid']

    # If provider is OSFstorage, check existence of requested file in the filetree
    # This prevents invalid GUIDs from being created
    if provider == 'osfstorage':
        node_settings = node.get_addon('osfstorage')

        try:
            path = node_settings.get_root().find_child_by_name(path)._id
        except NoResultsFound:
            raise HTTPError(
                404, data=dict(
                    message_short='File not found',
                    message_long='You requested a file that does not exist.'
                )
            )

    return redirect(
        node.web_url_for(
            'addon_view_or_download_file',
            path=path,
            provider=provider,
            action=action,
            **query_params
        ),
        code=httplib.MOVED_PERMANENTLY
    )


@must_be_valid_project
@must_be_contributor_or_public
def addon_view_or_download_file(auth, path, provider, **kwargs):
    extras = request.args.to_dict()
    extras.pop('_', None)  # Clean up our url params a bit
    action = extras.get('action', 'view')
    node = kwargs.get('node') or kwargs['project']

    node_addon = node.get_addon(provider)

    if not path:
        raise HTTPError(httplib.BAD_REQUEST)

    if not isinstance(node_addon, StorageAddonBase):
        raise HTTPError(httplib.BAD_REQUEST, {
            'message_short': 'Bad Request',
            'message_long': 'The add-on containing this file is no longer connected to the {}.'.format(node.project_or_component)
        })

    if not node_addon.has_auth:
        raise HTTPError(httplib.UNAUTHORIZED, {
            'message_short': 'Unauthorized',
            'message_long': 'The add-on containing this file is no longer authorized.'
        })

    if not node_addon.complete:
        raise HTTPError(httplib.BAD_REQUEST, {
            'message_short': 'Bad Request',
            'message_long': 'The add-on containing this file is no longer configured.'
        })

    file_node = FileNode.resolve_class(provider, FileNode.FILE).get_or_create(node, path)

    version = file_node.touch(**extras)

    if version is None:
        raise HTTPError(httplib.NOT_FOUND, {
            'message_short': 'Not Found',
            'message_long': 'This file does not exist'
        })

    if request.method == 'HEAD':
        return make_response(('', 200, {
            'Location': file_node.generate_download_url(**dict(extras, accept_url='false'))
        }))

    if action == 'download':
        return redirect(file_node.generate_download_url(**dict(extras, accept_url='false')))

    if len(request.path.strip('/').split('/')) > 1:
        guid = file_node.get_guid(create=True)
        return redirect(furl.furl('/{}/'.format(guid._id)).set(args=extras).url)

    return addon_view_file(auth, node, file_node, version)


def addon_view_file(auth, node, file_node, version):
    # TODO: resolve circular import issue
    from website.addons.wiki import settings as wiki_settings

    if isinstance(version, tuple):
        version, error = version
        error = error.replace('\n', '').strip()
    else:
        error = None

    ret = serialize_node(node, auth, primary=True)

    if file_node._id not in node.file_guid_to_share_uuids:
        node.file_guid_to_share_uuids[file_node._id] = uuid.uuid4()
        node.save()

    if ret['user']['can_edit']:
        sharejs_uuid = str(node.file_guid_to_share_uuids[file_node._id])
    else:
        sharejs_uuid = None

    download_url = furl.furl(request.url.encode('utf-8')).set(args=dict(request.args, **{
        'mode': 'render',
        'action': 'download',
        'accept_url': 'false'
    }))

    render_url = furl.furl(settings.MFR_SERVER_URL).set(
        path=['render'],
        args={'url': download_url.url}
    )

    ret.update({
        'urls': {
            'render': render_url.url,
            'mfr': settings.MFR_SERVER_URL,
            'sharejs': wiki_settings.SHAREJS_URL,
            'gravatar': get_gravatar(auth.user, 25),
            'files': node.web_url_for('collect_file_trees'),
        },
        'error': error,
        'file_name': file_node.name,
        'file_path': file_node.path,
        'sharejs_uuid': sharejs_uuid,
        'provider': file_node.provider,
        'materialized_path': file_node.materialized_path,
        'extra': json.dumps(version.metadata.get('extra', {})),
        'size': version.size if version.size is not None else 9966699,
        'private': getattr(node.get_addon(file_node.provider), 'is_private', False),
    })

    ret.update(rubeus.collect_addon_assets(node))
    return ret
