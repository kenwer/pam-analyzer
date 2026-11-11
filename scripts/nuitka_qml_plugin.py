"""Nuitka user plugin that keeps left-out QML plugins from adding Qt libraries.

build.py leaves most of PySide6's qml folder out with --noinclude-dlls. Nuitka
still scans the dependencies of those ignored plugins and bundles them, so the
Qt libraries behind Qt3D, Quick3D and the rest would ship anyway. This plugin
drops the dependencies of every library below the folders build.py lists in
PAM_QML_EXCLUDED. A Qt library then ships only if something kept links it.
"""

import os

from nuitka.plugins.PluginBase import NuitkaPluginBase


def _resolve(path):
    return os.path.normcase(os.path.realpath(path))


class PamQmlExclusionPlugin(NuitkaPluginBase):
    plugin_name = 'pam-qml-exclusion'
    plugin_desc = 'Drop the dependencies of left-out QML plugins.'

    def __init__(self):
        self.excluded = tuple(
            os.path.join(_resolve(folder), '')
            for folder in os.environ.get('PAM_QML_EXCLUDED', '').split(os.pathsep)
            if folder
        )

    def removeDllDependencies(self, dll_filename, dll_filenames):
        if _resolve(dll_filename).startswith(self.excluded):
            return dll_filenames
        return ()
