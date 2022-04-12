# -*- coding: utf-8 -*-
import collections
import datetime
import logging
import os
import re
import subprocess
import tarfile
import tempfile

GitRef = collections.namedtuple(
    "VersionRef",
    [
        "name",
        "commit",
        "source",
        "is_remote",
        "refname",
        "creatordate",
    ],
)

level = logging.DEBUG if os.environ["DEBUG"] else logging.INFO
logging.basicConfig(level=level)
logger = logging.getLogger(__name__)


def get_toplevel_path(cwd=None):
    cmd = (
        "git",
        "rev-parse",
        "--show-toplevel",
    )
    output = subprocess.check_output(cmd, cwd=cwd).decode()
    return output.rstrip("\n")


def get_all_refs(gitroot):
    cmd = (
        "git",
        "for-each-ref",
        "--format",
        "%(objectname)\t%(refname)\t%(creatordate:iso)",
        "refs",
    )
    output = subprocess.check_output(cmd, cwd=gitroot).decode()
    for line in output.splitlines():
        is_remote = False
        fields = line.strip().split("\t")
        if len(fields) != 3:
            continue

        commit = fields[0]
        refname = fields[1]
        creatordate = datetime.datetime.strptime(
            fields[2], "%Y-%m-%d %H:%M:%S %z"
        )

        # Parse refname
        matchobj = re.match(
            r"^refs/(heads|tags|remotes/[^/]+)/(\S+)$", refname
        )
        if not matchobj:
            continue
        source = matchobj.group(1)
        name = matchobj.group(2)

        if source.startswith("remotes/"):
            is_remote = True

        yield GitRef(name, commit, source, is_remote, refname, creatordate)


def get_refs(gitroot, config, files=()):
    for ref in get_all_refs(gitroot):
        if ref.source == "tags":
            if config.smv_tag_whitelist is None or not re.match(
                config.smv_tag_whitelist, ref.name
            ):
                logger.debug(
                    "Skipping '%s' because tag '%s' doesn't match the "
                    "whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        elif ref.source == "heads":
            if config.smv_branch_whitelist is None or not re.match(
                config.smv_branch_whitelist, ref.name
            ):
                logger.debug(
                    "Skipping '%s' because branch '%s' doesn't match the "
                    "whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        elif ref.is_remote and config.smv_remote_whitelist is not None:
            remote_name = ref.source.partition("/")[2]
            if not re.match(config.smv_remote_whitelist, remote_name):
                logger.debug(
                    "Skipping '%s' because remote '%s' doesn't match the "
                    "whitelist pattern",
                    ref.refname,
                    remote_name,
                )
                continue
            if config.smv_branch_whitelist is None or not re.match(
                config.smv_branch_whitelist, ref.name
            ):
                logger.debug(
                    "Skipping '%s' because branch '%s' doesn't match the "
                    "whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        else:
            logger.debug(
                "Skipping '%s' because its not a branch or tag", ref.refname
            )
            continue

        # The ref exists and meets list checks. Check for an override ref.
        if "" != config.smv_refs_override_suffix:
            candidate = "{}{}".format(
                ref.name, config.smv_refs_override_suffix
            )
            cmd = ["git", "show-ref", candidate]
            proc = subprocess.run(cmd, cwd=gitroot, capture_output=True)
            if 0 == proc.returncode:
                override = proc.stdout.decode().split()[0]
                logger.info(
                    "Overriding the ref from {}:::{}".format(
                        ref.refname, ref.commit
                    )
                )
                logger.info("   ...to {}:::{}.".format(candidate, override))

                cmd = [
                    "git",
                    "branch",
                    candidate,
                    "--track",
                    "origin/{}".format(candidate),
                ]
                proc = subprocess.run(cmd, cwd=gitroot, capture_output=True)
                if 0 != proc.returncode:
                    logger.info(
                        "Failed to create a local tracking branch for the override branch"
                    )
                ref = ref._replace(refname=candidate)
                ref = ref._replace(commit=override)

        missing_files = [
            filename
            for filename in files
            if filename != "."
            and not file_exists(gitroot, ref.refname, filename)
        ]
        if missing_files:
            logger.debug(
                "Skipping '%s' because it lacks required files: %r",
                ref.refname,
                missing_files,
            )
            continue

        logger.debug("Planning to build '%s'", ref.refname)

        yield ref


def file_exists(gitroot, refname, filename):
    if os.sep != "/":
        # Git requires / path sep, make sure we use that
        filename = filename.replace(os.sep, "/")

    cmd = (
        "git",
        "cat-file",
        "-e",
        "{}:{}".format(refname, filename),
    )
    proc = subprocess.run(
        cmd, cwd=gitroot, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return proc.returncode == 0


def copy_tree(gitroot, src, dst, reference, sourcepath="."):
    with tempfile.SpooledTemporaryFile() as fp:
        cmd = (
            "git",
            "archive",
            "--format",
            "tar",
            reference.commit,
            "--",
            sourcepath,
        )
        subprocess.check_call(cmd, cwd=gitroot, stdout=fp)
        fp.seek(0)
        with tarfile.TarFile(fileobj=fp) as tarfp:
            tarfp.extractall(dst)
