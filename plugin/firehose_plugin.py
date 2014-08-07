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


import cliapp
from contextlib import closing, contextmanager
from fs.tempfs import TempFS
import os
import re

from firehose.config import FirehoseConfig
import morphlib

from debian.debian_support import Version

@contextmanager
def firehose_git(app):
    try:
        username = (app.runcmd_unchecked(["git", "config", "--global", "user.name"]))[1].strip()
        email = (app.runcmd_unchecked(["git", "config", "--global", "user.email"]))[1].strip()
        app.runcmd(["git", "config", "--global", "user.name", "Firehose merge bot"])
        app.runcmd(["git", "config", "--global", "user.email", "firehose@merge.bot"])
        yield ()
    finally:
        app.runcmd(["git", "config", "--global", "user.name", username])
        app.runcmd(["git", "config", "--global", "user.email", email])



class FirehosePlugin(cliapp.Plugin):
    def enable(self):
        self.app.add_subcommand('firehose', self.firehose_cmd,
                                arg_synopsis='some-firehose.yaml...')

    def disable(self):
        pass

    def firehose_cmd(self, args):
        confs = []
        if len(args) == 0:
            raise cliapp.AppException("Expected list of firehoses on command line")
        for fname in args:
            with open(fname, "r") as fh:
                confs.append(FirehoseConfig(fname, fh))
        
        # Ensure all incoming configurations are based on, and landing in, the
        # same repository.  This is because we're only supporting an aggregated
        # integration mode for now.

        if len(set(c.landing_repo for c in confs)) > 1:
            raise cliapp.AppException("Not all firehoses have the same landing repo")
        if len(set(c.landing_baseref for c in confs)) > 1:
            raise cliapp.AppException("Not all firehoses have the same landing baseref")
        if len(set(c.landing_myref for c in confs)) > 1:
            raise cliapp.AppException("Not all firehoses have the same landing myref")

        
        # Ensure that all incoming configurations have unique things they are
        # integrating into.  Note: this allows for the same upstream to be
        # tracked in multiple configs, providing they are all targetting a
        # different part of the system.  (e.g. linux kernels for multiple BSPs)

        if len(set("%s:%s" % (c.landing_stratum,
                              c.landing_chunk) for c in confs)) != len(confs):
            raise cliapp.AppException("Not all firehoses have unique landing locations")

        with closing(TempFS(temp_dir=self.app.settings['tempdir'])) as tfs, \
                firehose_git(self.app):
            self.base_path = tfs.getsyspath("/")
            self.make_workspace()
            self.make_branch(confs[0].landing)
            self.reset_to_tracking(confs[0].landing)
            self.load_morphologies()
            for c in confs:
                self.update_for_conf(c)
            if self.updated_morphologies():
                print self.app.runcmd_unchecked(["git", "diff"], cwd=self.gitpath)[1]

    def make_path(self, *subpath):
        return os.path.join(self.base_path, *subpath)

    def make_workspace(self):
        self.app.subcommands['init']([self.make_path("ws")])

    def make_branch(self, root):
        os.chdir(self.make_path("ws"))
        try:
            self.app.subcommands['branch']([root['repo'], root['myref'], root['baseref']])
        except cliapp.AppException, ae:
            if "already exists in" in str(ae):
                self.app.subcommands['checkout']([root['repo'], root['myref']])
            else:
                raise
        repopath = root['repo'].replace(':', '/')
        self.gitpath = self.make_path("ws", root['myref'], repopath)

    def reset_to_tracking(self, root):
        branch_head_sha = self.app.runcmd(['git', 'rev-parse', 'HEAD'],
                                          cwd=self.gitpath).strip()
        self.app.runcmd(['git', 'reset', '--hard', 'origin/%s' % root['baseref']],
                        cwd=self.gitpath)
        self.app.runcmd(['git', 'reset', '--soft', branch_head_sha],
                        cwd=self.gitpath)

    def load_morphologies(self):
        ws = morphlib.workspace.open(self.gitpath)
        sb = morphlib.sysbranchdir.open_from_within(self.gitpath)
        loader = morphlib.morphloader.MorphologyLoader()
        morphs = morphlib.morphset.MorphologySet()
        for morph in sb.load_all_morphologies(loader):
            morphs.add_morphology(morph)

        self.ws = ws
        self.sb = sb
        self.loader = loader
        self.morphs = morphs
        self.lrc, self.rrc = morphlib.util.new_repo_caches(self.app)

    def find_cached_repo(self, stratum, chunk):
        urls = []
        def wanted_spec(m, kind, spec):
            if not m['kind'] == 'stratum' and kind == 'chunks':
                return False
            if m.get('name') == stratum and spec.get('name') == chunk:
                return True
        def process_spec(m, kind, spec):
            urls.append(spec['repo'])
            return False
        self.morphs.traverse_specs(process_spec, wanted_spec)
        if len(urls) != 1:
            raise cliapp.AppException(
                "Woah! expected 1 chunk matching %s:%s (got %d)" % (
                    stratum, chunk, len(urls)))
        return self.lrc.get_updated_repo(urls[0])

    def git_in_repo_unchecked(self, repo, *args):
        args = list(args)
        args.insert(0, "git")
        return self.app.runcmd_unchecked(args, cwd=repo.path)
    def git_in_repo(self, repo, *args):
        args = list(args)
        args.insert(0, "git")
        return self.app.runcmd(args, cwd=repo.path)

    def all_shas_for_refs(self, repo):
        all_lines = self.git_in_repo(repo, "for-each-ref").strip().split("\n")
        for refline in all_lines:
            (sha, objtype, name) = refline.split(None, 2)
            if objtype == 'tag':
                sha = self.git_in_repo(repo, "rev-list", "-1", sha).strip()
            yield name, sha

    def interested_in_ref(self, conf, refname):
        if conf.tracking_mode == 'follow-tip':
            return refname == conf.tracking_ref
        elif conf.tracking_mode == 'refs':
            return any(re.match(filterstr, refname)
                       for filterstr in conf.tracking_filters)
        else:
            raise FirehoseConfigError(conf, "Unknown value: %s" %
                                      conf.tracking_mode, ["tracking", "mode"])
        
    def rewrite_ref(self, conf, ref):
        if conf.tracking_mode == 'refs':
            for transform in conf.tracking_transforms:
                ref = re.sub(transform['match'], transform['replacement'], ref)
        return ref

    def compare_refs(self, ref1, ref2):
        if ref1[0] == ref2[0]:
            return 0
        v1 = Version(ref1[0].replace("/", "-"))
        v2 = Version(ref2[0].replace("/", "-"))
        if v1 < v2:
            return -1
        return 1

    def sanitise_refname(self, refname):
        if refname.startswith("refs/"):
            return "/".join((refname.split("/"))[2:])
        else:
            return refname

    def update_refs(self, stratum, chunk, sha, refname):
        def wanted_spec(m, kind, spec):
            if not m['kind'] == 'stratum' and kind == 'chunks':
                return False
            if m.get('name') == stratum and spec.get('name') == chunk:
                return True
        def process_spec(m, kind, spec):
            spec['ref'] = sha
            spec['unpetrify-ref'] = refname
            return True
        self.morphs.traverse_specs(process_spec, wanted_spec)

    def update_for_conf(self, conf):
        stratum = conf.landing_stratum
        chunk = conf.landing_chunk
        crc = self.find_cached_repo(stratum, chunk)
        interesting_refs = [
            (self.rewrite_ref(conf, name), name, sha)
            for (name, sha) in self.all_shas_for_refs(crc)
            if self.interested_in_ref(conf, name)]
        interesting_refs.sort(self.compare_refs)
        (_, refname, sha) = interesting_refs.pop()
        refname = self.sanitise_refname(refname)
        self.update_refs(stratum, chunk, sha, refname)

    def updated_morphologies(self):
        if not any(m.dirty for m in self.morphs.morphologies):
            return False
        for morph in self.morphs.morphologies:
            if morph.dirty:
                self.loader.unset_defaults(morph)
                self.loader.save_to_file(
                    self.sb.get_filename(morph.repo_url, morph.filename), morph)
                morph.dirty = False
        
        return True
