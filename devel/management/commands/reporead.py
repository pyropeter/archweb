# -*- coding: utf-8 -*-
"""
reporead command

Parses a repo.db.tar.gz file and updates the Arch database with the relevant
changes.

Usage: ./manage.py reporead ARCH PATH
 ARCH:  architecture to update; must be available in the database
 PATH:  full path to the repo.db.tar.gz file.

Example:
  ./manage.py reporead i686 /tmp/core.db.tar.gz
"""

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q

import codecs
import os
import re
import sys
import tarfile
import logging
from datetime import datetime
from optparse import make_option

from logging import ERROR, WARNING, INFO, DEBUG

from main.models import Arch, Package, Repo

logging.basicConfig(
    level=WARNING,
    format='%(asctime)s -> %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr)
logger = logging.getLogger()

class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option('-f', '--force', action='store_true', dest='force', default=False,
            help='Force a re-import of data for all packages instead of only new ones. Will not touch the \'last updated\' value.'),
        make_option('--filesonly', action='store_true', dest='filesonly', default=False,
            help='Load filelists if they are outdated, but will not add or remove any packages. Will not touch the \'last updated\' value.'),
    )
    help = "Runs a package repository import for the given arch and file."
    args = "<arch> <filename>"

    def handle(self, arch=None, filename=None, **options):
        if not arch:
            raise CommandError('Architecture is required.')
        if not validate_arch(arch):
            raise CommandError('Specified architecture %s is not currently known.' % arch)
        if not filename:
            raise CommandError('Package database file is required.')
        filename = os.path.normpath(filename)
        if not os.path.exists(filename) or not os.path.isfile(filename):
            raise CommandError('Specified package database file does not exist.')

        v = int(options.get('verbosity', 0))
        if v == 0:
            logger.level = ERROR
        elif v == 1:
            logger.level = INFO
        elif v == 2:
            logger.level = DEBUG

        import signal, traceback
        handler = lambda sig, stack: traceback.print_stack(stack)
        signal.signal(signal.SIGQUIT, handler)
        signal.signal(signal.SIGUSR1, handler)

        return read_repo(arch, filename, options)


class Pkg(object):
    """An interim 'container' object for holding Arch package data."""
    bare = ( 'name', 'base', 'arch', 'desc', 'filename',
            'md5sum', 'url', 'builddate', 'packager' )
    squash = ( 'license', )
    number = ( 'csize', 'isize' )

    def __init__(self, repo):
        self.repo = repo
        self.ver = None
        self.rel = None
        for k in self.bare + self.squash + self.number:
            setattr(self, k, None)

    def populate(self, values):
        for k, v in values.iteritems():
            # ensure we stay under our DB character limit
            if k in self.bare:
                setattr(self, k, v[0][:254])
            elif k in self.squash:
                setattr(self, k, u', '.join(v)[:254])
            elif k in self.number:
                setattr(self, k, long(v[0]))
            elif k == 'force':
                setattr(self, k, True)
            elif k == 'version':
                ver, rel = v[0].rsplit('-')
                setattr(self, 'ver', ver)
                setattr(self, 'rel', rel)
            else:
                # files, depends, etc.
                setattr(self, k, v)


def find_user(userstring):
    '''
    Attempt to find the corresponding User object for a standard
    packager string, e.g. something like
        'A. U. Thor <author@example.com>'.
    We start by searching for a matching email address; we then move onto
    matching by first/last name. If we cannot find a user, then return None.
    '''
    if userstring in find_user.cache:
        return find_user.cache[userstring]
    matches = re.match(r'^([^<]+)? ?<([^>]*)>', userstring)
    if not matches:
        return None

    user = None
    name = matches.group(1)
    email = matches.group(2)

    def user_email():
        return User.objects.get(email=email)
    def profile_email():
        return User.objects.get(userprofile__public_email=email)
    def user_name():
        # yes, a bit odd but this is the easiest way since we can't always be
        # sure how to split the name. Ensure every 'token' appears in at least
        # one of the two name fields.
        name_q = Q()
        for token in name.split():
            # ignore quoted parts; e.g. nicknames in strings
            if re.match(r'^[\'"].*[\'"]$', token):
                print "token match:", token
                continue
            name_q &= (Q(first_name__icontains=token) |
                    Q(last_name__icontains=token))
        return User.objects.get(name_q)

    for matcher in (user_email, profile_email, user_name):
        try:
            user = matcher()
            break
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            pass

    find_user.cache[userstring] = user
    return user

# cached mappings of user strings -> User objects so we don't have to do the
# lookup more than strictly necessary.
find_user.cache = {}

def populate_pkg(dbpkg, repopkg, force=False, timestamp=None):
    if repopkg.base:
        dbpkg.pkgbase = repopkg.base
    else:
        dbpkg.pkgbase = repopkg.name
    dbpkg.pkgver = repopkg.ver
    dbpkg.pkgrel = repopkg.rel
    dbpkg.pkgdesc = repopkg.desc
    dbpkg.license = repopkg.license
    dbpkg.url = repopkg.url
    dbpkg.filename = repopkg.filename
    dbpkg.compressed_size = repopkg.csize
    dbpkg.installed_size = repopkg.isize
    try:
        dbpkg.build_date = datetime.utcfromtimestamp(int(repopkg.builddate))
    except ValueError:
        try:
            dbpkg.build_date = datetime.strptime(repopkg.builddate,
                    '%a %b %d %H:%M:%S %Y')
        except ValueError:
            logger.warning('Package %s had unparsable build date %s' % \
                    (repopkg.name, repopkg.builddate))
    dbpkg.packager_str = repopkg.packager
    # attempt to find the corresponding django user for this string
    dbpkg.packager = find_user(repopkg.packager)

    if timestamp:
        dbpkg.flag_date = None
        dbpkg.last_update = timestamp
    dbpkg.save()

    populate_files(dbpkg, repopkg, force=force)

    dbpkg.packagedepend_set.all().delete()
    if 'depends' in repopkg.__dict__:
        for y in repopkg.depends:
            # make sure we aren't adding self depends..
            # yes *sigh* i have seen them in pkgbuilds
            dpname, dpvcmp = re.match(r"([a-z0-9._+-]+)(.*)", y).groups()
            if dpname == repopkg.name:
                logger.warning('Package %s has a depend on itself' % repopkg.name)
                continue
            dbpkg.packagedepend_set.create(depname=dpname, depvcmp=dpvcmp)
            logger.debug('Added %s as dep for pkg %s' % (dpname, repopkg.name))

    dbpkg.packagegroup_set.all().delete()
    if 'groups' in repopkg.__dict__:
        for y in repopkg.groups:
            dbpkg.packagegroup_set.create(name=y)


def populate_files(dbpkg, repopkg, force=False):
    if not force:
        if not dbpkg.files_last_update or not dbpkg.last_update:
            pass
        elif dbpkg.files_last_update > dbpkg.last_update:
            return
    # only delete files if we are reading a DB that contains them
    if 'files' in repopkg.__dict__:
        dbpkg.packagefile_set.all().delete()
        logger.info("adding %d files for package %s" % (len(repopkg.files), dbpkg.pkgname))
        for x in repopkg.files:
            dbpkg.packagefile_set.create(path=x)
        dbpkg.files_last_update = datetime.now()
        dbpkg.save()

def db_update(archname, reponame, pkgs, options):
    """
    Parses a list and updates the Arch dev database accordingly.

    Arguments:
      pkgs -- A list of Pkg objects.

    """
    logger.info('Updating Arch: %s' % archname)
    force = options.get('force', False)
    filesonly = options.get('filesonly', False)
    repository = Repo.objects.get(name__iexact=reponame)
    architecture = Arch.objects.get(name__iexact=archname)
    dbpkgs = Package.objects.filter(arch=architecture, repo=repository)
    # It makes sense to fully evaluate our DB query now because we will
    # be using 99% of the objects in our "in both sets" loop. Force eval
    # by calling list() on the QuerySet.
    list(dbpkgs)
    # This makes our inner loop where we find packages by name *way* more
    # efficient by not having to go to the database for each package to
    # SELECT them by name.
    dbdict = dict([(pkg.pkgname, pkg) for pkg in dbpkgs])

    # go go set theory!
    # thank you python for having a set class <3
    logger.debug("Creating sets")
    dbset = set([pkg.pkgname for pkg in dbpkgs])
    syncset = set([pkg.name for pkg in pkgs])
    logger.info("%d packages in current web DB" % len(dbset))
    logger.info("%d packages in new updating db" % len(syncset))
    # packages in syncdb and not in database (add to database)
    logger.debug("Set theory: Packages in syncdb not in database")
    in_sync_not_db = syncset - dbset
    logger.info("%d packages in sync not db" % len(in_sync_not_db))

    # Try to catch those random orphaning issues that make Eric so unhappy.
    if len(dbset) > 20:
        dbpercent = 100.0 * len(syncset) / len(dbset)
    else:
        # we don't have 20 packages in this repo/arch, so this check could
        # produce a lot of false positives (or a div by zero). fake it
        dbpercent = 100.0
    logger.info("DB package ratio: %.1f%%" % dbpercent)
    if dbpercent < 50.0 and not repository.testing:
        logger.error(".db.tar.gz has %.1f%% the number of packages in the web database" % dbpercent)
        raise Exception(
            'It looks like the syncdb is less than half the size of the web db. WTF?')

    if dbpercent < 75.0:
        logger.warning(".db.tar.gz has %.1f%% the number of packages in the web database." % dbpercent)

    if not filesonly:
        # packages in syncdb and not in database (add to database)
        logger.debug("Set theory: Packages in syncdb not in database")
        for p in [x for x in pkgs if x.name in in_sync_not_db]:
            logger.info("Adding package %s", p.name)
            pkg = Package(pkgname = p.name, arch = architecture, repo = repository)
            populate_pkg(pkg, p, timestamp=datetime.now())

        # packages in database and not in syncdb (remove from database)
        logger.debug("Set theory: Packages in database not in syncdb")
        in_db_not_sync = dbset - syncset
        for p in in_db_not_sync:
            logger.info("Removing package %s from database", p)
            Package.objects.get(
                pkgname=p, arch=architecture, repo=repository).delete()

    # packages in both database and in syncdb (update in database)
    logger.debug("Set theory: Packages in database and syncdb")
    pkg_in_both = syncset & dbset
    for p in [x for x in pkgs if x.name in pkg_in_both]:
        logger.debug("Looking for package updates")
        dbp = dbdict[p.name]
        timestamp = None
        # for a force, we don't want to update the timestamp.
        # for a non-force, we don't want to do anything at all.
        if filesonly:
            pass
        elif '-'.join((p.ver, p.rel)) == '-'.join((dbp.pkgver, dbp.pkgrel)):
            if not force:
                continue
        else:
            timestamp = datetime.now()
        if filesonly:
            logger.debug("Checking files for package %s in database", p.name)
            populate_files(dbp, p)
        else:
            logger.info("Updating package %s in database", p.name)
            populate_pkg(dbp, p, force=force, timestamp=timestamp)

    logger.info('Finished updating Arch: %s' % archname)


def parse_info(iofile):
    """
    Parses an Arch repo db information file, and returns variables as a list.
    """
    store = {}
    blockname = None
    for line in iofile:
        line = line.strip()
        if len(line) == 0:
            continue
        elif line.startswith('%') and line.endswith('%'):
            blockname = line[1:-1].lower()
            logger.debug("Parsing package block %s", blockname)
            store[blockname] = []
        elif blockname:
            store[blockname].append(line)
        else:
            raise Exception("Read package info outside a block: %s" % line)
    return store


def parse_repo(repopath):
    """
    Parses an Arch repo db file, and returns a list of Pkg objects.

    Arguments:
     repopath -- The path of a repository db file.

    """
    logger.info("Starting repo parsing")
    if not os.path.exists(repopath):
        logger.error("Could not read file %s", repopath)

    logger.info("Reading repo tarfile %s", repopath)
    filename = os.path.split(repopath)[1]
    m = re.match(r"^(.*)\.(db|files)\.tar\.(.*)$", filename)
    if m:
        reponame = m.group(1)
    else:
        logger.error("File does not have the proper extension")
        raise Exception("File does not have the proper extension")

    repodb = tarfile.open(repopath,"r:gz")
    ## assuming well formed tar, with dir first then files after
    ## repo-add enforces this
    logger.debug("Starting package parsing")
    dbfiles = ('desc', 'depends', 'files')
    pkgs = {}
    for tarinfo in repodb.getmembers():
        if tarinfo.isdir():
            continue
        elif tarinfo.isreg():
            pkgid, fname = os.path.split(tarinfo.name)
            if fname not in dbfiles:
                continue
            data_file = repodb.extractfile(tarinfo)
            data_file = codecs.EncodedFile(data_file, 'utf-8')
            try:
                data = parse_info(data_file)
                p = pkgs.setdefault(pkgid, Pkg(reponame))
                p.populate(data)
            except UnicodeDecodeError, e:
                logger.warn("Could not correctly decode %s, skipping file" % \
                        tarinfo.name)
            data_file.close()

            logger.debug("Done parsing file %s", fname)

    repodb.close()
    logger.info("Finished repo parsing, %d total packages" % len(pkgs))
    return (reponame, pkgs.values())

def validate_arch(arch):
    "Check if arch is valid."
    available_arches = [x.name for x in Arch.objects.all()]
    return arch in available_arches

@transaction.commit_on_success
def read_repo(primary_arch, file, options):
    """
    Parses repo.db.tar.gz file and returns exit status.
    """
    repo, packages = parse_repo(file)

    # sort packages by arch -- to handle noarch stuff
    packages_arches = {}
    packages_arches['any'] = []
    packages_arches[primary_arch] = []

    for package in packages:
        if package.arch in ('any', primary_arch):
            packages_arches[package.arch].append(package)
        else:
            # we don't include mis-arched packages
            logger.warning("Package %s arch = %s" % (
                package.name,package.arch))
    logger.info('Starting database updates.')
    for (arch, pkgs) in packages_arches.items():
        db_update(arch, repo, pkgs, options)
    logger.info('Finished database updates.')
    return 0

# vim: set ts=4 sw=4 et:
