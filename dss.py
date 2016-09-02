#!/usr/bin/env python
import argparse
import fnmatch
import os
from os import path
import re
import shutil
from SimpleHTTPServer import SimpleHTTPRequestHandler
import SocketServer
import string
import subprocess
import sys

from docutils.core import publish_parts

def _normpath(p):
    return path.normcase(path.normpath(path.abspath(p)))

class GitError(Exception):

    def __init__(self, error):
        if isinstance(error, subprocess.CalledProcessError):
            Exception.__init__(self, 'Failed: %s\n%s' % (error.cmd, error.output))
        else:
            Exception.__init__(self, error)

# TODO: show fatal rst syntax errors, e.g. include file that doesn't exist
class LiveSiteHandler(SimpleHTTPRequestHandler):
    '''
    Can extend server behavior by replacing this class with a subclass of itself that overrides the
    do_* methods as usual.
    '''

    def do_GET(self):
        # TODO: expire immediately
        # TODO: race condition when reloading during a compile
        # TODO: tests for this
        self.server.site.compile()
        prev_cwd = os.getcwd()
        try:
            os.chdir(self.server.site.target)
            SimpleHTTPRequestHandler.do_GET(self)
        finally:
            os.chdir(prev_cwd)

class Template(string.Template):
    delimiter = '~'

# TODO: ie http-equiv tag
# TODO: generator meta tag?
# TODO: header with home link
# TODO: footer with mod date info
default_template = Template('''<!DOCTYPE html>
<html><head>
    ~favicon
    ~title
    ~stylesheet
    ~scripts
</head><body>
    ~content
</body></html>
''')

class DeadSimpleSite(object):

    def __init__(self, source):
        self.source = path.abspath(source)
        self.target = path.join(self.source, '_site')

    def _clean(self):
        for root, dirs, files in os.walk(self.target, topdown=False):
            for filename in files:
                target = path.join(root, filename)
                relpath = path.relpath(target, self.target)
                if relpath[0] == '.' and filename != '.htaccess':
                    break
                os.remove(target)
            if path.relpath(root, self.target)[0] != '.' and len(os.listdir(root)) == 0:
                os.rmdir(root)

    def _git(self, *args):
        if not path.exists(self.target):
            os.makedirs(self.target)
        with open(os.devnull, 'w') as devnull:
            if args[0] == 'push':
                # Git reads username and password from console, so don't suppress output
                # Also, don't want to throw an error since most likely the user just mistyped
                subprocess.call(('git',) + args, cwd=self.target)
            else:
                return subprocess.check_output(('git',) + args, stderr=subprocess.STDOUT, cwd=self.target)

    def _render(self, source, template):
        relpath = path.relpath(source, self.source)
        depth = len(re.split(r'[/\\]', relpath)) - 1
        html_root = depth * '../'
        style_tag = '<link rel="stylesheet" href="%sstyle/global.css">' % html_root \
            if path.exists(path.join(self.source, 'style/global.css')) else ''
        script_tags = '\n'.join(['<script src="%s%s"></script>' % (html_root, script)
            for script in self._scripts()])
        favicon_tag = '<link rel="shortcut icon" href="favicon.ico">' \
            if path.exists(path.join(self.source, 'favicon.ico')) else ''
        with open(source) as source_file:
            parts = publish_parts(
                source=source_file.read(),
                source_path=source,
                writer_name='html',
                # TODO: smart quotes on
                # TODO: going to need something like this to get sensible title behavior
                #       also, I don't like the default docinfo, e.g. authors, copyright, they
                #       are related because both depend on being the first thing in the source
                #settings_overrides={'doctitle_xform': False}
                )
        return template.substitute(
            content = parts['html_body'],
            favicon = favicon_tag,
            title = '<title>%s</title>' % parts['title'],
            root = html_root,
            stylesheet = style_tag,
            scripts = script_tags)

    def _scripts(self):
        scripts = []
        for root, dirs, files in self._walk_source():
            # TODO: this walks the output directory - that's bad
            for script in fnmatch.filter(files, '*.js'):
                if script[0] == '.':
                    continue
                script_path = path.join(root, script)
                relpath = path.relpath(script_path, self.source)
                # In *.nix, paths may contain backslashes. Don't worry about psychos who do that.
                scripts.insert(0, relpath.replace('\\', '/')) # for Windows
        return scripts

    def _walk_source(self):
        for root, dirs, files in os.walk(self.source):
            dirs.sort()
            files.sort()
            remove = [d for d in dirs if d[0] == '.' or d == '_site']
            for dirname in remove:
                dirs.remove(dirname)
            yield root, dirs, [f for f in files if f[0] != '.' or f == '.htaccess']

    def compile(self):
        self._clean()
        for root, dirs, files in self._walk_source():
            rel = path.relpath(root, self.source)
            if not path.exists(path.join(self.target, rel)):
                os.makedirs(path.join(self.target, rel))
            rst = {path.splitext(f)[0] for f in files if path.splitext(f)[1] == '.rst'}
            for rst_name in rst:
                source = path.join(root, rst_name + '.rst')
                template_path = path.join(root, rst_name + '.html')
                target = path.join(self.target, rel, rst_name + '.html')
                ancestor = rel
                while not path.exists(template_path):
                    template_path = path.join(self.source, ancestor, '__template__.html')
                    if not ancestor:
                        break
                    ancestor = path.dirname(ancestor)
                if path.exists(template_path):
                    with open(template_path) as template_in:
                        template = Template(template_in.read())
                else:
                    template = default_template
                with open(target, 'w') as html_out:
                    html_out.write(self._render(source, template))
            for filename in files:
                source = path.join(root, filename)
                target = path.join(self.target, rel, filename)
                if path.splitext(filename)[1] in ['.rst', '.html']:
                    if path.splitext(filename)[0] in rst:
                        continue
                if filename == '__template__.html':
                    continue
                shutil.copy2(source, target)

    def serve(self, port=8000):
        SocketServer.TCPServer.allow_reuse_address = True
        server = SocketServer.TCPServer(('', port), LiveSiteHandler)
        server.site = self
        server.serve_forever()

    def publish(self, origin=None):
        ''' This will do the following:
        #. check whether _site is a git repo
        #. if not, clone the repository from Github
        #. checkout gh-pages branch
        #. if that fails, create a gh-pages branch
        #. compile site
        #. git addremove
        #. git commit
        #. git push gh-pages gh-pages
        '''
        def clone():
            if not origin:
                raise GitError('Origin required to clone remote repository')
            try:
                # TODO: if the github repo is not initialized, clone fails, so make a first commit
                self._git('clone', origin, '.')
            except subprocess.CalledProcessError as e:
                raise GitError(e)
        # TODO: handle "user pages" as well as project pages, where site is on master branch
        try:
            self._git('--version')
        except subprocess.CalledProcessError as e:
            # TODO: on nix, this raises OSError
            raise GitError('No git command found. Is git installed and on the path?')
        self._clean()
        if not path.exists(self.target):
            os.makedirs(self.target)
        try:
            gitdir = self._git('rev-parse', '--git-dir')
        except subprocess.CalledProcessError:
            clone()
        else:
            if not path.isabs(gitdir):
                gitdir = path.join(self.target, gitdir)
            if path.dirname(_normpath(gitdir)) != _normpath(self.target):
                clone()
        try:
            self._git('reset', '--hard')
            try:
                self._git('checkout', 'gh-pages')
            except subprocess.CalledProcessError:
                self._git('branch', 'gh-pages')
                self._git('checkout', 'gh-pages')
            else:
                self._git('pull')
            self.compile()
            self._git('add', '-A')
            self._git('commit', '-m', 'Dead Simple Site auto publish')
            # TODO: revert commit if push fails
            self._git('push', '-u', 'origin', 'gh-pages')
        except subprocess.CalledProcessError as e:
            raise GitError(e)

def cli(args=None):

    def serve(parsed):
        DeadSimpleSite(parsed.directory).serve(parsed.port)

    def compile(parsed):
        DeadSimpleSite(parsed.directory).compile()

    def publish(parsed):
        try:
            DeadSimpleSite(parsed.directory).publish(parsed.origin)
        except GitError as e:
            sys.stderr.write(str(e) + '\n')
            sys.exit(1)
    
    parser = argparse.ArgumentParser(description='Dead Simple Site generator')
    parser.add_argument('-d', '--directory', help='folder containing site source files', default='.')
    subs = parser.add_subparsers(title='subcommands')

    parser_serve = subs.add_parser('serve', help='serve the site for development')
    parser_serve.add_argument('-p', '--port', type=int, default=8000)
    parser_serve.set_defaults(func=serve)

    parser_compile = subs.add_parser('compile', help='build the site')
    parser_compile.set_defaults(func=compile)
    
    parser_publish = subs.add_parser('publish', help='publish to Github pages')
    # TODO: arguments could be optional if the working dir is a repo on github, by
    #       `git remote show origin`, or if _site is already a github repo
    parser_publish.add_argument('origin', nargs='?',
        help='Github URL to repository. Ignored if _site is already a repository.')
    parser_publish.set_defaults(func=publish)
    
    parsed = parser.parse_args(args)
    parsed.func(parsed)

if __name__ == '__main__':
    cli()
