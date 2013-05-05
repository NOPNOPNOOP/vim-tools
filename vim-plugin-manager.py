#!/usr/bin/env python
# vim: set fileencoding=utf-8 :

# TODO Automatically run tests before release? (first have to start writing them!)

"""
Usage: vim-plugin-manager [OPTIONS]

Publish Vim plug-ins to GitHub and/or Vim Online using a highly
automated workflow that includes the following steps:

 1. Find the previous release on Vim Online;
 2. Determine the release about to be published;
 3. Publish the changes and tags to GitHub;
 4. Generate a change log from the commit log;
 5. Approve the change log for use on Vim Online;
 6. Generate a release archive and upload it to Vim Online;
 7. Open the Vim Online page of the plug-in to review the result;
 8. Run a post-release hook for any further custom handling.

Supported options:
  -n, --dry-run      don't actually upload anything anywhere
  -i, --install      install shared pre/post commit hooks
  -p, --pre-commit   run shared pre-commit hooks
  -P, --post-commit  run shared post-commit hooks
  -r, --release      release to GitHub [and Vim Online]
  -v, --verbose      make more noise
  -h, --help         show this message and exit
"""

# Standard library modules.
import codecs
import ConfigParser
import getopt
import logging
import netrc
import os
import re
import subprocess
import sys
import time
import urllib
import webbrowser

# External dependency, install with:
#  apt-get install python-mechanize
#  pip install mechanize
import mechanize

# External dependencies, bundled with the Vim plug-in manager.
import coloredlogs
import html2vimdoc

def main():

    """
    Command line interface for the Vim plug-in manager.
    """

    # Parse the command line arguments.
    try:
        options, arguments = getopt.getopt(sys.argv[1:], 'nipPrvh',
                ['dry-run', 'install', 'pre-commit', 'post-commit', 'release', 'verbose', 'help'])
    except Exception, e:
        sys.stderr.write("Error: %s\n\n" % e)
        usage()
        sys.exit(1)

    # Command line option defaults.
    dry_run = False
    verbose = False
    install = False
    pre_commit = False
    post_commit = False
    release = False

    # Map options to variables.
    for option, value in options:
        if option in ('-n', '--dry-run'):
            dry_run = True
        elif option in ('-i', '--install'):
            install = True
        elif option in ('-p', '--pre-commit'):
            pre_commit = True
        elif option in ('-P', '--post-commit'):
            post_commit = True
        elif option in ('-r', '--release'):
            release = True
        elif option in ('-v', '--verbose'):
            verbose = True
        elif option in ('-h', '--help'):
            usage()
            return
        else:
            assert False, "Unhandled option!"

    if not (install or pre_commit or post_commit or release):
        usage()
    else:
        # Initialize the Vim plug-in manager with the selected options.
        manager = VimPluginManager(dry_run=dry_run, verbose=verbose)
        # Execute the requested action.
        if install:
            manager.install_git_hooks()
        if pre_commit:
            manager.run_precommit_hooks()
        if post_commit:
            manager.run_postcommit_hooks()
        if release:
            manager.publish_release()

def usage():
    sys.stdout.write("%s\n" % __doc__.strip())

class VimPluginManager:

    """
    The Vim plug-in manager is implemented as a class because it has quite a
    bit of internal state (specifically configuration and logging) and objects
    provide a nice way to encapsulate this.
    """

    def __init__(self, dry_run=False, verbose=False):
        """
        Initialize the internal state of the Vim plug-in manager, including the
        configuration and logging subsystems.
        """
        self.plugins = {}
        self.dry_run = dry_run
        self.init_logging()
        self.load_configuration()
        if dry_run:
            self.logger.info("Enabling dry run.")
        if verbose:
            self.logger.info("Enabling verbose output.")
            self.logger.setLevel(logging.DEBUG)

    def init_logging(self):
        """
        Initialize the logging subsystem.
        """
        self.logger = logging.getLogger('vim-plugin-manager')
        self.logger.addHandler(coloredlogs.ColoredConsoleHandler())
        self.logger.setLevel(logging.INFO)

    def load_configuration(self):
        """
        Load the configuration file with plug-in definitions.
        """
        filename = os.path.expanduser('~/.vimplugins')
        self.logger.debug("Loading configuration from %s ..", filename)
        parser = ConfigParser.RawConfigParser()
        parser.read(filename)
        for plugin_name in parser.sections():
            self.logger.debug("Loading plug-in: %s", plugin_name)
            items = dict(parser.items(plugin_name))
            items['name'] = plugin_name
            directory = os.path.expanduser(items['directory'])
            if not os.path.isdir('%s/.git' % directory):
                msg = "Configuration error: The directory %s is not a git repository!"
                raise Exception, msg % directory
            items['directory'] = directory
            self.plugins[plugin_name] = items

    ## Release management.

    def publish_release(self):
        """
        The main function of the Vim plug-in manager: Publishing new releases
        to GitHub and Vim Online.
        """
        try:
            plugin_name = self.find_current_plugin()
            previous_release = self.find_version_on_vim_online(plugin_name)
            committed_version = self.find_version_in_repository(plugin_name)
            if committed_version == previous_release:
                self.logger.info("Everything up to date!")
            else:
                if self.dry_run:
                    self.logger.info("Skipping GitHub push because we're doing a dry run.")
                else:
                    self.publish_changes_to_github()
                suggested_changelog = self.generate_changelog(plugin_name, previous_release, committed_version)
                approved_changelog = self.approve_changelog(suggested_changelog)
                if not approved_changelog.strip():
                    self.logger.warn("Empty change log, canceling release ..")
                elif self.dry_run:
                    self.logger.info("Skipping Vim Online release because we're doing a dry run.")
                else:
                    self.publish_release_to_vim_online(plugin_name, committed_version, approved_changelog)
                    self.show_release_on_vim_online(plugin_name)
                    self.run_post_release_hook(plugin_name)
                    self.logger.info("Done!")
        except ExternalCommandFailed, e:
            self.logger.fatal("External command failed: %s", ' '.join(e.command))
            self.logger.exception(e)
            sys.exit(1)
        except Exception, e:
            self.logger.fatal("Something went terribly wrong!")
            self.logger.exception(e)
            sys.exit(1)

    def publish_changes_to_github(self):
        """
        Publish committed changes and tags to the remote repository on GitHub.
        """
        self.logger.info("Pushing change sets to GitHub ..")
        run('git', 'push', 'origin', 'master')
        run('git', 'push', '--tags')

    def generate_changelog(self, plugin_name, previous_version, current_version):
        """
        Generate a change log from the one-line messages of all commits between
        the previous release and the current one combined with links to the
        commits on GitHub.
        """
        # Find the current tag in the local git repository.
        self.logger.debug("Generating change log based on git commits & tags ..")
        # Generate a range for git log to find all commits between the previous
        # release and the current one.
        commit_range = previous_version + '..' + current_version
        # Generate the change log from the abbreviated commit message(s).
        changelog = []
        repo_url = 'http://github.com/%s' % plugin_name
        for line in reversed(run('git', 'log', '--pretty=oneline', '--abbrev-commit', commit_range).splitlines()):
            commit_hash, commit_desc = line.split(None, 1)
            changelog.append(' \x95 %s:\n' % commit_desc.strip().rstrip(':') +
                             '   %s/commit/%s\n\n' % (repo_url, commit_hash))
        return changelog.rstrip()

    def approve_changelog(self, suggested_changelog):
        """
        Open the suggested change log in a text editor so the user gets a
        chance to inspect the suggested change log, make any required changes
        or clear the change log to abort the release.
        """
        # Save the change log to a temporary file.
        fname = '/tmp/vim-online-changelog'
        with open(fname, 'w') as handle:
            handle.write(suggested_changelog)
        # Run Vim with the cp1252 encoding because this is the encoding
        # expected by http://www.vim.org.
        run('gvim', '-fc', 'e ++enc=cp1252 %s' % fname)
        # Get the approved change log.
        with open(fname) as handle:
            approved_changelog = handle.read()
        os.unlink(fname)
        return approved_changelog.rstrip()

    def publish_release_to_vim_online(self, plugin_name, new_version, changelog):
        """
        Automatically publish a new release to Vim Online without opening an
        actual web browser (scripted HTTP exchange using Mechanize module).
        """
        self.logger.info("Uploading release to Vim Online ..")
        # Find the username & password in the ~/.netrc file.
        user_netrc = netrc.netrc(os.path.expanduser('~/.netrc'))
        username, _, password = user_netrc.hosts['www.vim.org']
        # Find the script ID in the plug-in configuration.
        script_id = int(self.plugins[plugin_name]['script-id'])
        # Generate the ZIP archive and up-load it.
        zip_archive = self.generate_release_archive()
        with open(zip_archive) as zip_handle:
            # Open a session to Vim Online.
            self.logger.debug("Connecting to Vim Online ..")
            session = mechanize.Browser()
            session.open("http://www.vim.org/scripts/add_script_version.php?script_id=%i" % script_id)
            # Fill in the login form.
            self.logger.debug("Logging in on Vim Online ..")
            session.select_form('login')
            session['userName'] = username
            session['password'] = password
            session.submit()
            # Fill in the upload form.
            self.logger.debug("Uploading release archive to Vim Online ..")
            session.select_form('script')
            session['vim_version'] = ['7.0']
            session['script_version'] = new_version
            session['version_comment'] = changelog
            session.form.add_file(zip_handle, 'application/zip', os.path.basename(zip_archive), 'script_file')
            session.submit()
            self.logger.debug("Finished uploading release archive!")
        # Cleanup the release archive.
        os.unlink(zip_archive)

    def generate_release_archive(self, plugin_name):
        """
        Generate a ZIP archive from the HEAD of the local git repository (clean
        of any local changes and/or uncommitted files).
        """
        filename = '/tmp/%s' % self.plugins[plugin_name]['zip-file']
        self.logger.info("Saving ZIP archive of HEAD to %s ..", filename)
        run('git', 'archive', '-o', filename, 'HEAD')
        return filename

    def show_release_on_vim_online(self, plugin_name):
        """
        Open the Vim Online web page of the Vim plug-in in a web browser so the
        user can verify that the new release was successfully uploaded.
        """
        script_id = int(self.plugins[plugin_name]['script-id'])
        webbrowser.open('http://www.vim.org/scripts/script.php?script_id=%d' % script_id)

    def run_post_release_hook(self, plugin_name):
        """
        Run a custom script after publishing the latest release to GitHub and
        Vim Online. In my case this script updates the link to the latest ZIP
        archive on peterodding.com to make sure I don't serve old downloads
        after releasing a new version.
        """
        self.logger.debug("Checking for post-release hook ..")
        try:
            pathname = run('which', 'after-vim-plugin-release')
        except ExternalCommandFailed:
            # The hook is not installed.
            self.logger.debug("No post-release hook installed!")
        else:
            self.logger.info("Running post-release hook %s ..", pathname)
            run(pathname)

    ## Git hook management.

    def install_git_hooks(self):
        """
        Install wrapper scripts for the shared git hooks
        """
        self.logger.info("Installing git hooks ..")
        for plugin in self.sorted_plugins:
            repository = plugin['directory']
            directory = '%s/.git/hooks' % repository
            if not os.path.isdir(directory):
                os.mkdir(directory)
            else:
                self.logger.debug("Deleting old hooks in %s ..", repository)
                for entry in os.listdir(directory):
                    os.unlink('%s/%s' % (directory, entry))
            self.create_hook_script('%s/pre-commit' % directory)
            self.create_hook_script('%s/post-commit' % directory)
        self.logger.info("Done. Created git hooks for %i plug-ins.", len(self.plugins))

    def create_hook_script(self, hook_path):
        """
        Create a git hook using a small wrapper script instead of a symbolic
        link. I keep my Vim profile and the git repositories of my plug-ins in
        my Dropbox and unfortunately Dropbox does not support symbolic links
        (it doesn't synchronize the link, it synchronizes the content, so the
        actual symbolic link only exists on the machine where it was created).
        """
        self.logger.debug("Creating hook script: %s", hook_path)
        hook_name = os.path.basename(hook_path)
        # The hook scripts become part of my Dropbox, synced between Mac OS X
        # and Linux. For this reason we generate a relative path to the
        # vim-plugin-manager script so that the hook works on both Linux
        # (/home/*) and Mac OS X (/Users/*).
        relpath = os.path.relpath(__file__, os.path.dirname(hook_path))
        with open(hook_path, 'w') as handle:
            handle.write('#!/bin/bash\n\n')
            handle.write('exec %s --%s\n' % (relpath, hook_name))
        os.chmod(hook_path, 0755)

    ## Pre-commit hooks.

    def run_precommit_hooks(self):
        """
        Automatic plug-in/repository maintenance just before a commit is made.
        """
        self.logger.info("Running pre-commit hooks ..")
        plugin_name = self.find_current_plugin()
        self.check_gitignore_file()
        self.update_copyright()
        self.update_vimdoc(plugin_name)

    def check_gitignore_file(self):
        """
        Make sure .gitignore excludes doc/tags.
        """
        self.logger.debug("Checking if .gitignore excludes doc/tags ..")
        if 'doc/tags' not in run('git', 'show', 'HEAD:.gitignore').splitlines():
            if '+doc/tags' not in run('git', 'diff', '--cached', '.gitignore').splitlines():
                self.logger.fatal("The .gitignore file does not exclude doc/tags! Please resolve before committing.")
                sys.exit(1)

    def update_copyright(self):
        """
        Update the year of copyright in README.md when needed.
        """
        contents = []
        updated_copyright = False
        self.logger.debug("Checking if copyright in README is up to date ..")
        with codecs.open('README.md', 'r', 'utf-8') as handle:
            for line in handle:
                line = line.rstrip()
                if line.startswith(u'©'):
                    replacement = u'© %s' % time.strftime('%Y')
                    new_line = re.sub(ur'© \d{4}', replacement, line)
                    if new_line != line:
                        updated_copyright = True
                    line = new_line
                contents.append(line)
        if updated_copyright:
            self.logger.info("Copyright in README was not up to date, changing it now ..")
            with codecs.open('README.md', 'w', 'utf-8') as handle:
                for line in contents:
                    handle.write(u'%s\n' % line)
            run('git', 'add', 'README.md')

    def update_vimdoc(self, plugin_name):
        """
        Generate a Vim help file from the README.md file in the git repository
        of a Vim plug-in using the html2vimdoc.py Python module.
        """
        help_file = self.plugins[plugin_name]['help-file']
        help_path = 'doc/%s' % help_file
        self.logger.info("Converting README.md to %s ..", help_path)
        with open('README.md') as handle:
            markdown = handle.read()
        html = html2vimdoc.markdown_to_html(markdown)
        vimdoc = html2vimdoc.html2vimdoc(html, filename=help_file)
        if not os.path.isdir('doc'):
            os.mkdir('doc')
        with codecs.open(help_path, 'w', 'utf-8') as handle:
            handle.write("%s\n" % vimdoc)
        run('git', 'add', help_path)

    ## Post-commit hooks.

    def run_postcommit_hooks(self):
        """
        Automatic plug-in/repository maintenance just after a commit is made.
        """
        self.logger.info("Running post-commit hooks ..")
        plugin_name = self.find_current_plugin()
        if self.on_master_branch():
            version = self.find_version_in_repository(plugin_name)
            existing_tags = run('git', 'tag').split()
            if version in existing_tags:
                self.logger.debug("Tag %s already exists ..", version)
            else:
                self.logger.info("Creating tag for version %s ..", version)
                run('git', 'tag', version)
        
    ## Miscellaneous methods.

    @property
    def sorted_plugins(self):
        def sort_key(plugin):
            user, repository = plugin['name'].split('/')
            return repository.lower()
        return sorted(self.plugins.values(), key=sort_key)

    def find_current_plugin(self):
        """
        Find the name of the "current" plug-in based on the git repository in
        the current working directory or any of the parent directories (which
        is how git usually works).
        """
        self.logger.debug("Finding current plug-in using 'git config --get remote.origin.url' ..")
        remote_origin_url = run('git', 'config', '--get', 'remote.origin.url')
        self.logger.debug("Raw output: %r", remote_origin_url)
        pattern_match = re.match(r'^git@github\.com:(.+?)\.git$', remote_origin_url)
        if not pattern_match:
            msg = "Failed to parse remote origin URL! (%r)"
            raise Exception, msg % remote_origin_url
        plugin_name = pattern_match.group(1)
        self.logger.info("Found current plug-in: %s", plugin_name)
        if plugin_name not in self.plugins:
            msg = "The repository %r doesn't contain a known Vim plug-in!"
            raise Exception, msg % plugin_name
        return plugin_name

    def on_master_branch(self):
        """
        Check if the master branch is currently checked out.
        """
        output = run('git', 'symbolic-ref', 'HEAD')
        tokens = output.split('/')
        return tokens[-1] == 'master'

    def find_version_on_vim_online(self, plugin_name):
        """
        Find the version of a Vim plug-in that is the highest version number
        that has been released on http://www.vim.org.
        """
        # Find the Vim plug-in on http://www.vim.org.
        script_id = self.plugins[plugin_name]['script-id']
        vim_online_url = 'http://www.vim.org/scripts/script.php?script_id=%s' % script_id
        self.logger.debug("Finding last released version on %s ..", vim_online_url)
        response = urllib.urlopen(vim_online_url)
        # Make sure the response is valid.
        if response.getcode() != 200:
            msg = "URL %r resulted in HTTP %i response!"
            raise Exception, msg % (vim_online_url, response.getcode())
        # Find all previously released versions by scraping the HTML.
        released_versions = []
        for html_row in re.findall('<tr>.+?</tr>', response.read(), re.DOTALL):
            if 'download_script.php' in html_row:
                version_string = re.search('<b>(\d+(?:\.\d+)+)</b>', html_row).group(1)
                version_number = map(int, version_string.split('.'))
                self.logger.log(logging.NOTSET, "Parsed version string %r into %r.", version_string, version_number)
                released_versions.append(version_number)
        # Make sure the scraping is still effective.
        if not released_versions:
            msg = "Failed to find any previous releases on %r!"
            raise Exception, msg % vim_online_url
        self.logger.debug("Found %i previous releases, sorting to find the latest ..", len(released_versions))
        released_versions.sort()
        previous_release = '.'.join([str(d) for d in released_versions[-1]])
        self.logger.info("Found last release on Vim Online: %s", previous_release)
        return previous_release

    def find_version_in_repository(self, plugin_name):
        """
        Find the version of a Vim plug-in that is the highest version number
        that has been committed to the local git repository of the plug-in (the
        version number is embedded as a string in the main auto-load script of
        the plug-in).
        """
        # Find the auto-load script.
        autoload_script = self.plugins[plugin_name]['autoload-script']
        # Find the name of the variable that should contain the version number.
        autoload_path = re.sub(r'^autoload/(.+?)\.vim$', r'\1', autoload_script)
        version_definition = 'let g:%s#version' % autoload_path.replace('/', '#')
        self.logger.debug("Finding local committed version by scanning %s for %r ..", autoload_script, version_definition)
        # Ignore uncommitted changes in the auto-load script.
        script_contents = run('git', 'show', 'HEAD:%s' % autoload_script)
        # Look for the version definition.
        for line in script_contents.splitlines():
            if line.startswith(version_definition):
                tokens = line.split('=', 1)
                last_token = tokens[-1].strip()
                version_string = last_token.strip('\'"')
                self.logger.info("Found last committed version: %s", version_string)
                return version_string
        msg = "Failed to determine last committed version of %s!"
        raise Exception, msg % plugin_name

class ExternalCommandFailed(Exception):

    """
    Exception used to signal that an external command exited with a nonzero
    return code.
    """

    def __init__(self, msg, command):
        super(ExternalCommandFailed, self).__init__(msg)
        self.command = command

def run(*command):
    """
    Run an external process, make sure it exited with a zero return code and
    return the standard output stripped from leading/trailing whitespace.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        msg = "External command %r exited with code %i"
        raise ExternalCommandFailed(msg % (command, process.returncode), command)
    return stdout.strip()

if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et