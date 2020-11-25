# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Create and update a dataset from a list of URLs.
"""

from collections.abc import Mapping

import logging
import os
import re
import string
import sys

from functools import partial
from urllib.parse import urlparse

import datalad.support.path as op
from datalad.distribution.dataset import resolve_path
from datalad.dochelpers import exc_str
from datalad.log import log_progress, with_result_progress
from datalad.interface.base import Interface
from datalad.interface.base import build_doc
from datalad.interface.results import annexjson2result, get_status_dict
from datalad.interface.common_opts import (
    jobs_opt,
    nosave_opt,
)
from datalad.support.exceptions import CommandError
from datalad.support.itertools import groupby_sorted
from datalad.support.network import get_url_filename
from datalad.support.path import split_ext
from datalad.support.parallel import (
    ProducerConsumerProgressLog,
    no_parentds_in_futures,
)
from datalad.support.s3 import get_versioned_url
from datalad.utils import (
    ensure_list,
    get_suggestions_msg,
    unlink,
)

lgr = logging.getLogger("datalad.plugin.addurls")

__docformat__ = "restructuredtext"


class Formatter(string.Formatter):
    """Formatter that gives precedence to custom keys.

    The first positional argument to the `format` call should be a
    mapping whose keys are exposed as placeholders (e.g.,
    "{key1}.py").

    Parameters
    ----------
    idx_to_name : dict
        A mapping from a positional index to a key.  If not provided,
        "{N}" elements are not supported.
    missing : str, optional
        When column lookup results in an empty string, use this value in
        its place.
    """

    def __init__(self, idx_to_name=None, missing_value=None):
        self.idx_to_name = idx_to_name or {}
        self.missing = missing_value

    def format(self, format_string, *args, **kwargs):
        if not isinstance(args[0], Mapping):
            raise ValueError("First positional argument should be mapping")
        return super(Formatter, self).format(format_string, *args, **kwargs)

    def get_value(self, key, args, kwargs):
        """Look for key's value in `args[0]` mapping first.
        """
        # FIXME: This approach will fail for keys that contain "!" and
        # ":" because they'll be interpreted as formatting flags.
        data = args[0]

        name = key
        try:
            key_int = int(key)
        except ValueError:
            pass
        else:
            name = self.idx_to_name[key_int]

        try:
            value = data[name]
        except KeyError:
            return super(Formatter, self).get_value(
                key, args, kwargs)

        if self.missing is not None and isinstance(value, str):
            return value or self.missing
        return value

    def convert_field(self, value, conversion):
        if conversion == 'l':
            return str(value).lower()
        return super(Formatter, self).convert_field(value, conversion)


class RepFormatter(Formatter):
    """Extend Formatter to support a {_repindex} placeholder.
    """

    def __init__(self, *args, **kwargs):
        super(RepFormatter, self).__init__(*args, **kwargs)
        self.repeats = {}
        self.repindex = 0

    def format(self, *args, **kwargs):
        self.repindex = 0
        result = super(RepFormatter, self).format(*args, **kwargs)
        if result in self.repeats:
            self.repindex = self.repeats[result] + 1
            self.repeats[result] = self.repindex
            result = super(RepFormatter, self).format(*args, **kwargs)
        else:
            self.repeats[result] = 0
        return result

    def get_value(self, key, args, kwargs):
        args[0]["_repindex"] = self.repindex
        return super(RepFormatter, self).get_value(key, args, kwargs)


def clean_meta_args(args):
    """Process metadata arguments.

    Parameters
    ----------
    args : iterable of str
        Formatted metadata arguments for 'git-annex metadata --set'.

    Returns
    -------
    A dict mapping field names to values.
    """
    results = {}
    for arg in args:
        parts = [x.strip() for x in arg.split("=", 1)]
        if len(parts) == 2:
            if not parts[0]:
                raise ValueError("Empty field name")
            field, value = parts
        else:
            raise ValueError("meta argument isn't in 'field=value' format")

        if not value:
            # The `url_file` may have an empty value.
            continue
        results[field] = value
    return results


def get_subpaths(filename):
    """Convert "//" marker in `filename` to a list of subpaths.

    >>> from datalad.plugin.addurls import get_subpaths
    >>> get_subpaths("p1/p2//p3/p4//file")
    ('p1/p2/p3/p4/file', ['p1/p2', 'p1/p2/p3/p4'])

    Note: With Python 3, the subpaths could be generated with

        itertools.accumulate(filename.split("//")[:-1], os.path.join)

    Parameters
    ----------
    filename : str
        File name with "//" marking subpaths.

    Returns
    -------
    A tuple of the filename with any "//" collapsed to a single
    separator and a list of subpaths (str).
    """
    if "//" not in filename:
        return filename, []

    spaths = []
    for part in filename.split("//")[:-1]:
        path = os.path.join(spaths[-1], part) if spaths else part
        spaths.append(path)
    return filename.replace("//", os.path.sep), spaths


def is_legal_metafield(name):
    """Test whether `name` is a valid metadata field.

    The set of permitted characters is taken from git-annex's
    MetaData.hs:legalField.
    """
    return bool(re.match(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z", name))


def filter_legal_metafield(fields):
    """Remove illegal names from `fields`.

    Note: This is like `filter(is_legal_metafield, fields)` but the
    dropped values are logged.
    """
    legal = []
    for field in fields:
        if is_legal_metafield(field):
            legal.append(field)
        else:
            lgr.debug("%s is not a valid metadata field name; dropping",
                      field)
    return legal


class AnnexKeyParser(object):
    """Parse a full annex key into subparts.

    See <https://git-annex.branchable.com/internals/key_format/>.

    Parameters
    ----------
    format_fn : callable
        Function that takes a format string and a row and returns the full key.
    format_string : str
        Format string for the full key.
    """

    def __init__(self, format_fn, format_string):
        self.format_fn = format_fn
        self.format_string = format_string
        self.regexp = re.compile(r"(?P<backend>[A-Z0-9]+)"
                                 r"(?:-[^-]+)?"
                                 r"--(?P<keyname>[^/\n]+)\Z")
        self.empty = "".join(i[0]
                             for i in string.Formatter().parse(format_string))

    def parse(self, row):
        """Format the key with the fields in `row` and parse it.

        Returns
        -------
        A dictionary with the following keys that match their counterparts in
        the output of `git annex examinekey --json`: "key" (the full annex
        key), "backend", and "keyname".

        Raises
        ------
        ValueError if the formatted value doesn't look like a valid key
        """
        try:
            key = self.format_fn(self.format_string, row)
        except KeyError as exc:
            lgr.debug("Row missing fields for --key: %s",
                      exc_str(exc))
            return {}

        if key == self.empty:
            lgr.debug("All fields in --key's value are empty in row: %s", row)
            # We got the same string that'd we get if all the fields were
            # empty, so this doesn't have a key.
            return {}

        match = self.regexp.match(key)
        if match:
            info = match.groupdict()
            info["key"] = key
            return info
        else:
            raise ValueError(
                "Key does not match expected "
                "<backend>[-[CmsS]NNN]--<key> format: {}"
                .format(key))


def get_fmt_names(format_string):
    """Yield field names in `format_string`.
    """
    for _, name, _, _ in string.Formatter().parse(format_string):
        if name:
            yield name


def fmt_to_name(format_string, num_to_name):
    """Try to map a format string to a single name.

    Parameters
    ----------
    format_string : string
    num_to_name : dict
        A dictionary that maps from an integer to a column name.  This
        enables mapping the format string to an integer to a name.

    Returns
    -------
    A placeholder name if `format_string` consists of a single
    placeholder and no other text.  Otherwise, None is returned.
    """
    parsed = list(string.Formatter().parse(format_string))
    if len(parsed) != 1:
        # It's an empty string or there's more than one placeholder.
        return
    if parsed[0][0]:
        # Format string contains text before the placeholder.
        return

    name = parsed[0][1]
    if not name:
        # The field name is empty.
        return

    try:
        return num_to_name[int(name)]
    except (KeyError, ValueError):
        return name


INPUT_TYPES = ["ext", "csv", "tsv", "json"]


def _read(stream, input_type):
    if input_type in ["csv", "tsv"]:
        import csv
        csvrows = csv.reader(stream,
                             delimiter="\t" if input_type == "tsv" else ",")
        try:
            headers = next(csvrows)
        except StopIteration:
            raise ValueError("Failed to read {} rows from {}"
                             .format(input_type.upper(), stream))
        lgr.debug("Taking %s fields from first line as headers: %s",
                  len(headers), headers)
        idx_map = dict(enumerate(headers))
        rows = [dict(zip(headers, r)) for r in csvrows]
    elif input_type == "json":
        import json
        try:
            rows = json.load(stream)
        except json.decoder.JSONDecodeError as e:
            raise ValueError(
                "Failed to read JSON from stream {}: {}"
                .format(stream, exc_str(e)))
        # For json input, we do not support indexing by position,
        # only names.
        idx_map = {}
    else:
        raise ValueError(
            "input_type {} is invalid. Known values: {}"
            .format(input_type, ", ".join(INPUT_TYPES)))
    return rows, idx_map


def _get_placeholder_exception(exc, msg_prefix, known):
    """Recast KeyError as a ValueError with close-match suggestions.
    """
    value = exc.args[0]
    if isinstance(value, str):
        sugmsg = get_suggestions_msg(value, known)
    else:
        sugmsg = "Out-of-bounds or unsupported index."
    # Note: Keeping this a KeyError is probably more appropriate but then the
    # entire message, which KeyError takes as the key, will be rendered with
    # outer quotes.
    return ValueError("{}: {}{}{}"
                      .format(msg_prefix, exc, ". " if sugmsg else "", sugmsg))


def _format_filenames(format_fn, rows, row_infos):
    subpaths = set()
    for row, info in zip(rows, row_infos):
        try:
            filename = format_fn(row)
        except KeyError as exc:
            raise _get_placeholder_exception(
                exc, "Unknown placeholder in file name", row)
        filename, spaths = get_subpaths(filename)
        subpaths |= set(spaths)
        info["filename"] = filename
        info["subpath"] = spaths[-1] if spaths else None
    return subpaths


def get_file_parts(filename, prefix="name"):
    """Assign a name to various parts of a file.

    Parameters
    ----------
    filename : str
        A file name (no leading path is permitted).
    prefix : str
        Prefix to prepend to the key names.

    Returns
    -------
    A dict mapping each part to a value.
    """
    root, ext = split_ext(filename)
    root_py, ext_py = os.path.splitext(filename)

    return {prefix: filename,
            prefix + "_root": root,
            prefix + "_ext": ext,
            prefix + "_root_py": root_py,
            prefix + "_ext_py": ext_py}


def get_url_parts(url):
    """Assign a name to various parts of the URL.

    Parameters
    ----------
    url : str

    Returns
    -------
    A dict with keys `_url_hostname` and, for a path with N+1 parts,
    '_url0' through '_urlN' .  There is also a `_url_basename` key for
    the rightmost part of the path.
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        return {}

    names = {"_url_hostname": parsed.netloc}

    path = parsed.path.strip("/")
    if not path:
        return names

    url_parts = path.split("/")
    for pidx, part in enumerate(url_parts):
        names["_url{}".format(pidx)] = part
    basename = url_parts[-1]
    names["_url_basename"] = basename
    names.update(get_file_parts(basename, prefix="_url_basename"))
    return names


def add_extra_filename_values(filename_format, rows, urls, dry_run):
    """Extend `rows` with values for special formatting fields.
    """
    file_fields = list(get_fmt_names(filename_format))
    if any(i.startswith("_url") for i in file_fields):
        for row, url in zip(rows, urls):
            row.update(get_url_parts(url))

    if any(i.startswith("_url_filename") for i in file_fields):
        if dry_run:  # Don't waste time making requests.
            dummy = get_file_parts("BASE.EXT", "_url_filename")
            for idx, row in enumerate(rows):
                row.update(
                    {k: v + str(idx) for k, v in dummy.items()})
        else:
            num_urls = len(urls)
            log_progress(lgr.info, "addurls_requestnames",
                         "Requesting file names for %d URLs", num_urls,
                         label="Requesting names", total=num_urls,
                         unit=" Files")
            for row, url in zip(rows, urls):
                # If we run into any issues here, we're just going to raise an
                # exception and then abort inside dlplugin.  It'd be good to
                # disentangle this from `extract` so that we could yield an
                # individual error, drop the row, and keep going.
                filename = get_url_filename(url)
                if filename:
                    row.update(get_file_parts(filename, "_url_filename"))
                else:
                    raise ValueError(
                        "{} does not contain a filename".format(url))
                log_progress(lgr.info, "addurls_requestnames",
                             "%s returned for %s", url, filename,
                             update=1, increment=True)
            log_progress(lgr.info, "addurls_requestnames",
                         "Finished requesting file names")


def sort_paths(paths):
    """Sort `paths` by directory level and then alphabetically.

    Parameters
    ----------
    paths : iterable of str

    Returns
    -------
    Generator of sorted paths.
    """
    def level_and_name(p):
        return p.count(os.path.sep), p

    yield from sorted(paths, key=level_and_name)


def extract(stream, input_type, url_format="{0}", filename_format="{1}",
            exclude_autometa=None, meta=None, key=None,
            dry_run=False, missing_value=None):
    """Extract and format information from `url_file`.

    Parameters
    ----------
    stream : file object
        Items used to construct the file names and URLs.
    input_type : {'csv', 'tsv', 'json'}

    All other parameters match those described in `AddUrls`.

    Returns
    -------
    A tuple where the first item is a list with a dict of extracted information
    for each row in `stream` and the second item a list subdataset paths,
    sorted breadth-first.
    """
    meta = ensure_list(meta)

    rows, colidx_to_name = _read(stream, input_type)
    if not rows:
        lgr.warning("No rows found in %s", stream)
        return [], []

    # Formatter for everything but file names
    fmt = Formatter(colidx_to_name, missing_value)
    format_url = partial(fmt.format, url_format)

    auto_meta_args = []
    if exclude_autometa not in ["*", ""]:
        urlcol = fmt_to_name(url_format, colidx_to_name)
        # TODO: Try to normalize invalid fields, checking for any
        # collisions.
        metacols = (c for c in sorted(rows[0].keys()) if c != urlcol)
        if exclude_autometa:
            metacols = (c for c in metacols
                        if not re.search(exclude_autometa, c))
        metacols = filter_legal_metafield(metacols)
        auto_meta_args = [c + "=" + "{" + c + "}" for c in metacols]

    # Unlike `filename_format` and `url_format`, `meta` is a list
    # because meta may be given multiple times on the command line.
    formats_meta = [partial(fmt.format, m) for m in meta + auto_meta_args]

    info_fns = []
    if formats_meta:
        def set_meta_args(info, row):
            info["meta_args"] = clean_meta_args(fmt(row)
                                                for fmt in formats_meta)
        info_fns.append(set_meta_args)
    if key:
        key_parser = AnnexKeyParser(fmt.format, key)

        def set_key(info, row):
            info["key"] = key_parser.parse(row)
        info_fns.append(set_key)

    rows_with_url = []
    infos = []
    for row in rows:
        try:
            url = format_url(row)
        except KeyError as exc:
            raise _get_placeholder_exception(
                exc, "Unknown placeholder in URL", row)
        if not url or url == missing_value:
            continue  # pragma: no cover, peephole optimization
        rows_with_url.append(row)
        info = {"url": url}
        for fn in info_fns:
            fn(info, row)
        infos.append(info)

    n_dropped = len(rows) - len(rows_with_url)
    if n_dropped:
        lgr.warning("Dropped %d row(s) that had an empty URL", n_dropped)

    # Format the filename in a second pass so that we can provide
    # information about the formatted URLs.
    add_extra_filename_values(filename_format, rows_with_url,
                              [i["url"] for i in infos],
                              dry_run)

    # For the file name, we allow the _repindex special key.
    format_filename = partial(
        RepFormatter(colidx_to_name, missing_value).format,
        filename_format)
    subpaths = _format_filenames(format_filename, rows_with_url, infos)
    return infos, list(sort_paths(subpaths))


def _add_url(row, ds, repo, options=None, drop_after=False):
    filename_abs = row["filename_abs"]
    filename = row["ds_filename"]
    try:
        out_json = repo.add_url_to_file(filename, row["url"],
                                        batch=True, options=options)
    except CommandError as exc:
        yield get_status_dict(action="addurls",
                              ds=ds,
                              type="file",
                              path=filename_abs,
                              message=exc_str(exc),
                              status="error")
        return

    # In the case of an error, the json object has file=None.
    if out_json["file"] is None:
        out_json["file"] = filename_abs
    res_addurls = annexjson2result(
        out_json, ds, action="addurls",
        type="file", logger=lgr)
    yield res_addurls

    if not res_addurls["status"] == "ok":
        return

    if drop_after and 'annexkey' in res_addurls:
        # unfortunately .drop has no batched mode, and drop_key ATM would
        # raise AssertionError if not success, and otherwise return nothing
        try:
            repo.drop_key(res_addurls['annexkey'], batch=True)
            st_kwargs = dict(status="ok")
        except (AssertionError, CommandError) as exc:
            st_kwargs = dict(message=exc_str(exc),
                             status="error")
        yield get_status_dict(action="drop",
                              ds=ds,
                              annexkey=res_addurls['annexkey'],
                              type="file",
                              path=filename_abs,
                              **st_kwargs)


class RegisterUrl(object):
    """Create files (without content) from user-supplied keys and register URLs.
    """

    def __init__(self, ds, repo=None):
        self.ds = ds
        self.repo = repo or ds.repo
        self._err_res = get_status_dict(action="addurls", ds=self.ds,
                                        type="file", status="error")

    def fromkey(self, key, filename):
        return self.repo._run_annex_command_json(
            "fromkey", opts=["--force", key, filename])[0]

    def registerurl(self, key, url):
        self.repo._run_annex_command("registerurl", annex_options=[key, url])

    def __call__(self, row):
        filename = row["ds_filename"]
        try:
            key = row["key"]["key"]
            self.registerurl(key, row["url"])
            res = self.fromkey(key, filename)
        except CommandError as exc:
                yield dict(self._err_res,
                           path=row["filename_abs"],
                           message=exc_str(exc))
        else:
            yield dict(
                annexjson2result(res, self.ds, type="file", logger=lgr),
                message="registered URL",
                action="addurls")


# Note: If any other modules end up needing these batch operations, this should
# find a new home.


class BatchedRegisterUrl(RegisterUrl):
    """Like `RegisterUrl`, but use batched commands underneath.
    """

    def __init__(self, ds, repo=None):
        super().__init__(ds, repo)
        self._batch_commands = {}

    def _batch(self, command, batch_input,
               output_proc=None, json=False, batch_options=None):
        bcmd = self._batch_commands.get(command)
        if not bcmd:
            repo = self.repo
            bcmd = repo._batched.get(
                command, path=repo.path, json=json, output_proc=output_proc,
                annex_options=batch_options)
            self._batch_commands[command] = bcmd
        return bcmd(batch_input)

    def fromkey(self, key, filename):
        return self._batch("fromkey", (key, filename), json=True,
                           # --force is needed because the key (usually) does
                           # not exist in the local repository.
                           batch_options=["--force"])

    @staticmethod
    def _ignore(_stdout):
        return

    def registerurl(self, key, url):
        self._batch("registerurl", (key, url),
                    output_proc=self._ignore, json=False)


def _log_filter_addurls(res):
    return res.get('type') == 'file' and res.get('action') in ["addurl", "addurls"]


@with_result_progress("Adding URLs", log_filter=_log_filter_addurls)
def _add_urls(rows, ds, repo, ifexists=None, options=None,
              drop_after=False, by_key=False):
    """Call `git annex addurl` using information in `rows`.
    """
    add_url = partial(_add_url, ds=ds, repo=repo,
                      drop_after=drop_after, options=options)
    if by_key:
        # The by_key parameter isn't strictly needed, but it lets us avoid some
        # setup if --key wasn't specified.
        if repo.fake_dates_enabled:
            register_url = RegisterUrl(ds, repo)
        else:
            register_url = BatchedRegisterUrl(ds, repo)
    else:
        def register_url(*args, **kwargs):
            raise RuntimeError("bug: this should be impossible")

    add_metadata = {}
    for row in rows:
        filename_abs = row["filename_abs"]
        filename = row["ds_filename"]
        lgr.debug("Adding URLs to %s in %s", filename, ds.path)

        if os.path.exists(filename_abs) or os.path.islink(filename_abs):
            if ifexists == "skip":
                yield get_status_dict(action="addurls",
                                      ds=ds,
                                      type="file",
                                      path=filename_abs,
                                      status="notneeded")
                continue
            elif ifexists == "overwrite":
                lgr.debug("Removing %s", filename_abs)
                unlink(filename_abs)
            else:
                lgr.debug("File %s already exists", filename_abs)

        fn = register_url if row.get("key") else add_url
        all_ok = True
        for res in fn(row):
            if res["status"] != "ok":
                all_ok = False
            yield res
        if not all_ok:
            continue

        if row.get("meta_args"):
            add_metadata[filename] = row["meta_args"]

    if not add_metadata:
        return

    # For some reason interleaving batched addurl and regular metadata calls
    # causes multiple commits, so we do need to have it separate.
    # TODO: figure out with Joey either could be avoided
    from unittest.mock import patch
    with patch.object(repo, "always_commit", False):
        for filename, meta in add_metadata.items():
            lgr.debug("Adding metadata to %s in %s", filename, repo.path)
            assert not repo.always_commit
            for a in repo.set_metadata_(filename, add=meta):
                res = annexjson2result(a, ds, type="file", logger=lgr)
                # Don't show all added metadata for the file because that
                # could quickly flood the output.
                del res["message"]
                yield res


@build_doc
class Addurls(Interface):
    """Create and update a dataset from a list of URLs.

    *Format specification*

    Several arguments take format strings.  These are similar to normal Python
    format strings where the names from `URL-FILE` (column names for a comma-
    or tab-separated file or properties for JSON) are available as
    placeholders. If `URL-FILE` is a CSV or TSV file, a positional index can
    also be used (i.e., "{0}" for the first column). Note that a placeholder
    cannot contain a ':' or '!'.

    In addition, the `FILENAME-FORMAT` arguments has a few special
    placeholders.

      - _repindex

        The constructed file names must be unique across all fields rows.  To
        avoid collisions, the special placeholder "_repindex" can be added to
        the formatter.  Its value will start at 0 and increment every time a
        file name repeats.

      - _url_hostname, _urlN, _url_basename*

        Various parts of the formatted URL are available.  Take
        "http://datalad.org/asciicast/seamless_nested_repos.sh" as an example.

        "datalad.org" is stored as "_url_hostname".  Components of the URL's
        path can be referenced as "_urlN".  "_url0" and "_url1" would map to
        "asciicast" and "seamless_nested_repos.sh", respectively.  The final
        part of the path is also available as "_url_basename".

        This name is broken down further.  "_url_basename_root" and
        "_url_basename_ext" provide access to the root name and extension.
        These values are similar to the result of os.path.splitext, but, in the
        case of multiple periods, the extension is identified using the same
        length heuristic that git-annex uses.  As a result, the extension of
        "file.tar.gz" would be ".tar.gz", not ".gz".  In addition, the fields
        "_url_basename_root_py" and "_url_basename_ext_py" provide access to
        the result of os.path.splitext.

      - _url_filename*

        These are similar to _url_basename* fields, but they are obtained with
        a server request.  This is useful if the file name is set in the
        Content-Disposition header.


    *Examples*

    Consider a file "avatars.csv" that contains::

        who,ext,link
        neurodebian,png,https://avatars3.githubusercontent.com/u/260793
        datalad,png,https://avatars1.githubusercontent.com/u/8927200

    To download each link into a file name composed of the 'who' and 'ext'
    fields, we could run::

      $ datalad addurls -d avatar_ds --fast avatars.csv '{link}' '{who}.{ext}'

    The `-d avatar_ds` is used to create a new dataset in "$PWD/avatar_ds".

    If we were already in a dataset and wanted to create a new subdataset in an
    "avatars" subdirectory, we could use "//" in the `FILENAME-FORMAT`
    argument::

      $ datalad addurls --fast avatars.csv '{link}' 'avatars//{who}.{ext}'

    If the information is represented as JSON lines instead of comma separated
    values or a JSON array, you can use a utility like jq to transform the JSON
    lines into an array that addurls accepts::

      $ ... | jq --slurp . | datalad addurls - '{link}' '{who}.{ext}'

    .. note::

       For users familiar with 'git annex addurl': A large part of this
       plugin's functionality can be viewed as transforming data from
       `URL-FILE` into a "url filename" format that fed to 'git annex addurl
       --batch --with-files'.
    """

    from datalad.distribution.dataset import datasetmethod
    from datalad.interface.utils import eval_results
    from datalad.distribution.dataset import EnsureDataset
    from datalad.support.constraints import EnsureChoice, EnsureNone, EnsureStr
    from datalad.support.param import Parameter

    _params_ = dict(
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""Add the URLs to this dataset (or possibly subdatasets of
            this dataset).  An empty or non-existent directory is passed to
            create a new dataset.  New subdatasets can be specified with
            `FILENAME-FORMAT`.""",
            constraints=EnsureDataset() | EnsureNone()),
        urlfile=Parameter(
            args=("urlfile",),
            metavar="URL-FILE",
            doc="""A file that contains URLs or information that can be used to
            construct URLs.  Depending on the value of --input-type, this
            should be a comma- or tab-separated file (with a header as the
            first row) or a JSON file (structured as a list of objects with
            string values). If '-', read from standard input, taking the
            content as JSON when --input-type is at its default value of
            'ext'."""),
        urlformat=Parameter(
            args=("urlformat",),
            metavar="URL-FORMAT",
            doc="""A format string that specifies the URL for each entry.  See
            the 'Format Specification' section above."""),
        filenameformat=Parameter(
            args=("filenameformat",),
            metavar="FILENAME-FORMAT",
            doc="""Like `URL-FORMAT`, but this format string specifies the file
            to which the URL's content will be downloaded. The name should be a
            relative path and will be taken as relative to the top-level
            dataset, regardless of whether it is specified via [PY: `dataset`
            PY][CMD: --dataset CMD] or inferred. The file name may contain
            directories. The separator "//" can be used to indicate that the
            left-side directory should be created as a new subdataset. See the
            'Format Specification' section above."""),
        input_type=Parameter(
            args=("-t", "--input-type"),
            metavar="TYPE",
            doc="""Whether `URL-FILE` should be considered a CSV file, TSV
            file, or JSON file. The default value, "ext", means to consider
            `URL-FILE` as a JSON file if it ends with ".json" or a TSV file if
            it ends with ".tsv". Otherwise, treat it as a CSV file.""",
            constraints=EnsureChoice(*INPUT_TYPES)),
        exclude_autometa=Parameter(
            args=("-x", "--exclude_autometa"),
            metavar="REGEXP",
            doc="""By default, metadata field=value pairs are constructed with
            each column in `URL-FILE`, excluding any single column that is
            specified via `URL-FORMAT`.  This argument can be used to exclude
            columns that match a regular expression.  If set to '*' or an empty
            string, automatic metadata extraction is disabled completely.  This
            argument does not affect metadata set explicitly with --meta."""),
        meta=Parameter(
            args=("-m", "--meta",),
            metavar="FORMAT",
            action="append",
            doc="""A format string that specifies metadata.  It should be
            structured as "<field>=<value>".  As an example, "location={3}"
            would mean that the value for the "location" metadata field should
            be set the value of the fourth column.  This option can be given
            multiple times."""),
        key=Parameter(
            args=("--key",),
            metavar="FORMAT",
            doc="""A format string that specifies an annex key for the file
            content. In this case, the file is not downloaded; instead the key
            is used to create the file without content. The value should be
            structured as "<backend>[-s<bytes>]--<hash>". As an example,
            "MD5-s{size}--{md5sum}" would use the 'md5sum' and 'size' columns
            to construct the key. Note: If the backend is an annex extension
            backend (i.e., a backend with a trailing "E"), the key must have an
            appropriate extension for the corresponding file name."""),
        message=Parameter(
            args=("--message",),
            metavar="MESSAGE",
            doc="""Use this message when committing the URL additions.""",
            constraints=EnsureNone() | EnsureStr()),
        dry_run=Parameter(
            args=("-n", "--dry-run"),
            action="store_true",
            doc="""Report which URLs would be downloaded to which files and
            then exit."""),
        fast=Parameter(
            args=("--fast",),
            action="store_true",
            doc="""If True, add the URLs, but don't download their content.
            Underneath, this passes the --fast flag to `git annex addurl`."""),
        ifexists=Parameter(
            args=("--ifexists",),
            doc="""What to do if a constructed file name already exists.  The
            default behavior is to proceed with the `git annex addurl`, which
            will fail if the file size has changed.  If set to 'overwrite',
            remove the old file before adding the new one.  If set to 'skip',
            do not add the new file.""",
            constraints=EnsureChoice(None, "overwrite", "skip")),
        missing_value=Parameter(
            args=("--missing-value",),
            metavar="VALUE",
            doc="""When an empty string is encountered, use this value
            instead.""",
            constraints=EnsureNone() | EnsureStr()),
        save=nosave_opt,
        version_urls=Parameter(
            args=("--version-urls",),
            action="store_true",
            doc="""Try to add a version ID to the URL. This currently only has
            an effect on HTTP URLs for AWS S3 buckets. s3:// URL versioning is
            not yet supported, but any URL that already contains a "versionId="
            parameter will be used as is."""),
        cfg_proc=Parameter(
            args=("-c", "--cfg-proc"),
            metavar="PROC",
            action='append',
            doc="""Pass this [PY: cfg_proc PY][CMD: --cfg_proc CMD] value when
            calling `create` to make datasets."""),
        jobs=jobs_opt,
        drop_after=Parameter(
            args=("--drop-after",),
            action="store_true",
            doc="""drop files after adding to annex""",
        ),
    )

    @staticmethod
    @datasetmethod(name='addurls')
    @eval_results
    def __call__(dataset, urlfile, urlformat, filenameformat,
                 input_type="ext", exclude_autometa=None, meta=None, key=None,
                 message=None, dry_run=False, fast=False, ifexists=None,
                 missing_value=None, save=True, version_urls=False,
                 cfg_proc=None, jobs=None, drop_after=False):
        # Temporarily work around gh-2269.
        url_file = urlfile
        url_format, filename_format = urlformat, filenameformat

        from requests.exceptions import RequestException

        from datalad.distribution.dataset import Dataset, require_dataset
        from datalad.interface.results import get_status_dict
        from datalad.support.annexrepo import AnnexRepo

        lgr = logging.getLogger("datalad.plugin.addurls")

        ds = require_dataset(dataset, check_installed=False)
        repo = ds.repo
        if repo and not isinstance(repo, AnnexRepo):
            yield get_status_dict(action="addurls",
                                  ds=ds,
                                  status="error",
                                  message="not an annex repo")
            return

        if url_file != "-":
            url_file = str(resolve_path(url_file, dataset))

        if input_type == "ext":
            if url_file == "-":
                input_type = "json"
            else:
                extension = os.path.splitext(url_file)[1]
                if extension == ".json":
                    input_type = "json"
                elif extension == ".tsv":
                    input_type = "tsv"
                else:
                    input_type = "csv"

        fd = sys.stdin if url_file == "-" else open(url_file)
        try:
            try:
                rows, subpaths = extract(fd, input_type,
                                         url_format, filename_format,
                                         exclude_autometa, meta, key,
                                         dry_run,
                                         missing_value)
            except (ValueError, RequestException) as exc:
                yield get_status_dict(action="addurls",
                                      ds=ds,
                                      status="error",
                                      message=exc_str(exc))
                return
        finally:
            if fd is not sys.stdin:
                fd.close()

        if not rows:
            yield get_status_dict(action="addurls",
                                  ds=ds,
                                  status="notneeded",
                                  message="No rows to process")
            return

        if len(rows) != len(set(row["filename"] for row in rows)):
            yield get_status_dict(action="addurls",
                                  ds=ds,
                                  status="error",
                                  message=("There are file name collisions; "
                                           "consider using {_repindex}"))
            return

        if dry_run:
            for subpath in subpaths:
                lgr.info("Would create a subdataset at %s", subpath)
            for row in rows:
                lgr.info("Would %s %s to %s",
                         "register" if row.get("key") else "download",
                         row["url"],
                         os.path.join(ds.path, row["filename"]))
                if "meta_args" in row:
                    lgr.info("Metadata: %s",
                             sorted(u"{}={}".format(k, v)
                                    for k, v in row["meta_args"].items()))
            yield get_status_dict(action="addurls",
                                  ds=ds,
                                  status="ok",
                                  message="dry-run finished")
            return

        if not repo:
            # Populate a new dataset with the URLs.
            yield from ds.create(
                result_xfm=None,
                return_type='generator',
                cfg_proc=cfg_proc)

        annex_options = ["--fast"] if fast else []

        # to be populated by addurls_to_ds
        files_to_add = set()
        created_subds = []

        def addurls_to_ds(args):
            """The "consumer" for ProducerConsumer parallel execution"""
            subpath, rows = args

            ds_path = ds.path  # shortcut on closure from outside

            # technically speaking, subds might be the ds
            if subpath:
                subds_path = os.path.join(ds_path, subpath)
            else:
                subds_path = ds_path

            for row in rows:
                # Add additional information that we'll need for various
                # operations.
                filename_abs = op.join(ds_path, row["filename"])
                ds_filename = op.relpath(filename_abs, subds_path)
                row.update({"filename_abs": filename_abs,
                            "ds_filename": ds_filename})

            subds = Dataset(subds_path)

            if subds.is_installed():
                lgr.debug(
                    "Not creating subdataset at existing path: %s",
                    subds_path)
            else:
                yield from subds.create(result_xfm=None,
                                        cfg_proc=cfg_proc,
                                        return_type='generator')
                created_subds.append(subpath)
            repo = subds.repo  # "expensive" so we get it once

            if version_urls:
                num_urls = len(rows)
                log_progress(lgr.info, "addurls_versionurls",
                             "Versioning %d URLs", num_urls,
                             label="Versioning URLs",
                             total=num_urls, unit=" URLs")
                for row in rows:
                    url = row["url"]
                    try:
                        # TODO: make get_versioned_url more efficient while going
                        # through the same bucket(s)
                        row["url"] = get_versioned_url(url)
                    except (ValueError, NotImplementedError) as exc:
                        # We don't expect this to happen because get_versioned_url
                        # should return the original URL if it isn't an S3 bucket.
                        # It only raises exceptions if it doesn't know how to
                        # handle the scheme for what looks like an S3 bucket.
                        lgr.warning("error getting version of %s: %s",
                                    row["url"], exc_str(exc))
                    log_progress(lgr.info, "addurls_versionurls",
                                 "Versioned result for %s: %s", url, row["url"],
                                 update=1, increment=True)
                log_progress(lgr.info, "addurls_versionurls", "Finished versioning URLs")

            subds_files_to_add = set()
            for r in _add_urls(rows, subds, repo,
                               ifexists=ifexists, options=annex_options,
                               drop_after=drop_after, by_key=key):
                if r["status"] == "ok":
                    subds_files_to_add.add(r["path"])
                yield r

            files_to_add.update(subds_files_to_add)
            pass  # end of addurls_to_ds


        # We need to group by dataset since otherwise we will initiate
        # batched annex process per each subdataset, which might be infeasible
        # in any use-case with a considerable number of subdatasets.
        # Also groupping allows us for parallelization across datasets, and avoids
        # proliferation of commit messages upon creation of each individual subdataset.

        def keyfn(d):
            # The top-level dataset has a subpath of None.
            return d.get("subpath") or ""

        # We need to serialize itertools.groupby .
        rows_by_ds = [(k, tuple(v)) for k, v in groupby_sorted(rows, key=keyfn)]

        # There could be "intermediate" subdatasets which have no rows but would need
        # their datasets created and saved, so let's add them
        add_subpaths = set(subpaths).difference(r[0] for r in rows_by_ds)
        nrows_by_ds_orig = len(rows_by_ds)
        if add_subpaths:
            for subpath in add_subpaths:
                rows_by_ds.append((subpath, tuple()))
            # and now resort them again since we added
            rows_by_ds = sorted(rows_by_ds, key=lambda r: r[0])

        # We want to provide progress overall files not just datasets
        # so our total will be just a len of rows
        def agg_files(*args, **kwargs):
            return len(rows)

        yield from ProducerConsumerProgressLog(
            rows_by_ds,
            addurls_to_ds,
            agg=agg_files,
            # It is ok to start with subdatasets since top dataset already exists
            safe_to_consume=partial(no_parentds_in_futures, skip=("", None, ".")),
            # our producer provides not only dataset paths and also rows, take just path
            producer_future_key=lambda row_by_ds: row_by_ds[0],
            jobs=jobs,
            # Logging options
            # we will be yielding all kinds of records, but of interest for progress
            # reporting only addurls on files
            # note: annexjson2result overrides 'action' with 'command' content from
            # annex, so we end up with 'addurl' even if we provide 'action'='addurls'.
            # TODO: see if it is all ok, since we might be now yielding both
            # addurls and addurl records.
            log_filter=_log_filter_addurls,
            unit="files",
            lgr=lgr,
        )

        if save:
            extra_msgs = []
            if created_subds:
                extra_msgs.append(f"{len(created_subds)} subdatasets were created")
            if extra_msgs:
                extra_msgs.append('')
            message_addurls = message or f"""\
[DATALAD] add {len(files_to_add)} files to {nrows_by_ds_orig} (sub)datasets from URLs

{os.linesep.join(extra_msgs)}
url_file='{urlfile}'
url_format='{url_format}'
filename_format='{filenameformat}'"""

            # save all in bulk
            files_to_add.update([r[0] for r in rows_by_ds])
            yield from ds.save(
                list(files_to_add),
                message=message_addurls,
                jobs=jobs,
                return_type='generator')


__datalad_plugin__ = Addurls
