import time
import os

from mako.template import Template
from website.addons.dataverse.client import connect

from website.project.decorators import must_be_contributor_or_public
from website.project.decorators import must_have_addon
from website.util import rubeus

import hurry


def dataverse_hgrid_root(node_settings, auth, state=None, **kwargs):

    node = node_settings.owner
    state = state or 'draft' if node.can_edit(auth) else 'released'

    connection = connect(
        node_settings.dataverse_username,
        node_settings.dataverse_password
    )

    # Quit if no study linked
    if node_settings.study_hdl is None or connection is None:
        return []

    dataverse = connection.get_dataverse(node_settings.dataverse_alias)
    study = dataverse.get_study_by_hdl(node_settings.study_hdl)

    name = '{0} / {1}'.format(
            node_settings.dataverse,
            node_settings.study,
    )

    permissions = {
        'edit': node.can_edit(auth) and not node.is_registration and state == 'draft',
        'view': node.can_view(auth)
    }

    urls = {
        'upload': node.api_url_for('dataverse_upload_file'),
        'fetch': node.api_url_for('dataverse_hgrid_data_contents', state=state),
        'branch': node.api_url_for('dataverse_root_folder_public'),
    }

    state_append = ' [{}]'.format(state.capitalize())
    if study.get_released_files() and node.can_edit(auth):
        state_append = dataverse_state_widget(state)

    return [rubeus.build_addon_root(
        node_settings,
        name,
        urls=urls,
        permissions=permissions,
        extra=state_append,
    )]

dataverse_state_template = Template('''
    <select class="dataverse-state-select">
        <option ${"selected" if state == "draft" else ""}>Draft</option>
        <option ${"selected" if state == "released" else ""}>Released</option>
    </select>
''')

def dataverse_state_widget(state):
    """Render branch selection widget for GitHub add-on. Displayed in the
    name field of HGrid file trees.

    """
    rendered = dataverse_state_template.render(
        state=state
    )
    return rendered

@must_be_contributor_or_public
@must_have_addon('dataverse', 'node')
def dataverse_root_folder_public(state=None, **kwargs):

    node = kwargs['node']
    node_settings = kwargs['node_addon']
    auth = kwargs['auth']
    state = state or 'draft' if node.can_edit(auth) else 'released'

    return dataverse_hgrid_root(node_settings, auth=auth, state=state)


@must_be_contributor_or_public
@must_have_addon('dataverse', 'node')
def dataverse_hgrid_data_contents(state=None, **kwargs):

    node_settings = kwargs['node_addon']
    auth = kwargs['auth']
    node = kwargs['node'] or kwargs['project']
    state = state or 'draft' if node.can_edit(auth) else 'released'
    released = state=='released'

    can_edit = node.can_edit(auth) and not node.is_registration and not released
    can_view = node.can_view(auth)

    connection = connect(
        node_settings.dataverse_username,
        node_settings.dataverse_password
    )

    if node_settings.study_hdl is None or connection is None:
        return []

    info = []

    dataverse = connection.get_dataverse(node_settings.dataverse_alias)
    study = dataverse.get_study_by_hdl(node_settings.study_hdl)

    for f in study.get_files(released):

        item = {
            rubeus.KIND: rubeus.FILE,
            'name': f.name,
            'urls': {
                    'view': node.web_url_for('dataverse_view_file',
                                             path=f.id),
                    'download': node.api_url_for('dataverse_download_file',
                                                 path=f.id),
                    'delete': node.api_url_for('dataverse_delete_file',
                                               path=f.id),
            },
            'permissions': {
                'view': can_view,
                'edit': can_edit,
            },
            'size': [
                    float(0), # TODO: Implement file size (if possible?),
                    hurry.filesize.size(0, system=hurry.filesize.alternative)
            ],
        }
        info.append(item)

    return info
