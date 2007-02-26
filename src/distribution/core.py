# -*- coding: iso-8859-1 -*-
# -----------------------------------------------------------------------------
# core.py - distribution core functions for kaa packages
# -----------------------------------------------------------------------------
# $Id$
#
# -----------------------------------------------------------------------------
# Copyright (C) 2006 Dirk Meyer, Jason Tackaberry
#
# First Edition: Dirk Meyer <dmeyer@tzi.de>
# Maintainer:    Dirk Meyer <dmeyer@tzi.de>
#
# Please see the file AUTHORS for a complete list of authors.
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# -----------------------------------------------------------------------------

# python imports
import os
import sys
import math
import stat
import re
import tempfile
import time
import distutils.core
import distutils.sysconfig

# internal imports
from version import Version
from build_py import build_py

__all__ = ['compile', 'check_library', 'get_library', 'setup', 'ConfigFile',
           'Extension', 'Library']

_libraries = {}

def compile(includes, code='', args=''):
    fd, outfile = tempfile.mkstemp()
    os.close(fd)
    args += ' %s %s' % (os.getenv('CFLAGS', ''), os.getenv('LDFLAGS', ''))
    cc = os.getenv('CC', 'cc')
    f = os.popen("%s -x c - -o %s %s 2>/dev/null >/dev/null" % (cc, outfile, args), "w")
    if not f:
        return False

    for i in includes:
        f.write('#include %s\n' % i)
    f.write('int main() { ' + code + '\nreturn 0;\n};\n')
    result = f.close()

    if os.path.exists(outfile):
        os.unlink(outfile)

    if result == None:
        return True
    return False


def check_library(name, *args):
    lib = Library(name)
    if len(args) < 2:
        minver = args[0]
        print 'checking for', name, '>=', minver, '...',
        sys.__stdout__.flush()
        found, version = lib.check(minver)
        if found:
            print version
        elif version:
            print 'no (%s)' % version
        else:
            print 'no'
    else:
        print 'checking for', name, '...',
        sys.__stdout__.flush()
        if lib.compile(*args):
            print 'ok'
        else:
            print 'no'
    _libraries[name] = lib
    return lib


def get_library(name):
    lib = _libraries.get(name)
    if lib and lib.valid:
        return lib
    return None


class Library(object):
    def __init__(self, name):
        self.name = name
        self.include_dirs = []
        self.library_dirs = []
        self.libraries = []
        self.version = None
        self.valid = False


    def compare_versions(self, a, b):
        # Get maximum component length in both version.
        maxlen =  max([ len(x) for x in (a + '.' + b).split('.') ])
        # Pad each component of A and B to maxlen
        a = [ x.zfill(maxlen) for x in a.split('.') ]
        b = [ x.zfill(maxlen) for x in b.split('.') ]
        # TODO: special handling of rc and beta (others?) suffixes, so
        # that 1.0.0 > 1.0.0rc1 
        return cmp(a, b)


    def get_numeric_version(self, version = None):
        """
        Returns an integer version suitable for numeric comparison.  This
        returns the version of the library gotten after calling check().  If
        check() was not called this method will raise an exception.

        This algorithm is pretty naive and won't work at all if the version
        contains any non-numeric characters.
        """
        if version is None:
            version = self.version

        if version is None:
            raise ValueError, "Version is not known; call check() first"

        if not version.replace(".", "").isdigit():
            raise ValueError, "Version cannot have non-numeric characters."

        # Performs: 1.2.3.4 => 4<<0 + 3<<9 + 2<<18 + 1<<27
        return sum([ int(v) << 9*s for s,v in enumerate(reversed(version.split('.'))) ])


    def check(self, minver):
        """
        Check dependencies add add the flags to include_dirs, library_dirs and
        libraries. The basic logic is taken from pygame.
        """
        if os.system("%s-config --version >/dev/null 2>/dev/null" % self.name) == 0:
            # Use foo-config if it exists.
            command = "%s-config %%s 2>/dev/null" % self.name
            version_arg = "--version"
        elif os.system("pkg-config %s --exists >/dev/null 2>/dev/null" % self.name) == 0:
            # Otherwise try pkg-config foo.
            command = "pkg-config %s %%s 2>/dev/null" % self.name
            version_arg = "--modversion"
        else:
            return False, None

        version = os.popen(command % version_arg).read().strip()
        if len(version) == 0:
            return False, None
        if minver and self.compare_versions(minver, version) > 0:
            return False, version

        for inc in os.popen(command % "--cflags").read().strip().split(' '):
            if inc[2:] and not inc[2:] in self.include_dirs:
                self.include_dirs.append(inc[2:])

        for flag in os.popen(command % "--libs").read().strip().split(' '):
            if flag[:2] == '-L' and not flag[2:] in self.library_dirs:
                self.library_dirs.append(flag[2:])
            if flag[:2] == '-l' and not flag[2:] in self.libraries:
                self.libraries.append(flag[2:])

        self.version = version
        self.valid = True
        return True, version


    def compile(self, includes, code='', args='', extra_libraries = []):
        ext_args = [ "-L%s" % x for x in self.library_dirs ]
        ext_args += [ "-I%s" % x for x in self.include_dirs ]

        for lib in extra_libraries:
            if isinstance(lib, basestring):
                lib = get_library(lib)
            for dir in lib.library_dirs:
                ext_args.append("-L%s" % dir)
            for dir in lib.include_dirs:
                ext_args.append("-I%s" % dir)

        if compile(includes, code, args + ' %s' % ' '.join(ext_args)):
            self.valid = True
            return True

        return False



class ConfigFile(object):
    """
    Config file for the build process.
    """
    def __init__(self, filename):
        self.file = os.path.abspath(filename)
        # create config file
        open(self.file, 'w').close()


    def append(self, line):
        """
        Append something to the config file.
        """
        f = open(self.file, 'a')
        f.write(line + '\n')
        f.close()


    def define(self, variable, value=None):
        """
        Set a #define.
        """
        if value == None:
            self.append('#define %s' % variable)
        else:
            self.append('#define %s %s' % (variable, value))


    def unlink(self):
        """
        Delete config file.
        """
        os.unlink(self.file)


class Extension(object):
    """
    Extension wrapper with additional functions to find libraries and
    support for config files.
    """
    def __init__(self, output, files, include_dirs=[], library_dirs=[],
                 libraries=[], extra_compile_args = [], config=None):
        """
        Init the Extension object.
        """
        self.output = output
        self.files = files
        self.include_dirs = include_dirs[:]
        self.library_dirs = library_dirs[:]
        self.libraries = libraries[:]
        self.library_objects = []
        self.extra_compile_args = ["-Wall"] + extra_compile_args
        if config:
            self.configfile = ConfigFile(config)
        else:
            self.configfile = None


    def config(self, line):
        """
        Write a line to the config file.
        """
        if not self.configfile:
            raise AttributeError('No config file defined')
        self.configfile.append(line)


    def add_library(self, name):
        """
        """
        lib = get_library(name)
        if lib and lib not in self.library_objects:
            self.library_objects.append(lib)
        return False


    def get_library(self, name):
        for lib in self.library_objects:
            if lib.name == name:
                return lib


    def check_library(self, name, minver):
        """
        Check dependencies add add the flags to include_dirs, library_dirs and
        libraries. The basic logic is taken from pygame.
        """
        try:
            lib = Library(name)
            found, version = lib.check(minver)
            if not found:
                if not version:
                    version = 'none'
                raise ValueError, 'requires %s version %s (none found)' % \
                                  (name, minver, version)
            self.library_objects.append(lib)
            return True
        except Exception, e:
            self.error = e

        return False


    def check_cc(self, includes, code='', args='', extra_libraries = []):
        """
        Check the given code with the linker. The optional parameter args
        can contain additional command line options like -l.
        """
        ext_args = []
        for lib in self.library_objects + extra_libraries:
            for dir in lib.library_dirs:
                ext_args.append("-L%s" % dir)
            for dir in lib.include_dirs:
                ext_args.append("-I%s" % dir)
        args += ' %s' % ' '.join(ext_args)
        return compile(includes, code, args)


    def convert(self):
        """
        Convert Extension into a distutils.core.Extension.
        """
        library_dirs = self.library_dirs[:]
        include_dirs = self.include_dirs[:]
        libraries = self.libraries[:]
        for lib in self.library_objects:
            library_dirs.extend(lib.library_dirs)
            include_dirs.extend(lib.include_dirs)
            libraries.extend(lib.libraries)

        return distutils.core.Extension(self.output, self.files,
                                        library_dirs=library_dirs,
                                        include_dirs=include_dirs,
                                        libraries=libraries,
                                        extra_compile_args=self.extra_compile_args)

    def __del__(self):
        """
        Delete the config file.
        """
        if self.configfile:
            self.configfile.unlink()


class EmptyExtensionsList(list):
    """
    A list that is non-zero even when empty.  Used for the ext_modules
    kwarg in setup() for modules with no ext_modules.

    This is a kludge to solve a peculiar problem.  On architectures like
    x86_64, distutils will install "pure" modules (i.e. no C extensions)
    under /usr/lib, and platform-specific modules (with extensions) under
    /usr/lib64.  This is a problem for kaa, because with kaa we have a single
    namespace (kaa/) that have several independent modules: that is, each
    module in Kaa can be installed separately, but they coexist under the
    kaa/ directory hierarchy.

    On x86_64, this results in some kaa modules being installed in /usr/lib
    and others installed in /usr/lib64.  The first problem is that
    kaa/__init__.py is provided by kaa.base, so if kaa.base is installed
    in /usr/lib/ then /usr/lib64/python*/site-packages/kaa/__init__.py does
    not exist and therefore we can't import any modules from that.  We could
    drop a dummy __init__.py there, but then python will have two valid kaa
    modules and only ever see one of them, the other will be ignored.

    So this kludge makes distutils always think the module has extensions,
    so it will install in the platform-specific libdir.  As a result, on
    x86_64, all kaa modules will be installed in /usr/lib64, which is what
    we want.
    """
    def __nonzero__(self):
        return True




class GentooEbuild (distutils.core.Command):

    description = "create gentoo ebuild"

    user_options = [
        ('prefix=', None,
         "portage prefix"),
        ]

    def initialize_options (self):
        self.prefix = None
        
    def finalize_options (self):
        pass
        
    def run (self):
        if not self.prefix:
            print 'Please provide overlay portage prefix'
            return 0
        name = self.distribution.metadata.name
        version = self.distribution.metadata.version
        if not os.path.isfile('%s.ebuild' % name):
            print 'No ebuild template provided'
            return 0
        edata = open('%s.ebuild' % name).read()
        ebuild = '%s/dev-python/%s/%s-%s.ebuild' % (self.prefix, name, name, version)
        if not os.path.isdir(os.path.dirname(ebuild)):
            os.makedirs(os.path.dirname(ebuild))
        open(ebuild, 'w').write(edata)
        os.system('ebuild %s digest' % ebuild)


def setup(**kwargs):
    """
    A setup script wrapper for kaa modules.
    """
    def _find_packages((kwargs, prefix), dirname, files):
        """
        Helper function to create 'packages' and 'package_dir'.
        """
        if not '__init__.py' in files:
            return
        python_dirname = prefix + dirname[3:].replace('/', '.')
        # Anything under module/src/extensions/foo gets translated to 
        # kaa.module.foo.
        python_dirname = python_dirname.replace(".extensions.", ".")
        kwargs['package_dir'][python_dirname] = dirname
        kwargs['packages'].append(python_dirname)


    if 'module' not in kwargs:
        raise AttributeError('\'module\' not defined')

    # create name
    kwargs['name'] = 'kaa-' + kwargs['module']

    # search for source files and add it package_dir and packages
    kwargs['package_dir'] = {}
    kwargs['packages']    = []
    if kwargs['module'] == 'base':
        os.path.walk('src', _find_packages, (kwargs, 'kaa'))
    else:
        os.path.walk('src', _find_packages, (kwargs, 'kaa.' + kwargs['module']))

    # convert Extensions
    if kwargs.get('ext_modules'):
        kaa_ext_modules = kwargs['ext_modules']
        ext_modules = []
        for ext in kaa_ext_modules:
            ext_modules.append(ext.convert())
        kwargs['ext_modules'] = ext_modules
        # We need to compile an extension, so first do a naive check to ensure
        # Python headers are available.
        if not os.path.exists(os.path.join(distutils.sysconfig.get_python_inc(), 'Python.h')):
            print "- Python headers not found; please install python development package."
            sys.exit(1)

    else:
        # No extensions, but trick distutils into thinking we do have, so
        # the module gets installed in the platform-specific libdir.
        kwargs['ext_modules'] = EmptyExtensionsList()

    # check version.py information
    write_version = False
    if 'version' in kwargs and not kwargs['module'] == 'base':
        write_version = True
        # check if a version.py is there
        if os.path.isfile('src/version.py'):
            # read the file to find the old version information
            f = open('src/version.py')
            for line in f.readlines():
                if line.startswith('VERSION'):
                    if eval(line[10:-1]) == kwargs['version']:
                        write_version = False
                    break
            f.close()

    # delete 'module' information, not used by distutils.setup
    del kwargs['module']

    if write_version:
        # Write a version.py and add it to the list of files to
        # be installed.
        f = open('src/version.py', 'w')
        f.write('# version information for %s\n' % kwargs['name'])
        f.write('# autogenerated by kaa.distribution\n\n')
        f.write('from kaa.version import Version\n')
        f.write('VERSION = Version(\'%s\')\n' % kwargs['version'])
        f.close()

    # add some missing keywords
    if 'author' not in kwargs:
        kwargs['author'] = 'Freevo Development Team'
    if 'author_email' not in kwargs:
        kwargs['author_email'] = 'freevo-devel@lists.sourceforge.net'
    if 'url' not in kwargs:
        kwargs['url'] = 'http://freevo.sourceforge.net/kaa'

    # We use summary and description as keywords that map to distutils
    # description and long_description
    if 'description' in kwargs:
        kwargs['long_description'] = kwargs['description']
        del kwargs['description']
    if 'summary' in kwargs:
        kwargs['description'] = kwargs['summary']
        if 'long_description' not in kwargs:
            kwargs['long_description'] = kwargs['summary']
        del kwargs['summary']

    # add extra commands
    if not 'cmdclass' in kwargs:
        kwargs['cmdclass'] = {}
    kwargs['cmdclass']['build_py'] = build_py
    kwargs['cmdclass']['ebuild'] = GentooEbuild

    if len(sys.argv) > 1 and sys.argv[1] == 'bdist_rpm':
        dist = None
        kwargs['name'] = 'python-' + kwargs['name']
        release = "1"
        if '--dist' in sys.argv:
            # TODO: determine this automatically
            idx = sys.argv.index('--dist')
            sys.argv.pop(idx)
            dist = sys.argv.pop(idx)

        if '--release' in sys.argv:
            idx = sys.argv.index('--release')
            sys.argv.pop(idx)
            release = sys.argv.pop(idx)

        if '--snapshot' in sys.argv:
            # If --snapshot is specified on the command line, set the release
            # to contain today's date, for bundling svn snapshots.
            release = "0.%s" % time.strftime("%Y%m%d")
            sys.argv.remove('--snapshot')

        if dist:
            release += "." + dist

        sys.argv.append('--release=%s' % release)

        if 'rpminfo' in kwargs:
            # Grab rpm metadata from setup kwargs and expose as cmdline 
            # parameters to distutils.
            rpminfo = kwargs['rpminfo']
            if dist in rpminfo:
                # dist-specific parameters take precedence
                for key, value in rpminfo[dist].items():
                    rpminfo[key] = value
            for param in ('requires', 'build_requires', 'conflicts', 'obsoletes', 'provides'):
                if param in rpminfo:
                    sys.argv.append("--%s=%s" % (param.replace('_', '-'), rpminfo[param]))


    if 'rpminfo' in kwargs:
        del kwargs['rpminfo']

    # run the distutils.setup function
    return distutils.core.setup(**kwargs)
