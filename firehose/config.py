# Copyright (C) 2014  Codethink Limited
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import yaml

class FirehoseConfigError(Exception):
    def __init__(self, config, what, path):
        self.config = config
        self.what = what
        self.path = path

    def __repr__(self):
        return "<FirehoseConfigError %s: %s (@ %s)>" % (
            self.config.sourcename, self.what, (".").join(self.path))

    def __str__(self):
        return repr(self)

class FirehoseConfig:
    def __init__(self, sourcename, readfrom):
        self.sourcename = sourcename
        self.content = yaml.safe_load(readfrom)
        assert(self.content.get("kind") == "firehose")
        
    def __getattr__(self, attrname):
        attrpath = attrname.split("_")
        node = self.content
        pathused = []
        while attrpath:
            elem = attrpath.pop(0)
            pathused.append(elem)
            if node.get(elem) is None:
                raise FirehoseConfigError(self, "Unknown element", pathused)
            else:
                node = node.get(elem)
        return node
