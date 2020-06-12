# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Plumbing command for dataset installation"""


import logging
import re
import requests
from os.path import expanduser
from collections import OrderedDict
from urllib.parse import unquote as urlunquote

from datalad.interface.base import Interface
from datalad.interface.utils import eval_results
from datalad.interface.base import build_doc
from datalad.interface.results import get_status_dict
from datalad.interface.common_opts import (
    location_description,
    reckless_opt,
)
from datalad.log import log_progress
from datalad.support.gitrepo import (
    GitRepo,
)
from datalad.cmd import (
    CommandError,
    GitRunner,
    StdOutCapture,
    WitlessRunner,
)
from datalad.distributed.ora_remote import (
    LocalIO,
    RIARemoteError,
    SSHRemoteIO,
)
from datalad.support.annexrepo import AnnexRepo
from datalad.support.constraints import (
    EnsureNone,
    EnsureStr,
    EnsureKeyChoice,
)
from datalad.support.param import Parameter
from datalad.support.network import (
    get_local_file_url,
    URL,
    RI,
    DataLadRI,
    PathRI,
)
from datalad.dochelpers import (
    exc_str,
    single_or_plural,
)
from datalad.utils import (
    assure_bool,
    knows_annex,
    make_tempfile,
    Path,
    PurePosixPath,
    rmtree,
)

from datalad.distribution.dataset import (
    Dataset,
    datasetmethod,
    resolve_path,
    require_dataset,
    EnsureDataset,
)
from datalad.distribution.utils import (
    _get_flexible_source_candidates,
)
from datalad.utils import (
    check_symlink_capability
)

__docformat__ = 'restructuredtext'

lgr = logging.getLogger('datalad.core.distributed.clone')


@build_doc
class Clone(Interface):
    """Obtain a dataset (copy) from a URL or local directory

    The purpose of this command is to obtain a new clone (copy) of a dataset
    and place it into a not-yet-existing or empty directory. As such `clone`
    provides a strict subset of the functionality offered by `install`. Only a
    single dataset can be obtained, and immediate recursive installation of
    subdatasets is not supported. However, once a (super)dataset is installed
    via `clone`, any content, including subdatasets can be obtained by a
    subsequent `get` command.

    Primary differences over a direct `git clone` call are 1) the automatic
    initialization of a dataset annex (pure Git repositories are equally
    supported); 2) automatic registration of the newly obtained dataset as a
    subdataset (submodule), if a parent dataset is specified; 3) support
    for additional resource identifiers (DataLad resource identifiers as used
    on datasets.datalad.org, and RIA store URLs as used for store.datalad.org
    - optionally in specific versions as identified by a branch or a tag; see
    examples); and 4) automatic configurable generation of alternative access
    URL for common cases (such as appending '.git' to the URL in case the
    accessing the base URL failed).

    || PYTHON >>By default, the command returns a single Dataset instance for
    an installed dataset, regardless of whether it was newly installed ('ok'
    result), or found already installed from the specified source ('notneeded'
    result).<< PYTHON ||

    .. seealso::

      :ref:`handbook:3-001`
        More information on Remote Indexed Archive (RIA) stores
    """
    # by default ignore everything but install results
    # i.e. no "add to super dataset"
    result_filter = EnsureKeyChoice('action', ('install',))
    # very frequently this command will yield exactly one installed dataset
    # spare people the pain of going through a list by default
    return_type = 'item-or-list'
    # as discussed in #1409 and #1470, we want to return dataset instances
    # matching what is actually available after command completion (and
    # None for any failed dataset installation)
    result_xfm = 'successdatasets-or-none'

    _examples_ = [
        dict(text="Install a dataset from Github into the current directory",
             code_py="clone("
             "source='https://github.com/datalad-datasets/longnow"
             "-podcasts.git')",
             code_cmd="datalad clone "
             "https://github.com/datalad-datasets/longnow-podcasts.git"),
        dict(text="Install a dataset into a specific directory",
             code_py="""\
             clone(source='https://github.com/datalad-datasets/longnow-podcasts.git',
                   path='myfavpodcasts')""",
             code_cmd="""\
             datalad clone https://github.com/datalad-datasets/longnow-podcasts.git \\
             myfavpodcasts"""),
        dict(text="Install a dataset as a subdataset into the current dataset",
             code_py="""\
             clone(dataset='.',
                   source='https://github.com/datalad-datasets/longnow-podcasts.git')""",
             code_cmd="datalad clone -d . "
             "https://github.com/datalad-datasets/longnow-podcasts.git"),
        dict(text="Install the main superdataset from datasets.datalad.org",
             code_py="clone(source='///')",
             code_cmd="datalad clone ///"),
        dict(text="Install a dataset identified by a literal alias from store.datalad.org",
             code_py="clone(source='ria+http://store.datalad.org#~hcp-openaccess')",
             code_cmd="datalad clone ria+http://store.datalad.org#~hcp-openaccess"),
        dict(
            text="Install a dataset in a specific version as identified by a"
                 "branch or tag name from store.datalad.org",
            code_py="clone(source='ria+http://store.datalad.org#76b6ca66-36b1-11ea-a2e6-f0d5bf7b5561@myidentifier')",
            code_cmd="datalad clone ria+http://store.datalad.org#76b6ca66-36b1-11ea-a2e6-f0d5bf7b5561@myidentifier"),
        dict(
            text="Install a dataset with group-write access permissions",
            code_py=\
            "clone(source='http://example.com/dataset', reckless='shared-group')",
            code_cmd=\
            "datalad clone http://example.com/dataset --reckless shared-group"),
    ]

    _params_ = dict(
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""(parent) dataset to clone into. If given, the newly cloned
            dataset is registered as a subdataset of the parent. Also, if given,
            relative paths are interpreted as being relative to the parent
            dataset, and not relative to the working directory.""",
            constraints=EnsureDataset() | EnsureNone()),
        source=Parameter(
            args=("source",),
            metavar='SOURCE',
            doc="""URL, DataLad resource identifier, local path or instance of
            dataset to be cloned""",
            constraints=EnsureStr() | EnsureNone()),
        path=Parameter(
            args=("path",),
            metavar='PATH',
            nargs="?",
            doc="""path to clone into.  If no `path` is provided a
            destination path will be derived from a source URL
            similar to :command:`git clone`"""),
        description=location_description,
        reckless=reckless_opt,
    )

    @staticmethod
    @datasetmethod(name='clone')
    @eval_results
    def __call__(
            source,
            path=None,
            dataset=None,
            description=None,
            reckless=None):
        # did we explicitly get a dataset to install into?
        # if we got a dataset, path will be resolved against it.
        # Otherwise path will be resolved first.
        ds = require_dataset(
            dataset, check_installed=True, purpose='cloning') \
            if dataset is not None else dataset
        refds_path = ds.path if ds else None

        # legacy compatibility
        if reckless is True:
            # so that we can forget about how things used to be
            reckless = 'auto'
        if reckless is None and ds:
            # if reckless is not explicitly given, but we operate on a
            # superdataset, query whether it has been instructed to operate
            # in a reckless mode, and inherit it for the coming clone
            reckless = ds.config.get('datalad.clone.reckless', None)

        if isinstance(source, Dataset):
            source = source.path

        if source == path:
            # even if they turn out to be identical after resolving symlinks
            # and more sophisticated witchcraft, it would still happily say
            # "it appears to be already installed", so we just catch an
            # obviously pointless input combination
            raise ValueError(
                "clone `source` and destination `path` are identical [{}]. "
                "If you are trying to add a subdataset simply use `save`".format(
                    path))

        if path is not None:
            path = resolve_path(path, dataset)

        # derive target from source:
        if path is None:
            # we got nothing but a source. do something similar to git clone
            # and derive the path from the source and continue
            # since this is a relative `path`, resolve it:
            # we are not going to reuse the decoded URL, as this is done for
            # all source candidates in clone_dataset(), we just use to determine
            # a destination path here in order to perform a bunch of additional
            # checks that shall not pollute the helper function
            source_ = decode_source_spec(
                source, cfg=None if ds is None else ds.config)
            path = resolve_path(source_['default_destpath'], dataset)
            lgr.debug("Determined clone target path from source")
        lgr.debug("Resolved clone target path to: '%s'", path)

        # there is no other way -- my intoxicated brain tells me
        assert(path is not None)

        result_props = dict(
            action='install',
            logger=lgr,
            refds=refds_path,
            source_url=source)

        try:
            # this will implicitly cause pathlib to run a bunch of checks
            # whether the present path makes any sense on the platform
            # we are running on -- we don't care if the path actually
            # exists at this point, but we want to abort early if the path
            # spec is determined to be useless
            path.exists()
        except OSError as e:
            yield get_status_dict(
                status='error',
                path=path,
                message=('cannot handle target path: %s', exc_str(e)),
                **result_props)
            return

        destination_dataset = Dataset(path)
        result_props['ds'] = destination_dataset

        if ds is not None and ds.pathobj not in path.parents:
            yield get_status_dict(
                status='error',
                message=("clone target path '%s' not in specified target dataset '%s'",
                         path, ds),
                **result_props)
            return

        # perform the actual cloning operation
        yield from clone_dataset(
            [source],
            destination_dataset,
            reckless,
            description,
            result_props,
            cfg=None if ds is None else ds.config,
        )

        # TODO handle any 'version' property handling and verification using a dedicated
        # public helper

        if ds is not None:
            # we created a dataset in another dataset
            # -> make submodule
            for r in ds.save(
                    path,
                    return_type='generator',
                    result_filter=None,
                    result_xfm=None,
                    on_failure='ignore'):
                yield r


def clone_dataset(
        srcs,
        destds,
        reckless=None,
        description=None,
        result_props=None,
        cfg=None):
    """Internal helper to perform cloning without sanity checks (assumed done)

    This helper does not handle any saving of subdataset modification or adding
    in a superdataset.

    Parameters
    ----------
    srcs : list
      Any suitable clone source specifications (paths, URLs)
    destds : Dataset
      Dataset instance for the clone destination
    reckless : {None, 'auto', 'ephemeral', 'shared-...'}, optional
      Mode switch to put cloned dataset into unsafe/throw-away configurations, i.e.
      sacrifice data safety for performance or resource footprint.
    description : str, optional
      Location description for the annex of the dataset clone (if there is any).
    result_props : dict, optional
      Default properties for any yielded result, passed on to get_status_dict().
    cfg : ConfigManager, optional
      Configuration will be queried from this instance (i.e. from a particular
      dataset). If None is given, the global DataLad configuration will be
      queried.

    Yields
    ------
    dict
      DataLad result records
    """
    if not result_props:
        # in case the caller had no specific idea on how results should look
        # like, provide sensible defaults
        result_props = dict(
            action='install',
            logger=lgr,
            ds=destds,
        )

    dest_path = destds.pathobj

    # decode all source candidate specifications
    candidate_sources = [decode_source_spec(s, cfg=cfg) for s in srcs]

    # now expand the candidate sources with additional variants of the decoded
    # giturl, while duplicating the other properties in the additional records
    # for simplicity. The hope is to overcome a few corner cases and be more
    # robust than git clone
    candidate_sources = [
        dict(props, giturl=s) for props in candidate_sources
        for s in _get_flexible_source_candidates(props['giturl'])
    ]

    # important test! based on this `rmtree` will happen below after failed clone
    dest_path_existed = dest_path.exists()
    if dest_path_existed and any(dest_path.iterdir()):
        if destds.is_installed():
            # check if dest was cloned from the given source before
            # this is where we would have installed this from
            # this is where it was actually installed from
            track_name, track_url = _get_tracking_source(destds)
            try:
                # this will get us track_url in system native path conventions,
                # whenever it is a path (and not a URL)
                # this is needed to match it to any potentially incoming local
                # source path in the 'notneeded' test below
                track_path = str(Path(track_url))
            except Exception:
                # this should never happen, because Path() will let any non-path stringification
                # pass through unmodified, but we do not want any potential crash due to
                # pathlib behavior changes
                lgr.debug("Unexpected behavior of pathlib!")
                track_path = None
            for cand in candidate_sources:
                src = cand['giturl']
                if track_url == src \
                        or get_local_file_url(track_url, compatibility='git') == src \
                        or track_path == expanduser(src):
                    yield get_status_dict(
                        status='notneeded',
                        message=("dataset %s was already cloned from '%s'",
                                 destds,
                                 src),
                        **result_props)
                    return
        # anything else is an error
        yield get_status_dict(
            status='error',
            message='target path already exists and not empty, refuse to clone into target path',
            **result_props)
        return

    log_progress(
        lgr.info,
        'cloneds',
        'Cloning dataset to %s', destds,
        total=len(candidate_sources),
        label='Clone attempt',
        unit=' Candidate locations',
    )
    error_msgs = OrderedDict()  # accumulate all error messages formatted per each url
    for cand in candidate_sources:
        log_progress(
            lgr.info,
            'cloneds',
            'Attempting to clone from %s to %s', cand['giturl'], dest_path,
            update=1,
            increment=True)

        clone_opts = {}

        if cand.get('version', None):
            clone_opts['branch'] = cand['version']
        try:
            # TODO for now GitRepo.clone() cannot handle Path instances, and PY35
            # doesn't make it happen seemlessly
            GitRepo.clone(
                path=str(dest_path),
                url=cand['giturl'],
                clone_options=clone_opts,
                create=True)

        except CommandError as e:
            e_stderr = e.stderr

            error_msgs[cand['giturl']] = e
            lgr.debug("Failed to clone from URL: %s (%s)",
                      cand['giturl'], exc_str(e))
            if dest_path.exists():
                lgr.debug("Wiping out unsuccessful clone attempt at: %s",
                          dest_path)
                # We must not just rmtree since it might be curdir etc
                # we should remove all files/directories under it
                # TODO stringification can be removed once patlib compatible
                # or if PY35 is no longer supported
                rmtree(str(dest_path), children_only=dest_path_existed)

            if 'could not create work tree' in e_stderr.lower():
                # this cannot be fixed by trying another URL
                re_match = re.match(r".*fatal: (.*)$", e_stderr,
                                    flags=re.MULTILINE | re.DOTALL)
                # cancel progress bar
                log_progress(
                    lgr.info,
                    'cloneds',
                    'Completed clone attempts for %s', destds
                )
                yield get_status_dict(
                    status='error',
                    message=re_match.group(1).strip()
                    if re_match else "stderr: " + e_stderr,
                    **result_props)
                return
            # next candidate
            continue

        result_props['source'] = cand
        # do not bother with other sources if succeeded
        break

    log_progress(
        lgr.info,
        'cloneds',
        'Completed clone attempts for %s', destds
    )

    if not destds.is_installed():
        if len(error_msgs):
            if all(not e.stdout and not e.stderr for e in error_msgs.values()):
                # there is nothing we can learn from the actual exception,
                # the exit code is uninformative, the command is predictable
                error_msg = "Failed to clone from all attempted sources: %s"
                error_args = list(error_msgs.keys())
            else:
                error_msg = "Failed to clone from any candidate source URL. " \
                            "Encountered errors per each url were:\n- %s"
                error_args = '\n- '.join(
                    '{}\n  {}'.format(url, exc_str(exc))
                    for url, exc in error_msgs.items()
                )
        else:
            # yoh: Not sure if we ever get here but I felt that there could
            #      be a case when this might happen and original error would
            #      not be sufficient to troubleshoot what is going on.
            error_msg = "Awkward error -- we failed to clone properly. " \
                        "Although no errors were encountered, target " \
                        "dataset at %s seems to be not fully installed. " \
                        "The 'succesful' source was: %s"
            error_args = (destds.path, cand['giturl'])
        yield get_status_dict(
            status='error',
            message=(error_msg, error_args),
            **result_props)
        return

    if not cand.get("version"):
        postclone_check_head(destds)

    # act on --reckless=shared-...
    # must happen prior git-annex-init, where we can cheaply alter the repo
    # setup through safe re-init'ing
    if reckless and reckless.startswith('shared-'):
        lgr.debug('Reinit %s to enable shared access permissions', destds)
        destds.repo.call_git(['init', '--shared={}'.format(reckless[7:])])

    yield from postclonecfg_annexdataset(
        destds,
        reckless,
        description)

    # perform any post-processing that needs to know details of the clone
    # source
    if result_props['source']['type'] == 'ria':
        yield from postclonecfg_ria(destds, result_props['source'])

    # yield successful clone of the base dataset now, as any possible
    # subdataset clone down below will not alter the Git-state of the
    # parent
    yield get_status_dict(status='ok', **result_props)


def postclone_check_head(ds):
    repo = ds.repo
    if not repo.commit_exists("HEAD"):
        # HEAD points to an unborn branch. A likely cause of this is that the
        # remote's main branch is something other than master but HEAD wasn't
        # adjusted accordingly.
        #
        # Let's choose the most recently updated remote ref (according to
        # commit date). In the case of a submodule, switching to a ref with
        # commits prevents .update_submodule() from failing. It is likely that
        # the ref includes the registered commit, but we don't have the
        # information here to know for sure. If it doesn't, .update_submodule()
        # will check out a detached HEAD.
        remote_branches = (
            b["refname:strip=2"] for b in repo.for_each_ref_(
                fields="refname:strip=2", sort="-committerdate",
                pattern="refs/remotes/origin"))
        for rbranch in remote_branches:
            if rbranch in ["origin/git-annex", "HEAD"]:
                continue
            repo.call_git(["checkout", "-b", rbranch[7:],  # drop "origin/"
                           "--track", rbranch])
            lgr.debug("Checked out local branch from %s", rbranch)
            return
        lgr.warning("Cloned %s but could not find a branch "
                    "with commits", ds.path)


def postclonecfg_ria(ds, props):
    """Configure a dataset freshly cloned from a RIA store"""
    repo = ds.repo
    # RIA uses hashdir mixed, copying data to it via git-annex (if cloned via
    # ssh) would make it see a bare repo and establish a hashdir lower annex
    # object tree.
    # Moreover, we want the ORA remote to receive all data for the store, so its
    # objects could be moved into archives (the main point of a RIA store).
    RIA_REMOTE_NAME = 'origin'  # don't hardcode everywhere
    ds.config.set(
        'remote.{}.annex-ignore'.format(RIA_REMOTE_NAME), 'true',
        where='local')

    # chances are that if this dataset came from a RIA store, its subdatasets
    # may live there too. Place a subdataset source candidate config that makes
    # get probe this RIA store when obtaining subdatasets
    ds.config.set(
        # we use the label 'origin' for this candidate in order to not have to
        # generate a complicated name from the actual source specification.
        # we pick a priority of 20 to sort it before datalad's default candidates
        # for non-RIA URLs, because they prioritize hierarchical layouts that
        # cannot be found in a RIA store
        'datalad.get.subdataset-source-candidate-200origin',
        # use the entire original URL, up to the fragment + plus dataset ID
        # placeholder, this should make things work with any store setup we
        # support (paths, ports, ...)
        props['source'].split('#', maxsplit=1)[0] + '#{id}',
        where='local')

    # setup publication dependency, if a corresponding special remote exists
    # and was enabled (there could be RIA stores that actually only have repos)
    # make this function be a generator
    ora_remotes = [s for s in ds.siblings('query', result_renderer='disabled')
                   if s.get('annex-externaltype') == 'ora']
    if not ora_remotes and any(
            r.get('externaltype') == 'ora'
            for r in (repo.get_special_remotes().values()
                      if hasattr(repo, 'get_special_remotes')
                      else [])):
        # no ORA remote autoenabled, but configuration known about at least one.
        # Let's check origin's config for datalad.ora-remote.uuid as stored by
        # create-sibling-ria and enable try enabling that one.
        lgr.debug("Found no autoenabled ORA special remote. Trying to look it "
                  "up in source config ...")

        # First figure whether we cloned via SSH, HTTP or local path and then
        # get that config file the same way:
        config_content = None
        scheme = props['giturl'].split(':', 1)[0]
        if scheme == 'http':

            try:
                response = requests.get("{}{}config".format(
                    props['giturl'],
                    '/' if not props['giturl'].endswith('/') else '')
                )
                config_content = response.text
            except requests.RequestException as e:
                lgr.debug("Failed to get config file from source:\n%s",
                          exc_str(e))

        elif scheme == 'ssh':
            # TODO: switch the following to proper command abstraction:
            # SSHRemoteIO ignores the path part ATM. No remote CWD! (To be
            # changed with command abstractions). So we need to get that part to
            # have a valid path to origin's config file:
            cfg_path = PurePosixPath(URL(props['giturl']).path) / 'config'
            op = SSHRemoteIO(props['giturl'])
            try:
                config_content = op.read_file(cfg_path)
            except RIARemoteError as e:
                lgr.debug("Failed to get config file from source: %s",
                          exc_str(e))

        elif scheme == 'file':
            # TODO: switch the following to proper command abstraction:
            op = LocalIO()
            cfg_path = Path(URL(props['giturl']).localpath) / 'config'
            try:
                config_content = op.read_file(cfg_path)
            except (RIARemoteError, OSError) as e:
                lgr.debug("Failed to get config file from source: %s",
                          exc_str(e))
        else:
            lgr.debug("Unknown URL-Scheme in %s. Can handle SSH, HTTP or "
                      "FILE scheme URLs.", props['source'])

        # 3. And read it
        org_uuid = None
        if config_content:
            # TODO: We might be able to spare the saving to a file.
            #       "git config -f -" is not explicitly documented but happens
            #       to work and would read from stdin. Make sure we know this
            #       works for required git versions and on all platforms.
            with make_tempfile(content=config_content) as cfg_file:
                runner = WitlessRunner(env=GitRunner.get_git_environ_adjusted())
                try:
                    result = runner.run(
                        ['git', 'config', '-f', cfg_file,
                         'datalad.ora-remote.uuid'],
                        protocol=StdOutCapture
                    )
                    org_uuid = result['stdout'].strip()
                except CommandError as e:
                    # doesn't contain what we are looking for
                    lgr.debug("Found no UUID for ORA special remote at "
                              "'%s' (%s)", RIA_REMOTE_NAME, exc_str(e))

        # Now, enable it. If annex-init didn't fail to enable it as stored, we
        # wouldn't end up here, so enable with store URL as suggested by the URL
        # we cloned from.
        if org_uuid:
            srs = repo.get_special_remotes()
            if org_uuid in srs.keys():
                # TODO: - Double-check autoenable value and only do this when
                #         true?
                #       - What if still fails? -> Annex shouldn't change config
                #         in that case

                # we only need the store:
                new_url = props['source'].split('#')[0]
                try:
                    repo.enable_remote(srs[org_uuid]['name'],
                                       options=['url={}'.format(new_url)]
                                       )
                    lgr.info("Reconfigured %s for %s",
                             srs[org_uuid]['name'], new_url)
                    # update ora_remotes for considering publication dependency
                    # below
                    ora_remotes = [s for s in
                                   ds.siblings('query',
                                               result_renderer='disabled')
                                   if s.get('annex-externaltype', None) ==
                                   'ora']
                except CommandError as e:
                    lgr.debug("Failed to reconfigure ORA special remote: %s",
                              exc_str(e))
            else:
                lgr.debug("Unknown ORA special remote uuid at '%s': %s",
                          RIA_REMOTE_NAME, org_uuid)
    if ora_remotes:
        if len(ora_remotes) == 1:
            yield from ds.siblings('configure',
                                   name=RIA_REMOTE_NAME,
                                   publish_depends=ora_remotes[0]['name'],
                                   result_filter=None,
                                   result_renderer='disabled')
        else:
            lgr.warning("Found multiple ORA remotes. Couldn't decide which "
                        "publishing to 'origin' should depend on: %s. Consider "
                        "running 'datalad siblings configure -s origin "
                        "--publish-depends ORAREMOTENAME' to set publication "
                        "dependency manually.",
                        [r['name'] for r in ora_remotes])


def postclonecfg_annexdataset(ds, reckless, description=None):
    """If ds "knows annex" -- annex init it, set into reckless etc

    Provides additional tune up to a possibly an annex repo, e.g.
    "enables" reckless mode, sets up description
    """
    # in any case check whether we need to annex-init the installed thing:
    if not knows_annex(ds.path):
        # not for us
        return

    # init annex when traces of a remote annex can be detected
    if reckless == 'auto':
        lgr.debug(
            "Instruct annex to hardlink content in %s from local "
            "sources, if possible (reckless)", ds.path)
        ds.config.set(
            'annex.hardlink', 'true', where='local', reload=True)

    lgr.debug("Initializing annex repo at %s", ds.path)
    # Note, that we cannot enforce annex-init via AnnexRepo().
    # If such an instance already exists, its __init__ will not be executed.
    # Therefore do quick test once we have an object and decide whether to call
    # its _init().
    #
    # Additionally, call init if we need to add a description (see #1403),
    # since AnnexRepo.__init__ can only do it with create=True
    repo = AnnexRepo(ds.path, init=True)
    if not repo.is_initialized() or description:
        repo._init(description=description)
    if reckless == 'auto' or (reckless and reckless.startswith('shared-')):
        repo._run_annex_command('untrust', annex_options=['here'])

    elif reckless == 'ephemeral':
        # with ephemeral we declare 'here' as 'dead' right away, whenever
        # we symlink origin's annex, since availability from 'here' should
        # not be propagated for an ephemeral clone when we publish back to
        # origin.
        # This will cause stuff like this for a locally present annexed file:
        # % git annex whereis d1
        # whereis d1 (0 copies) failed
        # BUT this works:
        # % git annex find . --not --in here
        # % git annex find . --in here
        # d1

        # we don't want annex copy-to origin
        ds.config.set(
            'remote.origin.annex-ignore', 'true',
            where='local')

        ds.repo.set_remote_dead('here')

        if check_symlink_capability(ds.repo.dot_git / 'dl_link_test',
                                    ds.repo.dot_git / 'dl_target_test'):
            # symlink the annex to avoid needless copies in an emphemeral clone
            annex_dir = ds.repo.dot_git / 'annex'
            origin_annex_url = ds.config.get("remote.origin.url", None)
            origin_git_path = None
            if origin_annex_url:
                try:
                    # Deal with file:// scheme URLs as well as plain paths.
                    # If origin isn't local, we have nothing to do.
                    origin_git_path = Path(RI(origin_annex_url).localpath)
                    if origin_git_path.name != '.git':
                        origin_git_path /= '.git'
                except ValueError:
                    # Note, that accessing localpath on a non-local RI throws
                    # ValueError rather than resulting in an AttributeError.
                    # TODO: Warning level okay or is info level sufficient?
                    # Note, that setting annex-dead is independent of
                    # symlinking .git/annex. It might still make sense to
                    # have an ephemeral clone that doesn't propagate its avail.
                    # info. Therefore don't fail altogether.
                    lgr.warning("reckless=ephemeral mode: origin doesn't seem "
                                "local: %s\nno symlinks being used",
                                origin_annex_url)
            if origin_git_path:
                # TODO make sure that we do not delete any unique data
                rmtree(str(annex_dir)) \
                    if not annex_dir.is_symlink() else annex_dir.unlink()
                annex_dir.symlink_to(origin_git_path / 'annex',
                                     target_is_directory=True)
        else:
            # TODO: What level? + note, that annex-dead is independ
            lgr.warning("reckless=ephemeral mode: Unable to create symlinks on "
                        "this file system.")

    if reckless:
        # we successfully dealt with reckless here.
        # store the reckless setting in the dataset to make it
        # known to later clones of subdatasets via get()
        ds.config.set(
            'datalad.clone.reckless', reckless,
            where='local',
            reload=True)

    srs = {True: [], False: []}  # special remotes by "autoenable" key
    remote_uuids = None  # might be necessary to discover known UUIDs

    # Note: The purpose of this function is to inform the user. So if something
    # looks misconfigured, we'll warn and move on to the next item.
    for uuid, config in repo.get_special_remotes().items():
        sr_name = config.get('name', None)
        if sr_name is None:
            lgr.warning(
                'Ignoring special remote %s because it does not have a name. '
                'Known information: %s',
                uuid, config)
            continue
        sr_autoenable = config.get('autoenable', False)
        try:
            sr_autoenable = assure_bool(sr_autoenable)
        except ValueError:
            lgr.warning(
                'Failed to process "autoenable" value %r for sibling %s in '
                'dataset %s as bool.'
                'You might need to enable it later manually and/or fix it up to'
                ' avoid this message in the future.',
                sr_autoenable, sr_name, ds.path)
            continue

        # determine whether there is a registered remote with matching UUID
        if uuid:
            if remote_uuids is None:
                remote_uuids = {
                    # Check annex-config-uuid first. For sameas annex remotes,
                    # this will point to the UUID for the configuration (i.e.
                    # the key returned by get_special_remotes) rather than the
                    # shared UUID.
                    (repo.config.get('remote.%s.annex-config-uuid' % r) or
                     repo.config.get('remote.%s.annex-uuid' % r))
                    for r in repo.get_remotes()
                }
            if uuid not in remote_uuids:
                srs[sr_autoenable].append(sr_name)

    if srs[True]:
        lgr.debug(
            "configuration for %s %s added because of autoenable,"
            " but no UUIDs for them yet known for dataset %s",
            # since we are only at debug level, we could call things their
            # proper names
            single_or_plural("special remote",
                             "special remotes", len(srs[True]), True),
            ", ".join(srs[True]),
            ds.path
        )

    if srs[False]:
        # if has no auto-enable special remotes
        lgr.info(
            'access to %s %s not auto-enabled, enable with:\n'
            '\t\tdatalad siblings -d "%s" enable -s %s',
            # but since humans might read it, we better confuse them with our
            # own terms!
            single_or_plural("dataset sibling",
                             "dataset siblings", len(srs[False]), True),
            ", ".join(srs[False]),
            ds.path,
            srs[False][0] if len(srs[False]) == 1 else "SIBLING",
        )

    # we have just cloned the repo, so it has 'origin', configure any
    # reachable origin of origins
    yield from configure_origins(ds, ds)


_handle_possible_annex_dataset = postclonecfg_annexdataset


def configure_origins(cfgds, probeds, label=None):
    """Configure any discoverable local dataset 'origin' sibling as a remote

    Parameters
    ----------
    cfgds : Dataset
      Dataset to receive the remote configurations
    probeds : Dataset
      Dataset to start looking for 'origin' remotes. May be identical with
      `cfgds`.
    label : int, optional
      Each discovered 'origin' will be configured as a remote under the name
      'origin-<label>'. If no label is given, '2' will be used by default,
      given that there is typically a 'origin' remote already.
    """
    if label is None:
        label = 1
    # let's look at the URL for that remote and see if it is a local
    # dataset
    origin_url = probeds.config.get('remote.origin.url')
    if not origin_url:
        # no origin, nothing to do
        return
    if not cfgds.config.obtain(
            'datalad.install.inherit-local-origin',
            default=True):
        # no inheritance wanted
        return
    if not isinstance(RI(origin_url), PathRI):
        # not local path
        return

    # no need to reconfigure original/direct origin again
    if cfgds != probeds:
        # prevent duplicates
        known_remote_urls = set(
            cfgds.config.get(r + '.url', None)
            for r in cfgds.config.sections()
            if r.startswith('remote.')
        )
        if origin_url not in known_remote_urls:
            yield from cfgds.siblings(
                'configure',
                # no chance for conflict, can only be the second configured
                # remote
                name='origin-{}'.format(label),
                url=origin_url,
                # fetch to get all annex info
                fetch=True,
                result_renderer='disabled',
                on_failure='ignore',
            )
    # and dive deeper
    # given the clone source is a local dataset, we can have a
    # cheap look at it, and configure its own 'origin' as a remote
    # (if there is any), and benefit from additional annex availability
    yield from configure_origins(
        cfgds,
        Dataset(probeds.pathobj / origin_url),
        label=label + 1)


def _get_tracking_source(ds):
    """Returns name and url of a potential configured source
    tracking remote"""
    vcs = ds.repo
    # if we have a remote, let's check the location of that remote
    # for the presence of the desired submodule

    remote_name, tracking_branch = vcs.get_tracking_branch()
    if not remote_name and isinstance(vcs, AnnexRepo):
        # maybe cloned from a source repo that was in adjusted mode
        # https://github.com/datalad/datalad/issues/3969
        remote_name, tracking_branch = vcs.get_tracking_branch(
            corresponding=False)
    # TODO: better default `None`? Check where we might rely on '':
    remote_url = ''
    if remote_name:
        remote_url = vcs.get_remote_url(remote_name, push=False)

    return remote_name, remote_url


def _get_installationpath_from_url(url):
    """Returns a relative path derived from the trailing end of a URL

    This can be used to determine an installation path of a Dataset
    from a URL, analog to what `git clone` does.
    """
    ri = RI(url)
    if isinstance(ri, (URL, DataLadRI)):  # decode only if URL
        path = ri.path.rstrip('/')
        path = urlunquote(path) if path else ri.hostname
        if '/' in path:
            path = path.split('/')
            if path[-1] == '.git':
                path = path[-2]
            else:
                path = path[-1]
    else:
        path = Path(url).parts[-1]
    if path.endswith('.git'):
        path = path[:-4]
    return path


def decode_source_spec(spec, cfg=None):
    """Decode information from a clone source specification

    Parameters
    ----------
    spec : str
      Any supported clone source specification
    cfg : ConfigManager, optional
      Configuration will be queried from the instance (i.e. from a particular
      dataset). If None is given, the global DataLad configuration will be
      queried.

    Returns
    -------
    dict
      The value of each decoded property is stored under its own key in this
      dict. By default the following keys are return: 'type', a specification
      type label {'giturl', 'dataladri', 'ria'}; 'source' the original
      source specification; 'giturl' a URL for the source that is a suitable
      source argument for git-clone; 'version' a version-identifer, if present
      (None else); 'default_destpath' a relative path that that can be used as
      a clone destination.
    """
    if cfg is None:
        from datalad import cfg
    # standard property dict composition
    props = dict(
        source=spec,
        version=None,
    )

    # Git never gets to see these URLs, so let's manually apply any
    # rewrite configuration Git might know about.
    # Note: We need to rewrite before parsing, otherwise parsing might go wrong.
    # This is particularly true for insteadOf labels replacing even the URL
    # scheme.
    spec = cfg.rewrite_url(spec)
    # common starting point is a RI instance, support for accepting an RI
    # instance is kept for backward-compatibility reasons
    source_ri = RI(spec) if not isinstance(spec, RI) else spec

    # scenario switch, each case must set 'giturl' at the very minimum
    if isinstance(source_ri, DataLadRI):
        # we have got our DataLadRI as the source, so expand it
        props['type'] = 'dataladri'
        props['giturl'] = source_ri.as_git_url()
    elif isinstance(source_ri, URL) and source_ri.scheme.startswith('ria+'):
        # parse a RIA URI
        dsid, version = source_ri.fragment.split('@', maxsplit=1) \
            if '@' in source_ri.fragment else (source_ri.fragment, None)
        uuid_regex = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
        if re.match(uuid_regex, dsid):
            trace = '{}/{}'.format(dsid[:3], dsid[3:])
            default_destpath = dsid
        elif dsid.startswith('~'):
            trace = 'alias/{}'.format(dsid[1:])
            default_destpath = dsid[1:]
        else:
            raise ValueError(
                'RIA URI not recognized, no valid dataset ID or other supported '
                'scheme: {}'.format(spec))
        # now we cancel the fragment in the original URL, but keep everthing else
        # in order to be able to support the various combinations of ports, paths,
        # and everything else
        source_ri.fragment = ''
        # strip the custom protocol and go with standard one
        source_ri.scheme = source_ri.scheme[4:]
        # take any existing path, and add trace to dataset within the store
        source_ri.path = '{urlpath}{urldelim}{trace}'.format(
            urlpath=source_ri.path if source_ri.path else '',
            urldelim='' if not source_ri.path or source_ri.path.endswith('/') else '/',
            trace=trace,
        )
        props.update(
            type='ria',
            giturl=str(source_ri),
            version=version,
            default_destpath=default_destpath,
        )
    else:
        # let's assume that anything else is a URI that Git can handle
        props['type'] = 'giturl'
        # use original input verbatim
        props['giturl'] = spec

    if 'default_destpath' not in props:
        # if we still have no good idea on where a dataset could be cloned to if no
        # path was given, do something similar to git clone and derive the path from
        # the source
        props['default_destpath'] = _get_installationpath_from_url(props['giturl'])

    return props
