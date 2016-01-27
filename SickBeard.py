#!/usr/bin/env python2.7
# -*- coding: utf-8 -*
# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of SickRage.
#
# SickRage is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickRage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickRage. If not, see <http://www.gnu.org/licenses/>.


"""
Usage: SickBeard.py [OPTION]...

Options:
  -h,  --help            Prints this message
  -q,  --quiet           Disables logging to console
       --nolaunch        Suppress launching web browser on startup

  -d,  --daemon          Run as double forked daemon (with --quiet --nolaunch)
                         On Windows and MAC, this option is ignored but still
                         applies --quiet --nolaunch
       --pidfile=[FILE]  Combined with --daemon creates a pid file

  -p,  --port=[PORT]     Override default/configured port to listen on
       --datadir=[PATH]  Override folder (full path) as location for
                         storing database, config file, cache, and log files
                         Default SickRage directory
       --config=[FILE]   Override config filename for loading configuration
                         Default config.ini in SickRage directory or
                         location specified with --datadir
       --noresize        Prevent resizing of the banner/posters even if PIL
                         is installed
"""

from __future__ import unicode_literals
from __future__ import print_function

import codecs
import datetime
import getopt
import io
import locale
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

codecs.register(lambda name: codecs.lookup('utf-8') if name == 'cp65001' else None)
sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

if sys.version_info < (2, 7):
    print('Sorry, requires Python 2.7.x')
    sys.exit(1)

# pylint: disable=wrong-import-position
# https://mail.python.org/pipermail/python-dev/2014-September/136300.html
if sys.version_info >= (2, 7, 9):
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context  # pylint: disable=protected-access

import shutil_custom  # pylint: disable=import-error
shutil.copyfile = shutil_custom.copyfile_custom

# Do this before importing sickbeard, to prevent locked files and incorrect import
OLD_TORNADO = os.path.abspath(os.path.join(os.path.dirname(__file__), 'tornado'))
if os.path.isdir(OLD_TORNADO):
    shutil.move(OLD_TORNADO, OLD_TORNADO + '_kill')
    shutil.rmtree(OLD_TORNADO + '_kill')

import sickbeard
from sickbeard import db, logger, network_timezones, failed_history  # , name_cache
from sickbeard.tv import TVShow
from sickbeard.webserveInit import SRWebServer
from sickbeard.event_queue import Events
from configobj import ConfigObj  # pylint: disable=import-error

from sickrage.helper.encoding import ek

# http://bugs.python.org/issue7980#msg221094
THROWAWAY = datetime.datetime.strptime('20110101', '%Y%m%d')

signal.signal(signal.SIGINT, sickbeard.sig_handler)
signal.signal(signal.SIGTERM, sickbeard.sig_handler)


class SickRage(object):
    # pylint: disable=too-many-instance-attributes
    """
    Main SickRage module
    """

    def __init__(self):
        # system event callback for shutdown/restart
        sickbeard.events = Events(self.shutdown)

        # daemon constants
        self.run_as_daemon = False
        self.create_pid = False
        self.pid_file = ''

        # web server constants
        self.web_server = None
        self.forced_port = None
        self.no_launch = False

        self.web_host = '0.0.0.0'
        self.start_port = sickbeard.WEB_PORT
        self.web_options = {}

        self.log_dir = None
        self.console_logging = True

    @staticmethod
    def clear_cache():
        """
        Remove the Mako cache directory
        """
        try:
            cache_folder = ek(os.path.join, sickbeard.CACHE_DIR, 'mako')
            if os.path.isdir(cache_folder):
                shutil.rmtree(cache_folder)
        except Exception:  # pylint: disable=broad-except
            logger.log('Unable to remove the cache/mako directory!', logger.WARNING)  # pylint: disable=no-member

    @staticmethod
    def help_message():
        """
        Print help message for commandline options
        """
        help_msg = __doc__
        help_msg = help_msg.replace('SickBeard.py', sickbeard.MY_FULLNAME)
        help_msg = help_msg.replace('SickRage directory', sickbeard.PROG_DIR)

        return help_msg

    def start(self):  # pylint: disable=too-many-branches,too-many-statements
        """
        Start SickRage
        """
        # do some preliminary stuff
        sickbeard.MY_FULLNAME = ek(os.path.normpath, ek(os.path.abspath, __file__))
        sickbeard.MY_NAME = ek(os.path.basename, sickbeard.MY_FULLNAME)
        sickbeard.PROG_DIR = ek(os.path.dirname, sickbeard.MY_FULLNAME)
        sickbeard.DATA_DIR = sickbeard.PROG_DIR
        sickbeard.MY_ARGS = sys.argv[1:]

        try:
            locale.setlocale(locale.LC_ALL, '')
            sickbeard.SYS_ENCODING = locale.getpreferredencoding()
        except (locale.Error, IOError):
            sickbeard.SYS_ENCODING = 'UTF-8'

        # pylint: disable=no-member
        if not sickbeard.SYS_ENCODING or sickbeard.SYS_ENCODING.lower() in ('ansi_x3.4-1968', 'us-ascii', 'ascii', 'charmap') or \
                (sys.platform.startswith('win') and sys.getwindowsversion()[0] >= 6 and str(getattr(sys.stdout, 'device', sys.stdout).encoding).lower() in ('cp65001', 'charmap')):
            sickbeard.SYS_ENCODING = 'UTF-8'

        # TODO: Continue working on making this unnecessary, this hack creates all sorts of hellish problems
        if not hasattr(sys, 'setdefaultencoding'):
            reload(sys)

        try:
            # On non-unicode builds this will raise an AttributeError, if encoding type is not valid it throws a LookupError
            sys.setdefaultencoding(sickbeard.SYS_ENCODING)  # pylint: disable=no-member
        except (AttributeError, LookupError):
            sys.exit('Sorry, you MUST add the SickRage folder to the PYTHONPATH environment variable\n'
                     'or find another way to force Python to use %s for string encoding.' % sickbeard.SYS_ENCODING)

        # Need console logging for SickBeard.py and SickBeard-console.exe
        self.console_logging = (not hasattr(sys, 'frozen')) or (sickbeard.MY_NAME.lower().find('-console') > 0)

        # Rename the main thread
        threading.currentThread().name = 'MAIN'

        try:
            opts, _ = getopt.getopt(
                sys.argv[1:], 'hqdp::',
                ['help', 'quiet', 'nolaunch', 'daemon', 'pidfile=', 'port=', 'datadir=', 'config=', 'noresize']
            )
        except getopt.GetoptError:
            sys.exit(self.help_message())

        for option, value in opts:
            # Prints help message
            if option in ('-h', '--help'):
                sys.exit(self.help_message())

            # For now we'll just silence the logging
            if option in ('-q', '--quiet'):
                self.console_logging = False

            # Suppress launching web browser
            # Needed for OSes without default browser assigned
            # Prevent duplicate browser window when restarting in the app
            if option in ('--nolaunch',):
                self.no_launch = True

            # Override default/configured port
            if option in ('-p', '--port'):
                try:
                    self.forced_port = int(value)
                except ValueError:
                    sys.exit('Port: %s is not a number. Exiting.' % value)

            # Run as a double forked daemon
            if option in ('-d', '--daemon'):
                self.run_as_daemon = True
                # When running as daemon disable console_logging and don't start browser
                self.console_logging = False
                self.no_launch = True

                if sys.platform == 'win32' or sys.platform == 'darwin':
                    self.run_as_daemon = False

            # Write a pid file if requested
            if option in ('--pidfile',):
                self.create_pid = True
                self.pid_file = str(value)

                # If the pid file already exists, SickRage may still be running, so exit
                if ek(os.path.exists, self.pid_file):
                    sys.exit('PID file: %s already exists. Exiting.' % self.pid_file)

            # Specify folder to load the config file from
            if option in ('--config',):
                sickbeard.CONFIG_FILE = ek(os.path.abspath, value)

            # Specify folder to use as the data directory
            if option in ('--datadir',):
                sickbeard.DATA_DIR = ek(os.path.abspath, value)

            # Prevent resizing of the banner/posters even if PIL is installed
            if option in ('--noresize',):
                sickbeard.NO_RESIZE = True

        # The pid file is only useful in daemon mode, make sure we can write the file properly
        if self.create_pid:
            if self.run_as_daemon:
                pid_dir = ek(os.path.dirname, self.pid_file)
                if not ek(os.access, pid_dir, os.F_OK):
                    sys.exit('PID dir: %s doesn\'t exist. Exiting.' % pid_dir)
                if not ek(os.access, pid_dir, os.W_OK):
                    sys.exit('PID dir: %s must be writable (write permissions). Exiting.' % pid_dir)

            else:
                if self.console_logging:
                    sys.stdout.write('Not running in daemon mode. PID file creation disabled.\n')

                self.create_pid = False

        # If they don't specify a config file then put it in the data dir
        if not sickbeard.CONFIG_FILE:
            sickbeard.CONFIG_FILE = ek(os.path.join, sickbeard.DATA_DIR, 'config.ini')

        # Make sure that we can create the data dir
        if not ek(os.access, sickbeard.DATA_DIR, os.F_OK):
            try:
                ek(os.makedirs, sickbeard.DATA_DIR, 0o744)
            except os.error:
                raise SystemExit('Unable to create data directory: %s' % sickbeard.DATA_DIR)

        # Make sure we can write to the data dir
        if not ek(os.access, sickbeard.DATA_DIR, os.W_OK):
            raise SystemExit('Data directory must be writeable: %s' % sickbeard.DATA_DIR)

        # Rename sickrage.db to sickbeard.db
        self.rename_db()

        # Make sure we can write to the config file
        if not ek(os.access, sickbeard.CONFIG_FILE, os.W_OK):
            if ek(os.path.isfile, sickbeard.CONFIG_FILE):
                raise SystemExit('Config file must be writeable: %s' % sickbeard.CONFIG_FILE)
            elif not ek(os.access, ek(os.path.dirname, sickbeard.CONFIG_FILE), os.W_OK):
                raise SystemExit('Config file root dir must be writeable: %s' % ek(os.path.dirname, sickbeard.CONFIG_FILE))

        ek(os.chdir, sickbeard.DATA_DIR)

        # Check if we need to perform a restore first
        restore_dir = ek(os.path.join, sickbeard.DATA_DIR, 'restore')
        if ek(os.path.exists, restore_dir):
            success = self.restore_db(restore_dir, sickbeard.DATA_DIR)
            if self.console_logging:
                sys.stdout.write('Restore: restoring DB and config.ini %s!\n' % ('FAILED', 'SUCCESSFUL')[success])

        # Load the config and publish it to the sickbeard package
        if self.console_logging and not ek(os.path.isfile, sickbeard.CONFIG_FILE):
            sys.stdout.write('Unable to find %s, all settings will be default!\n' % sickbeard.CONFIG_FILE)

        sickbeard.CFG = ConfigObj(sickbeard.CONFIG_FILE)

        # Initialize the config and our threads
        sickbeard.initialize(consoleLogging=self.console_logging)

        if self.run_as_daemon:
            self.daemonize()

        # Get PID
        sickbeard.PID = os.getpid()

        # Build from the DB to start with
        self.load_shows_from_db()

        logger.log('Starting SickRage [%s] from \'%s\'' % (sickbeard.BRANCH, sickbeard.CONFIG_FILE))

        self.clear_cache()

        if self.forced_port:
            logger.log('Forcing web server to port %s' % self.forced_port)
            self.start_port = self.forced_port
        else:
            self.start_port = sickbeard.WEB_PORT

        if sickbeard.WEB_LOG:
            self.log_dir = sickbeard.LOG_DIR
        else:
            self.log_dir = None

        # sickbeard.WEB_HOST is available as a configuration value in various
        # places but is not configurable. It is supported here for historic reasons.
        if sickbeard.WEB_HOST and sickbeard.WEB_HOST != '0.0.0.0':
            self.web_host = sickbeard.WEB_HOST
        else:
            self.web_host = '' if sickbeard.WEB_IPV6 else '0.0.0.0'

        # web server options
        self.web_options = {
            'port': int(self.start_port),
            'host': self.web_host,
            'data_root': ek(os.path.join, sickbeard.PROG_DIR, 'gui', sickbeard.GUI_NAME),
            'web_root': sickbeard.WEB_ROOT,
            'log_dir': self.log_dir,
            'username': sickbeard.WEB_USERNAME,
            'password': sickbeard.WEB_PASSWORD,
            'enable_https': sickbeard.ENABLE_HTTPS,
            'handle_reverse_proxy': sickbeard.HANDLE_REVERSE_PROXY,
            'https_cert': ek(os.path.join, sickbeard.PROG_DIR, sickbeard.HTTPS_CERT),
            'https_key': ek(os.path.join, sickbeard.PROG_DIR, sickbeard.HTTPS_KEY),
        }

        # start web server
        self.web_server = SRWebServer(self.web_options)
        self.web_server.start()

        # Fire up all our threads
        sickbeard.start()

        # Build internal name cache
        # name_cache.buildNameCache()

        # Pre-populate network timezones, it isn't thread safe
        network_timezones.update_network_dict()

        # sure, why not?
        if sickbeard.USE_FAILED_DOWNLOADS:
            failed_history.trimHistory()

        # # Check for metadata indexer updates for shows (Disabled until we use api)
        # sickbeard.showUpdateScheduler.forceRun()

        # Launch browser
        if sickbeard.LAUNCH_BROWSER and not (self.no_launch or self.run_as_daemon):
            sickbeard.launchBrowser('https' if sickbeard.ENABLE_HTTPS else 'http', self.start_port, sickbeard.WEB_ROOT)

        # main loop
        while True:
            time.sleep(1)

    def daemonize(self):
        """
        Fork off as a daemon
        """
        # pylint: disable=no-member,protected-access
        # An object is accessed for a non-existent member.
        # Access to a protected member of a client class
        # Make a non-session-leader child process
        try:
            pid = os.fork()  # @UndefinedVariable - only available in UNIX
            if pid != 0:
                os._exit(0)
        except OSError as error_message:
            sys.stderr.write('fork #1 failed: %d (%s)\n' % (error_message.errno, error_message.strerror))
            sys.exit(1)

        os.setsid()  # @UndefinedVariable - only available in UNIX

        # https://github.com/SickRage/sickrage-issues/issues/2969
        # http://www.microhowto.info/howto/cause_a_process_to_become_a_daemon_in_c.html#idp23920
        # https://www.safaribooksonline.com/library/view/python-cookbook/0596001673/ch06s08.html
        # Previous code simply set the umask to whatever it was because it was ANDing instead of OR-ing
        # Daemons traditionally run with umask 0 anyways and this should not have repercussions
        os.umask(0)

        # Make the child a session-leader by detaching from the terminal
        try:
            pid = os.fork()  # @UndefinedVariable - only available in UNIX
            if pid != 0:
                os._exit(0)
        except OSError as error_message:
            sys.stderr.write('fork #2 failed: %d (%s)\n' % (error_message.errno, error_message.strerror))
            sys.exit(1)

        # Write pid
        if self.create_pid:
            pid = os.getpid()
            logger.log('Writing PID: %s to %s' % (pid, self.pid_file))

            try:
                with io.open(self.pid_file, 'w') as f_pid:
                    f_pid.write('%s\n' % pid)
            except EnvironmentError as error_message:
                logger.log_error_and_exit('Unable to write PID file: %s Error: %s [%s]' % (self.pid_file, error_message.strerror, error_message.errno))

        # Redirect all output
        sys.stdout.flush()
        sys.stderr.flush()

        devnull = getattr(os, 'devnull', '/dev/null')
        stdin = file(devnull)
        stdout = file(devnull, 'a+')
        stderr = file(devnull, 'a+')

        os.dup2(stdin.fileno(), getattr(sys.stdin, 'device', sys.stdin).fileno())
        os.dup2(stdout.fileno(), getattr(sys.stdout, 'device', sys.stdout).fileno())
        os.dup2(stderr.fileno(), getattr(sys.stderr, 'device', sys.stderr).fileno())

    @staticmethod
    def remove_pid_file(pid_file):
        """
        Remove pid file

        :param pid_file: to remove
        :return:
        """
        try:
            if ek(os.path.exists, pid_file):
                ek(os.remove, pid_file)
        except EnvironmentError:
            return False

        return True

    @staticmethod
    def rename_db():
        """
        move sickrage.db to sickbeard.db
        """
        old = sickbeard.db.dbFilename(filename="sickrage.db")
        new = sickbeard.db.dbFilename()
        if os.path.exists(old) and not os.path.exists(new):
            logger.log('Renaming {} to {}'.format(old, new), logger.DEBUG)  # pylint: disable=no-member
            os.rename(old, new)

    @staticmethod
    def load_shows_from_db():
        """
        Populates the showList with shows from the database
        """
        logger.log('Loading initial show list', logger.DEBUG)  # pylint: disable=no-member

        main_db_con = db.DBConnection()
        sql_results = main_db_con.select('SELECT indexer, indexer_id, location FROM tv_shows;')

        sickbeard.showList = []
        for sql_show in sql_results:
            try:
                cur_show = TVShow(sql_show[b'indexer'], sql_show[b'indexer_id'])
                cur_show.nextEpisode()
                sickbeard.showList.append(cur_show)
            except Exception as error_msg:  # pylint: disable=broad-except
                logger.log('There was an error creating the show in %s: %s' %  # pylint: disable=no-member
                           (sql_show[b'location'], str(error_msg).decode()), logger.ERROR)
                logger.log(traceback.format_exc(), logger.DEBUG)  # pylint: disable=no-member

    @staticmethod
    def restore_db(src_dir, dst_dir):
        """
        Restore the Database from a backup

        :param src_dir: Directory containing backup
        :param dst_dir: Directory to restore to
        :return:
        """
        try:
            files_list = ['sickrage.db', 'sickbeard.db', 'config.ini', 'failed.db', 'cache.db']

            for filename in files_list:
                src_file = ek(os.path.join, src_dir, filename)
                if os.path.exists(src_file):
                    dst_file = ek(os.path.join, dst_dir, filename).replace('sickrage.db', 'sickbeard.db')
                    bak_file = ek(os.path.join, dst_dir, '%s.bak-%s' % (filename, datetime.datetime.now().strftime('%Y%m%d_%H%M%S')))
                    if ek(os.path.isfile, dst_file):
                        shutil.move(dst_file, bak_file)
                    shutil.move(src_file, dst_file)
            return True
        except Exception:  # pylint: disable=broad-except
            return False

    def shutdown(self, event):
        """
        Shut down SickRage

        :param event: Type of shutdown event, used to see if restart required
        """
        if sickbeard.started:
            sickbeard.halt()  # stop all tasks
            sickbeard.saveAll()  # save all shows to DB

            # shutdown web server
            if self.web_server:
                logger.log('Shutting down Tornado')  # pylint: disable=no-member
                self.web_server.shutDown()

                try:
                    self.web_server.join(10)
                except Exception:  # pylint: disable=broad-except
                    pass

            self.clear_cache()  # Clean cache

            # if run as daemon delete the pid file
            if self.run_as_daemon and self.create_pid:
                self.remove_pid_file(self.pid_file)

            if event == sickbeard.event_queue.Events.SystemEvent.RESTART:
                install_type = sickbeard.versionCheckScheduler.action.install_type

                popen_list = []

                if install_type in ('git', 'source'):
                    popen_list = [sys.executable, sickbeard.MY_FULLNAME]
                elif install_type == 'win':
                    logger.log('You are using a binary Windows build of SickRage. '  # pylint: disable=no-member
                               'Please switch to using git.', logger.ERROR)

                if popen_list and not sickbeard.NO_RESTART:
                    popen_list += sickbeard.MY_ARGS
                    if '--nolaunch' not in popen_list:
                        popen_list += ['--nolaunch']
                    logger.log('Restarting SickRage with %s' % popen_list)  # pylint: disable=no-member
                    # shutdown the logger to make sure it's released the logfile BEFORE it restarts SR.
                    logger.shutdown()  # pylint: disable=no-member
                    subprocess.Popen(popen_list, cwd=os.getcwd())

        # Make sure the logger has stopped, just in case
        logger.shutdown()  # pylint: disable=no-member
        os._exit(0)  # pylint: disable=protected-access


if __name__ == '__main__':
    # start SickRage
    SickRage().start()
