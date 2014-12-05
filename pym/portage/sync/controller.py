# Copyright 2014 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

from __future__ import print_function


import sys
import logging
import grp
import pwd

import portage
from portage import os
from portage.progress import ProgressBar
#from portage.emaint.defaults import DEFAULT_OPTIONS
#from portage.util._argparse import ArgumentParser
from portage.util import writemsg_level
from portage.output import create_color_func
good = create_color_func("GOOD")
bad = create_color_func("BAD")
warn = create_color_func("WARN")
from portage.package.ebuild.doebuild import _check_temp_dir
from portage.metadata import action_metadata
from portage import OrderedDict
from portage import _unicode_decode
from portage import util


class TaskHandler(object):
	"""Handles the running of the tasks it is given
	"""

	def __init__(self, show_progress_bar=True, verbose=True, callback=None):
		self.show_progress_bar = show_progress_bar
		self.verbose = verbose
		self.callback = callback
		self.isatty = os.environ.get('TERM') != 'dumb' and sys.stdout.isatty()
		self.progress_bar = ProgressBar(self.isatty, title="Portage-Sync", max_desc_length=27)


	def run_tasks(self, tasks, func, status=None, verbose=True, options=None):
		"""Runs the module tasks"""
		# Ensure we have a task and function
		assert(tasks)
		assert(func)
		for task in tasks:
			inst = task()
			show_progress = self.show_progress_bar and self.isatty
			# check if the function is capable of progressbar
			# and possibly override it off
			if show_progress and hasattr(inst, 'can_progressbar'):
				show_progress = inst.can_progressbar(func)
			if show_progress:
				self.progress_bar.reset()
				self.progress_bar.set_label(func + " " + inst.name())
				onProgress = self.progress_bar.start()
			else:
				onProgress = None
			kwargs = {
				'onProgress': onProgress,
				# pass in a copy of the options so a module can not pollute or change
				# them for other tasks if there is more to do.
				'options': options.copy()
				}
			result = getattr(inst, func)(**kwargs)
			if show_progress:
				# make sure the final progress is displayed
				self.progress_bar.display()
				print()
				self.progress_bar.stop()
			if self.callback:
				self.callback(result)


def print_results(results):
	if results:
		print()
		print("\n".join(results))
		print("\n")


class SyncManager(object):
	'''Main sync control module'''

	def __init__(self, settings, logger):
		self.settings = settings
		self.logger = logger
		# Similar to emerge, sync needs a default umask so that created
		# files have sane permissions.
		os.umask(0o22)

		self.module_controller = portage.sync.module_controller
		self.module_names = self.module_controller.module_names
		self.hooks = {}
		for _dir in ["repo.postsync.d", "postsync.d"]:
			postsync_dir = os.path.join(self.settings["PORTAGE_CONFIGROOT"],
				portage.USER_CONFIG_PATH, _dir)
			hooks = OrderedDict()
			for filepath in util._recursive_file_list(postsync_dir):
				name = filepath.split(postsync_dir)[1].lstrip(os.sep)
				if os.access(filepath, os.X_OK):
					hooks[filepath] = name
				else:
					writemsg_level(" %s %s hook: '%s' is not executable\n"
						% (warn("*"), _dir, _unicode_decode(name),),
						level=logging.WARN, noiselevel=2)
			self.hooks[_dir] = hooks


	def get_module_descriptions(self, mod):
		desc = self.module_controller.get_func_descriptions(mod)
		if desc:
			return desc
		return []


	def sync(self, emerge_config=None, repo=None, callback=None):
		self.emerge_config = emerge_config
		self.callback = callback or self._sync_callback
		self.repo = repo
		self.exitcode = 1
		if repo.sync_type in self.module_names[1:]:
			tasks = [self.module_controller.get_class(repo.sync_type)]
		else:
			msg = "\n%s: Sync module '%s' is not an installed/known type'\n" \
				% (bad("ERROR"), repo.sync_type)
			return self.exitcode, msg

		rval = self.pre_sync(repo)
		if rval != os.EX_OK:
			return rval, None

		# need to pass the kwargs dict to the modules
		# so they are available if needed.
		task_opts = {
			'emerge_config': emerge_config,
			'logger': self.logger,
			'portdb': self.trees[self.settings['EROOT']]['porttree'].dbapi,
			'repo': repo,
			'settings': self.settings,
			'spawn_kwargs': self.spawn_kwargs,
			'usersync_uid': self.usersync_uid,
			'xterm_titles': self.xterm_titles,
			}
		func = 'sync'
		status = None
		taskmaster = TaskHandler(callback=self.do_callback)
		taskmaster.run_tasks(tasks, func, status, options=task_opts)

		self.perform_post_sync_hook(repo.name, repo.sync_uri, repo.location)

		return self.exitcode, None


	def do_callback(self, result):
		#print("result:", result, "callback()", self.callback)
		exitcode, updatecache_flg = result
		self.exitcode = exitcode
		if self.callback:
			self.callback(exitcode, updatecache_flg)
		return


	def perform_post_sync_hook(self, reponame, dosyncuri='', repolocation=''):
		succeeded = os.EX_OK
		if reponame:
			_hooks = self.hooks["repo.postsync.d"]
		else:
			_hooks = self.hooks["postsync.d"]
		for filepath in _hooks:
			writemsg_level("Spawning post_sync hook: %s\n"
				% (_unicode_decode(_hooks[filepath])),
				level=logging.ERROR, noiselevel=4)
			retval = portage.process.spawn([filepath,
				reponame, dosyncuri, repolocation], env=self.settings.environ())
			if retval != os.EX_OK:
				writemsg_level(" %s Spawn failed for: %s, %s\n" % (bad("*"),
					_unicode_decode(_hooks[filepath]), filepath),
					level=logging.ERROR, noiselevel=-1)
				succeeded = retval
		return succeeded


	def pre_sync(self, repo):
		self.settings, self.trees, self.mtimedb = self.emerge_config
		self.xterm_titles = "notitles" not in self.settings.features
		msg = ">>> Synchronization of repository '%s' located in '%s'..." \
			% (repo.name, repo.location)
		self.logger(self.xterm_titles, msg)
		writemsg_level(msg + "\n")
		try:
			st = os.stat(repo.location)
		except OSError:
			st = None
		if st is None:
			writemsg_level(">>> '%s' not found, creating it."
				% _unicode_decode(repo.location))
			portage.util.ensure_dirs(repo.location, mode=0o755)
			st = os.stat(repo.location)

		self.usersync_uid = None
		spawn_kwargs = {}
		spawn_kwargs["env"] = self.settings.environ()
		if repo.sync_user is not None:
			def get_sync_user_data(sync_user):
				user = None
				group = None
				home = None

				spl = sync_user.split(':', 1)
				if spl[0]:
					username = spl[0]
					try:
						try:
							pw = pwd.getpwnam(username)
						except KeyError:
							pw = pwd.getpwuid(int(username))
					except (ValueError, KeyError):
						writemsg("!!! User '%s' invalid or does not exist\n"
								% username, noiselevel=-1)
						return (user, group, home)
					user = pw.pw_uid
					group = pw.pw_gid
					home = pw.pw_dir

				if len(spl) > 1:
					groupname = spl[1]
					try:
						try:
							gp = grp.getgrnam(groupname)
						except KeyError:
							pw = grp.getgrgid(int(groupname))
					except (ValueError, KeyError):
						writemsg("!!! Group '%s' invalid or does not exist\n"
								% groupname, noiselevel=-1)
						return (user, group, home)

					group = gp.gr_gid

				return (user, group, home)

			# user or user:group
			(uid, gid, home) = get_sync_user_data(repo.sync_user)
			if uid is not None:
				spawn_kwargs["uid"] = uid
				self.usersync_uid = uid
			if gid is not None:
				spawn_kwargs["gid"] = gid
				spawn_kwargs["groups"] = [gid]
			if home is not None:
				spawn_kwargs["env"]["HOME"] = home
		elif ('usersync' in self.settings.features and
			portage.data.secpass >= 2 and
			(st.st_uid != os.getuid() and st.st_mode & 0o700 or
			st.st_gid != os.getgid() and st.st_mode & 0o070)):
			try:
				homedir = pwd.getpwuid(st.st_uid).pw_dir
			except KeyError:
				pass
			else:
				# Drop privileges when syncing, in order to match
				# existing uid/gid settings.
				self.usersync_uid = st.st_uid
				spawn_kwargs["uid"]    = st.st_uid
				spawn_kwargs["gid"]    = st.st_gid
				spawn_kwargs["groups"] = [st.st_gid]
				spawn_kwargs["env"]["HOME"] = homedir
				umask = 0o002
				if not st.st_mode & 0o020:
					umask = umask | 0o020
				spawn_kwargs["umask"] = umask
		# override the defaults when sync_umask is set
		if repo.sync_umask is not None:
			spawn_kwargs["umask"] = int(repo.sync_umask, 8)
		self.spawn_kwargs = spawn_kwargs

		if self.usersync_uid is not None:
			# PORTAGE_TMPDIR is used below, so validate it and
			# bail out if necessary.
			rval = _check_temp_dir(self.settings)
			if rval != os.EX_OK:
				return rval

		os.umask(0o022)
		return os.EX_OK


	def _sync_callback(self, exitcode, updatecache_flg):
		if updatecache_flg and "metadata-transfer" not in self.settings.features:
			updatecache_flg = False

		if updatecache_flg and \
			os.path.exists(os.path.join(
			self.repo.location, 'metadata', 'md5-cache')):

			# Only update cache for repo.location since that's
			# the only one that's been synced here.
			action_metadata(self.settings, self.portdb, self.emerge_config.opts,
				porttrees=[self.repo.location])
