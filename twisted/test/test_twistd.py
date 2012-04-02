# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.application.app} and L{twisted.scripts.twistd}.
"""

import signal, inspect, errno

import os, sys, StringIO

try:
    import pwd, grp
except ImportError:
    pwd = grp = None

try:
    import cPickle as pickle
except ImportError:
    import pickle

from zope.interface import implements
from zope.interface.verify import verifyObject

from twisted.trial import unittest

from twisted import plugin
from twisted.application.service import IServiceMaker
from twisted.application import service, app, reactors
from twisted.scripts import twistd
from twisted.python import log
from twisted.python.usage import UsageError
from twisted.python.log import ILogObserver
from twisted.python.versions import Version
from twisted.python.components import Componentized
from twisted.internet.defer import Deferred
from twisted.python.fakepwd import UserDatabase

try:
    from twisted.python import syslog
except ImportError:
    syslog = None

try:
    from twisted.scripts import _twistd_unix
except ImportError:
    _twistd_unix = None
else:
    from twisted.scripts._twistd_unix import UnixApplicationRunner
    from twisted.scripts._twistd_unix import UnixAppLogger

try:
    import profile
except ImportError:
    profile = None

try:
    import hotshot
    import hotshot.stats
except (ImportError, SystemExit):
    # For some reasons, hotshot.stats seems to raise SystemExit on some
    # distributions, probably when considered non-free.  See the import of
    # this module in twisted.application.app for more details.
    hotshot = None

try:
    import pstats
    import cProfile
except ImportError:
    cProfile = None

if getattr(os, 'setuid', None) is None:
    setuidSkip = "Platform does not support --uid/--gid twistd options."
else:
    setuidSkip = None


def patchUserDatabase(patch, user, uid, group, gid):
    """
    Patch L{pwd.getpwnam} so that it behaves as though only one user exists
    and patch L{grp.getgrnam} so that it behaves as though only one group
    exists.

    @param patch: A function like L{TestCase.patch} which will be used to
        install the fake implementations.

    @type user: C{str}
    @param user: The name of the single user which will exist.

    @type uid: C{int}
    @param uid: The UID of the single user which will exist.

    @type group: C{str}
    @param group: The name of the single user which will exist.

    @type gid: C{int}
    @param gid: The GID of the single group which will exist.
    """
    # Try not to be an unverified fake, but try not to depend on quirks of
    # the system either (eg, run as a process with a uid and gid which
    # equal each other, and so doesn't reliably test that uid is used where
    # uid should be used and gid is used where gid should be used). -exarkun
    pwent = pwd.getpwuid(os.getuid())
    grent = grp.getgrgid(os.getgid())

    database = UserDatabase()
    database.addUser(
        user, pwent.pw_passwd, uid, pwent.pw_gid,
        pwent.pw_gecos, pwent.pw_dir, pwent.pw_shell)

    def getgrnam(name):
        result = list(grent)
        result[result.index(grent.gr_name)] = group
        result[result.index(grent.gr_gid)] = gid
        result = tuple(result)
        return {group: result}[name]

    patch(pwd, "getpwnam", database.getpwnam)
    patch(grp, "getgrnam", getgrnam)



class MockServiceMaker(object):
    """
    A non-implementation of L{twisted.application.service.IServiceMaker}.
    """
    tapname = 'ueoa'

    def makeService(self, options):
        """
        Take a L{usage.Options} instance and return a
        L{service.IService} provider.
        """
        self.options = options
        self.service = service.Service()
        return self.service



class CrippledAppLogger(app.AppLogger):
    """
    @see: CrippledApplicationRunner.
    """

    def start(self, application):
        pass



class CrippledApplicationRunner(twistd._SomeApplicationRunner):
    """
    An application runner that cripples the platform-specific runner and
    nasty side-effect-having code so that we can use it without actually
    running any environment-affecting code.
    """
    loggerFactory = CrippledAppLogger

    def preApplication(self):
        pass


    def postApplication(self):
        pass



class ServerOptionsTest(unittest.TestCase):
    """
    Non-platform-specific tests for the pltaform-specific ServerOptions class.
    """
    def test_subCommands(self):
        """
        subCommands is built from IServiceMaker plugins, and is sorted
        alphabetically.
        """
        class FakePlugin(object):
            def __init__(self, name):
                self.tapname = name
                self._options = 'options for ' + name
                self.description = 'description of ' + name

            def options(self):
                return self._options

        apple = FakePlugin('apple')
        banana = FakePlugin('banana')
        coconut = FakePlugin('coconut')
        donut = FakePlugin('donut')

        def getPlugins(interface):
            self.assertEqual(interface, IServiceMaker)
            yield coconut
            yield banana
            yield donut
            yield apple

        config = twistd.ServerOptions()
        self.assertEqual(config._getPlugins, plugin.getPlugins)
        config._getPlugins = getPlugins

        # "subCommands is a list of 4-tuples of (command name, command
        # shortcut, parser class, documentation)."
        subCommands = config.subCommands
        expectedOrder = [apple, banana, coconut, donut]

        for subCommand, expectedCommand in zip(subCommands, expectedOrder):
            name, shortcut, parserClass, documentation = subCommand
            self.assertEqual(name, expectedCommand.tapname)
            self.assertEqual(shortcut, None)
            self.assertEqual(parserClass(), expectedCommand._options),
            self.assertEqual(documentation, expectedCommand.description)


    def test_sortedReactorHelp(self):
        """
        Reactor names are listed alphabetically by I{--help-reactors}.
        """
        class FakeReactorInstaller(object):
            def __init__(self, name):
                self.shortName = 'name of ' + name
                self.description = 'description of ' + name

        apple = FakeReactorInstaller('apple')
        banana = FakeReactorInstaller('banana')
        coconut = FakeReactorInstaller('coconut')
        donut = FakeReactorInstaller('donut')

        def getReactorTypes():
            yield coconut
            yield banana
            yield donut
            yield apple

        config = twistd.ServerOptions()
        self.assertEqual(config._getReactorTypes, reactors.getReactorTypes)
        config._getReactorTypes = getReactorTypes
        config.messageOutput = StringIO.StringIO()

        self.assertRaises(SystemExit, config.parseOptions, ['--help-reactors'])
        helpOutput = config.messageOutput.getvalue()
        indexes = []
        for reactor in apple, banana, coconut, donut:
            def getIndex(s):
                self.assertIn(s, helpOutput)
                indexes.append(helpOutput.index(s))

            getIndex(reactor.shortName)
            getIndex(reactor.description)

        self.assertEqual(
            indexes, sorted(indexes),
            'reactor descriptions were not in alphabetical order: %r' % (
                helpOutput,))


    def test_postOptionsSubCommandCausesNoSave(self):
        """
        postOptions should set no_save to True when a subcommand is used.
        """
        config = twistd.ServerOptions()
        config.subCommand = 'ueoa'
        config.postOptions()
        self.assertEqual(config['no_save'], True)


    def test_postOptionsNoSubCommandSavesAsUsual(self):
        """
        If no sub command is used, postOptions should not touch no_save.
        """
        config = twistd.ServerOptions()
        config.postOptions()
        self.assertEqual(config['no_save'], False)


    def test_listAllProfilers(self):
        """
        All the profilers that can be used in L{app.AppProfiler} are listed in
        the help output.
        """
        config = twistd.ServerOptions()
        helpOutput = str(config)
        for profiler in app.AppProfiler.profilers:
            self.assertIn(profiler, helpOutput)


    def test_defaultUmask(self):
        """
        The default value for the C{umask} option is C{None}.
        """
        config = twistd.ServerOptions()
        self.assertEqual(config['umask'], None)


    def test_umask(self):
        """
        The value given for the C{umask} option is parsed as an octal integer
        literal.
        """
        config = twistd.ServerOptions()
        config.parseOptions(['--umask', '123'])
        self.assertEqual(config['umask'], 83)
        config.parseOptions(['--umask', '0123'])
        self.assertEqual(config['umask'], 83)


    def test_invalidUmask(self):
        """
        If a value is given for the C{umask} option which cannot be parsed as
        an integer, L{UsageError} is raised by L{ServerOptions.parseOptions}.
        """
        config = twistd.ServerOptions()
        self.assertRaises(UsageError, config.parseOptions, ['--umask', 'abcdef'])

    if _twistd_unix is None:
        msg = "twistd unix not available"
        test_defaultUmask.skip = test_umask.skip = test_invalidUmask.skip = msg


    def test_unimportableConfiguredLogObserver(self):
        """
        C{--logger} with an unimportable module raises a L{UsageError}.
        """
        config = twistd.ServerOptions()
        e = self.assertRaises(UsageError, config.parseOptions,
                          ['--logger', 'no.such.module.I.hope'])
        self.assertTrue(e.args[0].startswith(
                "Logger 'no.such.module.I.hope' could not be imported: "
                "'no.such.module.I.hope' does not name an object"))
        self.assertNotIn('\n', e.args[0])


    def test_badAttributeWithConfiguredLogObserver(self):
        """
        C{--logger} with a non-existent object raises a L{UsageError}.
        """
        config = twistd.ServerOptions()
        e = self.assertRaises(UsageError, config.parseOptions,
                              ["--logger", "twisted.test.test_twistd.FOOBAR"])
        self.assertTrue(e.args[0].startswith(
                "Logger 'twisted.test.test_twistd.FOOBAR' could not be "
                "imported: 'module' object has no attribute 'FOOBAR'"))
        self.assertNotIn('\n', e.args[0])



class TapFileTest(unittest.TestCase):
    """
    Test twistd-related functionality that requires a tap file on disk.
    """

    def setUp(self):
        """
        Create a trivial Application and put it in a tap file on disk.
        """
        self.tapfile = self.mktemp()
        f = file(self.tapfile, 'wb')
        pickle.dump(service.Application("Hi!"), f)
        f.close()


    def test_createOrGetApplicationWithTapFile(self):
        """
        Ensure that the createOrGetApplication call that 'twistd -f foo.tap'
        makes will load the Application out of foo.tap.
        """
        config = twistd.ServerOptions()
        config.parseOptions(['-f', self.tapfile])
        application = CrippledApplicationRunner(config).createOrGetApplication()
        self.assertEqual(service.IService(application).name, 'Hi!')



class TestLoggerFactory(object):
    """
    A logger factory for L{TestApplicationRunner}.
    """

    def __init__(self, runner):
        self.runner = runner


    def start(self, application):
        """
        Save the logging start on the C{runner} instance.
        """
        self.runner.order.append("log")
        self.runner.hadApplicationLogObserver = hasattr(self.runner,
                                                        'application')


    def stop(self):
        """
        Don't log anything.
        """



class TestApplicationRunner(app.ApplicationRunner):
    """
    An ApplicationRunner which tracks the environment in which its methods are
    called.
    """

    def __init__(self, options):
        app.ApplicationRunner.__init__(self, options)
        self.order = []
        self.logger = TestLoggerFactory(self)


    def preApplication(self):
        self.order.append("pre")
        self.hadApplicationPreApplication = hasattr(self, 'application')


    def postApplication(self):
        self.order.append("post")
        self.hadApplicationPostApplication = hasattr(self, 'application')



class ApplicationRunnerTest(unittest.TestCase):
    """
    Non-platform-specific tests for the platform-specific ApplicationRunner.
    """
    def setUp(self):
        config = twistd.ServerOptions()
        self.serviceMaker = MockServiceMaker()
        # Set up a config object like it's been parsed with a subcommand
        config.loadedPlugins = {'test_command': self.serviceMaker}
        config.subOptions = object()
        config.subCommand = 'test_command'
        self.config = config


    def test_applicationRunnerGetsCorrectApplication(self):
        """
        Ensure that a twistd plugin gets used in appropriate ways: it
        is passed its Options instance, and the service it returns is
        added to the application.
        """
        arunner = CrippledApplicationRunner(self.config)
        arunner.run()

        self.assertIdentical(
            self.serviceMaker.options, self.config.subOptions,
            "ServiceMaker.makeService needs to be passed the correct "
            "sub Command object.")
        self.assertIdentical(
            self.serviceMaker.service,
            service.IService(arunner.application).services[0],
            "ServiceMaker.makeService's result needs to be set as a child "
            "of the Application.")


    def test_preAndPostApplication(self):
        """
        Test thet preApplication and postApplication methods are
        called by ApplicationRunner.run() when appropriate.
        """
        s = TestApplicationRunner(self.config)
        s.run()
        self.assertFalse(s.hadApplicationPreApplication)
        self.assertTrue(s.hadApplicationPostApplication)
        self.assertTrue(s.hadApplicationLogObserver)
        self.assertEqual(s.order, ["pre", "log", "post"])


    def _applicationStartsWithConfiguredID(self, argv, uid, gid):
        """
        Assert that given a particular command line, an application is started
        as a particular UID/GID.

        @param argv: A list of strings giving the options to parse.
        @param uid: An integer giving the expected UID.
        @param gid: An integer giving the expected GID.
        """
        self.config.parseOptions(argv)

        events = []
        class FakeUnixApplicationRunner(twistd._SomeApplicationRunner):
            def setupEnvironment(self, chroot, rundir, nodaemon, umask,
                                 pidfile):
                events.append('environment')

            def shedPrivileges(self, euid, uid, gid):
                events.append(('privileges', euid, uid, gid))

            def startReactor(self, reactor, oldstdout, oldstderr):
                events.append('reactor')

            def removePID(self, pidfile):
                pass


        class FakeService(object):
            implements(service.IService, service.IProcess)

            processName = None
            uid = None
            gid = None

            def setName(self, name):
                pass

            def setServiceParent(self, parent):
                pass

            def disownServiceParent(self):
                pass

            def privilegedStartService(self):
                events.append('privilegedStartService')

            def startService(self):
                events.append('startService')

            def stopService(self):
                pass

        application = FakeService()
        verifyObject(service.IService, application)
        verifyObject(service.IProcess, application)

        runner = FakeUnixApplicationRunner(self.config)
        runner.preApplication()
        runner.application = application
        runner.postApplication()

        self.assertEqual(
            events,
            ['environment', 'privilegedStartService',
             ('privileges', False, uid, gid), 'startService', 'reactor'])


    def test_applicationStartsWithConfiguredNumericIDs(self):
        """
        L{postApplication} should change the UID and GID to the values
        specified as numeric strings by the configuration after running
        L{service.IService.privilegedStartService} and before running
        L{service.IService.startService}.
        """
        uid = 1234
        gid = 4321
        self._applicationStartsWithConfiguredID(
            ["--uid", str(uid), "--gid", str(gid)], uid, gid)
    test_applicationStartsWithConfiguredNumericIDs.skip = setuidSkip


    def test_applicationStartsWithConfiguredNameIDs(self):
        """
        L{postApplication} should change the UID and GID to the values
        specified as user and group names by the configuration after running
        L{service.IService.privilegedStartService} and before running
        L{service.IService.startService}.
        """
        user = "foo"
        uid = 1234
        group = "bar"
        gid = 4321
        patchUserDatabase(self.patch, user, uid, group, gid)
        self._applicationStartsWithConfiguredID(
            ["--uid", user, "--gid", group], uid, gid)
    test_applicationStartsWithConfiguredNameIDs.skip = setuidSkip


    def test_startReactorRunsTheReactor(self):
        """
        L{startReactor} calls L{reactor.run}.
        """
        reactor = DummyReactor()
        runner = app.ApplicationRunner({
                "profile": False,
                "profiler": "profile",
                "debug": False})
        runner.startReactor(reactor, None, None)
        self.assertTrue(
            reactor.called, "startReactor did not call reactor.run()")



class UnixApplicationRunnerSetupEnvironmentTests(unittest.TestCase):
    """
    Tests for L{UnixApplicationRunner.setupEnvironment}.

    @ivar root: The root of the filesystem, or C{unset} if none has been
        specified with a call to L{os.chroot} (patched for this TestCase with
        L{UnixApplicationRunnerSetupEnvironmentTests.chroot ).

    @ivar cwd: The current working directory of the process, or C{unset} if
        none has been specified with a call to L{os.chdir} (patched for this
        TestCase with L{UnixApplicationRunnerSetupEnvironmentTests.chdir).

    @ivar mask: The current file creation mask of the process, or C{unset} if
        none has been specified with a call to L{os.umask} (patched for this
        TestCase with L{UnixApplicationRunnerSetupEnvironmentTests.umask).

    @ivar daemon: A boolean indicating whether daemonization has been performed
        by a call to L{_twistd_unix.daemonize} (patched for this TestCase with
        L{UnixApplicationRunnerSetupEnvironmentTests.
    """
    if _twistd_unix is None:
        skip = "twistd unix not available"

    unset = object()

    def setUp(self):
        self.root = self.unset
        self.cwd = self.unset
        self.mask = self.unset
        self.daemon = False
        self.pid = os.getpid()
        self.patch(os, 'chroot', lambda path: setattr(self, 'root', path))
        self.patch(os, 'chdir', lambda path: setattr(self, 'cwd', path))
        self.patch(os, 'umask', lambda mask: setattr(self, 'mask', mask))
        self.patch(_twistd_unix, "daemonize", self.daemonize)
        self.runner = UnixApplicationRunner({})


    def daemonize(self):
        """
        Indicate that daemonization has happened and change the PID so that the
        value written to the pidfile can be tested in the daemonization case.
        """
        self.daemon = True
        self.patch(os, 'getpid', lambda: self.pid + 1)


    def test_chroot(self):
        """
        L{UnixApplicationRunner.setupEnvironment} changes the root of the
        filesystem if passed a non-C{None} value for the C{chroot} parameter.
        """
        self.runner.setupEnvironment("/foo/bar", ".", True, None, None)
        self.assertEqual(self.root, "/foo/bar")


    def test_noChroot(self):
        """
        L{UnixApplicationRunner.setupEnvironment} does not change the root of
        the filesystem if passed C{None} for the C{chroot} parameter.
        """
        self.runner.setupEnvironment(None, ".", True, None, None)
        self.assertIdentical(self.root, self.unset)


    def test_changeWorkingDirectory(self):
        """
        L{UnixApplicationRunner.setupEnvironment} changes the working directory
        of the process to the path given for the C{rundir} parameter.
        """
        self.runner.setupEnvironment(None, "/foo/bar", True, None, None)
        self.assertEqual(self.cwd, "/foo/bar")


    def test_daemonize(self):
        """
        L{UnixApplicationRunner.setupEnvironment} daemonizes the process if
        C{False} is passed for the C{nodaemon} parameter.
        """
        self.runner.setupEnvironment(None, ".", False, None, None)
        self.assertTrue(self.daemon)


    def test_noDaemonize(self):
        """
        L{UnixApplicationRunner.setupEnvironment} does not daemonize the
        process if C{True} is passed for the C{nodaemon} parameter.
        """
        self.runner.setupEnvironment(None, ".", True, None, None)
        self.assertFalse(self.daemon)


    def test_nonDaemonPIDFile(self):
        """
        L{UnixApplicationRunner.setupEnvironment} writes the process's PID to
        the file specified by the C{pidfile} parameter.
        """
        pidfile = self.mktemp()
        self.runner.setupEnvironment(None, ".", True, None, pidfile)
        fObj = file(pidfile)
        pid = int(fObj.read())
        fObj.close()
        self.assertEqual(pid, self.pid)


    def test_daemonPIDFile(self):
        """
        L{UnixApplicationRunner.setupEnvironment} writes the daemonized
        process's PID to the file specified by the C{pidfile} parameter if
        C{nodaemon} is C{False}.
        """
        pidfile = self.mktemp()
        self.runner.setupEnvironment(None, ".", False, None, pidfile)
        fObj = file(pidfile)
        pid = int(fObj.read())
        fObj.close()
        self.assertEqual(pid, self.pid + 1)


    def test_umask(self):
        """
        L{UnixApplicationRunner.setupEnvironment} changes the process umask to
        the value specified by the C{umask} parameter.
        """
        self.runner.setupEnvironment(None, ".", False, 123, None)
        self.assertEqual(self.mask, 123)


    def test_noDaemonizeNoUmask(self):
        """
        L{UnixApplicationRunner.setupEnvironment} doesn't change the process
        umask if C{None} is passed for the C{umask} parameter and C{True} is
        passed for the C{nodaemon} parameter.
        """
        self.runner.setupEnvironment(None, ".", True, None, None)
        self.assertIdentical(self.mask, self.unset)


    def test_daemonizedNoUmask(self):
        """
        L{UnixApplicationRunner.setupEnvironment} changes the process umask to
        C{0077} if C{None} is passed for the C{umask} parameter and C{False} is
        passed for the C{nodaemon} parameter.
        """
        self.runner.setupEnvironment(None, ".", False, None, None)
        self.assertEqual(self.mask, 0077)



class UnixApplicationRunnerStartApplicationTests(unittest.TestCase):
    """
    Tests for L{UnixApplicationRunner.startApplication}.
    """
    if _twistd_unix is None:
        skip = "twistd unix not available"

    def test_setupEnvironment(self):
        """
        L{UnixApplicationRunner.startApplication} calls
        L{UnixApplicationRunner.setupEnvironment} with the chroot, rundir,
        nodaemon, umask, and pidfile parameters from the configuration it is
        constructed with.
        """
        options = twistd.ServerOptions()
        options.parseOptions([
                '--nodaemon',
                '--umask', '0070',
                '--chroot', '/foo/chroot',
                '--rundir', '/foo/rundir',
                '--pidfile', '/foo/pidfile'])
        application = service.Application("test_setupEnvironment")
        self.runner = UnixApplicationRunner(options)

        args = []
        def fakeSetupEnvironment(self, chroot, rundir, nodaemon, umask, pidfile):
            args.extend((chroot, rundir, nodaemon, umask, pidfile))

        # Sanity check
        self.assertEqual(
            inspect.getargspec(self.runner.setupEnvironment),
            inspect.getargspec(fakeSetupEnvironment))

        self.patch(UnixApplicationRunner, 'setupEnvironment', fakeSetupEnvironment)
        self.patch(UnixApplicationRunner, 'shedPrivileges', lambda *a, **kw: None)
        self.patch(app, 'startApplication', lambda *a, **kw: None)
        self.runner.startApplication(application)

        self.assertEqual(
            args,
            ['/foo/chroot', '/foo/rundir', True, 56, '/foo/pidfile'])



class UnixApplicationRunnerRemovePID(unittest.TestCase):
    """
    Tests for L{UnixApplicationRunner.removePID}.
    """
    if _twistd_unix is None:
        skip = "twistd unix not available"


    def test_removePID(self):
        """
        L{UnixApplicationRunner.removePID} deletes the file the name of
        which is passed to it.
        """
        runner = UnixApplicationRunner({})
        path = self.mktemp()
        os.makedirs(path)
        pidfile = os.path.join(path, "foo.pid")
        file(pidfile, "w").close()
        runner.removePID(pidfile)
        self.assertFalse(os.path.exists(pidfile))


    def test_removePIDErrors(self):
        """
        Calling L{UnixApplicationRunner.removePID} with a non-existent filename logs
        an OSError.
        """
        runner = UnixApplicationRunner({})
        runner.removePID("fakepid")
        errors = self.flushLoggedErrors(OSError)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].value.errno, errno.ENOENT)



class DummyReactor(object):
    """
    A dummy reactor, only providing a C{run} method and checking that it
    has been called.

    @ivar called: if C{run} has been called or not.
    @type called: C{bool}
    """
    called = False

    def run(self):
        """
        A fake run method, checking that it's been called one and only time.
        """
        if self.called:
            raise RuntimeError("Already called")
        self.called = True



class AppProfilingTestCase(unittest.TestCase):
    """
    Tests for L{app.AppProfiler}.
    """

    def test_profile(self):
        """
        L{app.ProfileRunner.run} should call the C{run} method of the reactor
        and save profile data in the specified file.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "profile"
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        data = file(config["profile"]).read()
        self.assertIn("DummyReactor.run", data)
        self.assertIn("function calls", data)

    if profile is None:
        test_profile.skip = "profile module not available"


    def _testStats(self, statsClass, profile):
        out = StringIO.StringIO()

        # Patch before creating the pstats, because pstats binds self.stream to
        # sys.stdout early in 2.5 and newer.
        stdout = self.patch(sys, 'stdout', out)

        # If pstats.Stats can load the data and then reformat it, then the
        # right thing probably happened.
        stats = statsClass(profile)
        stats.print_stats()
        stdout.restore()

        data = out.getvalue()
        self.assertIn("function calls", data)
        self.assertIn("(run)", data)


    def test_profileSaveStats(self):
        """
        With the C{savestats} option specified, L{app.ProfileRunner.run}
        should save the raw stats object instead of a summary output.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "profile"
        config["savestats"] = True
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        self._testStats(pstats.Stats, config['profile'])

    if profile is None:
        test_profileSaveStats.skip = "profile module not available"


    def test_withoutProfile(self):
        """
        When the C{profile} module is not present, L{app.ProfilerRunner.run}
        should raise a C{SystemExit} exception.
        """
        savedModules = sys.modules.copy()

        config = twistd.ServerOptions()
        config["profiler"] = "profile"
        profiler = app.AppProfiler(config)

        sys.modules["profile"] = None
        try:
            self.assertRaises(SystemExit, profiler.run, None)
        finally:
            sys.modules.clear()
            sys.modules.update(savedModules)


    def test_profilePrintStatsError(self):
        """
        When an error happens during the print of the stats, C{sys.stdout}
        should be restored to its initial value.
        """
        class ErroneousProfile(profile.Profile):
            def print_stats(self):
                raise RuntimeError("Boom")
        self.patch(profile, "Profile", ErroneousProfile)

        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "profile"
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        oldStdout = sys.stdout
        self.assertRaises(RuntimeError, profiler.run, reactor)
        self.assertIdentical(sys.stdout, oldStdout)

    if profile is None:
        test_profilePrintStatsError.skip = "profile module not available"


    def test_hotshot(self):
        """
        L{app.HotshotRunner.run} should call the C{run} method of the reactor
        and save profile data in the specified file.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "hotshot"
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        data = file(config["profile"]).read()
        self.assertIn("run", data)
        self.assertIn("function calls", data)

    if hotshot is None:
        test_hotshot.skip = "hotshot module not available"


    def test_hotshotSaveStats(self):
        """
        With the C{savestats} option specified, L{app.HotshotRunner.run} should
        save the raw stats object instead of a summary output.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "hotshot"
        config["savestats"] = True
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        self._testStats(hotshot.stats.load, config['profile'])

    if hotshot is None:
        test_hotshotSaveStats.skip = "hotshot module not available"


    def test_withoutHotshot(self):
        """
        When the C{hotshot} module is not present, L{app.HotshotRunner.run}
        should raise a C{SystemExit} exception and log the C{ImportError}.
        """
        savedModules = sys.modules.copy()
        sys.modules["hotshot"] = None

        config = twistd.ServerOptions()
        config["profiler"] = "hotshot"
        profiler = app.AppProfiler(config)
        try:
            self.assertRaises(SystemExit, profiler.run, None)
        finally:
            sys.modules.clear()
            sys.modules.update(savedModules)


    def test_hotshotPrintStatsError(self):
        """
        When an error happens while printing the stats, C{sys.stdout}
        should be restored to its initial value.
        """
        class ErroneousStats(pstats.Stats):
            def print_stats(self):
                raise RuntimeError("Boom")
        self.patch(pstats, "Stats", ErroneousStats)

        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "hotshot"
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        oldStdout = sys.stdout
        self.assertRaises(RuntimeError, profiler.run, reactor)
        self.assertIdentical(sys.stdout, oldStdout)

    if hotshot is None:
        test_hotshotPrintStatsError.skip = "hotshot module not available"


    def test_cProfile(self):
        """
        L{app.CProfileRunner.run} should call the C{run} method of the
        reactor and save profile data in the specified file.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "cProfile"
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        data = file(config["profile"]).read()
        self.assertIn("run", data)
        self.assertIn("function calls", data)

    if cProfile is None:
        test_cProfile.skip = "cProfile module not available"


    def test_cProfileSaveStats(self):
        """
        With the C{savestats} option specified,
        L{app.CProfileRunner.run} should save the raw stats object
        instead of a summary output.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "cProfile"
        config["savestats"] = True
        profiler = app.AppProfiler(config)
        reactor = DummyReactor()

        profiler.run(reactor)

        self.assertTrue(reactor.called)
        self._testStats(pstats.Stats, config['profile'])

    if cProfile is None:
        test_cProfileSaveStats.skip = "cProfile module not available"


    def test_withoutCProfile(self):
        """
        When the C{cProfile} module is not present,
        L{app.CProfileRunner.run} should raise a C{SystemExit}
        exception and log the C{ImportError}.
        """
        savedModules = sys.modules.copy()
        sys.modules["cProfile"] = None

        config = twistd.ServerOptions()
        config["profiler"] = "cProfile"
        profiler = app.AppProfiler(config)
        try:
            self.assertRaises(SystemExit, profiler.run, None)
        finally:
            sys.modules.clear()
            sys.modules.update(savedModules)


    def test_unknownProfiler(self):
        """
        Check that L{app.AppProfiler} raises L{SystemExit} when given an
        unknown profiler name.
        """
        config = twistd.ServerOptions()
        config["profile"] = self.mktemp()
        config["profiler"] = "foobar"

        error = self.assertRaises(SystemExit, app.AppProfiler, config)
        self.assertEqual(str(error), "Unsupported profiler name: foobar")


    def test_defaultProfiler(self):
        """
        L{app.Profiler} defaults to the hotshot profiler if not specified.
        """
        profiler = app.AppProfiler({})
        self.assertEqual(profiler.profiler, "hotshot")


    def test_profilerNameCaseInsentive(self):
        """
        The case of the profiler name passed to L{app.AppProfiler} is not
        relevant.
        """
        profiler = app.AppProfiler({"profiler": "HotShot"})
        self.assertEqual(profiler.profiler, "hotshot")



def _patchFileLogObserver(patch):
    """
    Patch L{log.FileLogObserver} to record every call and keep a reference to
    the passed log file for tests.

    @param patch: a callback for patching (usually L{unittest.TestCase.patch}).

    @return: the list that keeps track of the log files.
    @rtype: C{list}
    """
    logFiles = []
    oldFileLobObserver = log.FileLogObserver
    def FileLogObserver(logFile):
        logFiles.append(logFile)
        return oldFileLobObserver(logFile)
    patch(log, 'FileLogObserver', FileLogObserver)
    return logFiles



def _setupSyslog(testCase):
    """
    Make fake syslog, and return list to which prefix and then log
    messages will be appended if it is used.
    """
    logMessages = []
    class fakesyslogobserver(object):
        def __init__(self, prefix):
            logMessages.append(prefix)
        def emit(self, eventDict):
            logMessages.append(eventDict)
    testCase.patch(syslog, "SyslogObserver", fakesyslogobserver)
    return logMessages



class AppLoggerTestCase(unittest.TestCase):
    """
    Tests for L{app.AppLogger}.

    @ivar observers: list of observers installed during the tests.
    @type observers: C{list}
    """

    def setUp(self):
        """
        Override L{log.addObserver} so that we can trace the observers
        installed in C{self.observers}.
        """
        self.observers = []
        def startLoggingWithObserver(observer):
            self.observers.append(observer)
            log.addObserver(observer)
        self.patch(log, 'startLoggingWithObserver', startLoggingWithObserver)


    def tearDown(self):
        """
        Remove all installed observers.
        """
        for observer in self.observers:
            log.removeObserver(observer)


    def _checkObserver(self, logs):
        """
        Ensure that initial C{twistd} logs are written to the given list.

        @type logs: C{list}
        @param logs: The list whose C{append} method was specified as the
            initial log observer.
        """
        self.assertEqual(self.observers, [logs.append])
        self.assertIn("starting up", logs[0]["message"][0])
        self.assertIn("reactor class", logs[1]["message"][0])


    def test_start(self):
        """
        L{app.AppLogger.start} calls L{log.addObserver}, and then writes some
        messages about twistd and the reactor.
        """
        logger = app.AppLogger({})
        observer = []
        logger._getLogObserver = lambda: observer.append
        logger.start(Componentized())
        self._checkObserver(observer)


    def test_startUsesApplicationLogObserver(self):
        """
        When the L{ILogObserver} component is available on the application,
        that object will be used as the log observer instead of constructing a
        new one.
        """
        application = Componentized()
        logs = []
        application.setComponent(ILogObserver, logs.append)
        logger = app.AppLogger({})
        logger.start(application)
        self._checkObserver(logs)


    def _setupConfiguredLogger(self, application, extraLogArgs={},
                               appLogger=app.AppLogger):
        """
        Set up an AppLogger which exercises the C{logger} configuration option.

        @type application: L{Componentized}
        @param application: The L{Application} object to pass to
            L{app.AppLogger.start}.
        @type extraLogArgs: C{dict}
        @param extraLogArgs: extra values to pass to AppLogger.
        @type appLogger: L{AppLogger} class, or a subclass
        @param appLogger: factory for L{AppLogger} instances.

        @rtype: C{list}
        @return: The logs accumulated by the log observer.
        """
        logs = []
        logArgs = {"logger": lambda: logs.append}
        logArgs.update(extraLogArgs)
        logger = appLogger(logArgs)
        logger.start(application)
        return logs


    def test_startUsesConfiguredLogObserver(self):
        """
        When the C{logger} key is specified in the configuration dictionary
        (i.e., when C{--logger} is passed to twistd), the initial log observer
        will be the log observer returned from the callable which the value
        refers to in FQPN form.
        """
        application = Componentized()
        self._checkObserver(self._setupConfiguredLogger(application))


    def test_configuredLogObserverBeatsComponent(self):
        """
        C{--logger} takes precedence over a ILogObserver component set on
        Application.
        """
        nonlogs = []
        application = Componentized()
        application.setComponent(ILogObserver, nonlogs.append)
        self._checkObserver(self._setupConfiguredLogger(application))
        self.assertEqual(nonlogs, [])


    def test_configuredLogObserverBeatsSyslog(self):
        """
        C{--logger} takes precedence over a C{--syslog} command line
        argument.
        """
        logs = _setupSyslog(self)
        application = Componentized()
        self._checkObserver(self._setupConfiguredLogger(application,
                                                        {"syslog": True},
                                                        UnixAppLogger))
        self.assertEqual(logs, [])

    if _twistd_unix is None or syslog is None:
        test_configuredLogObserverBeatsSyslog.skip = "Not on POSIX, or syslog not available."


    def test_configuredLogObserverBeatsLogfile(self):
        """
        C{--logger} takes precedence over a C{--logfile} command line
        argument.
        """
        application = Componentized()
        path = self.mktemp()
        self._checkObserver(self._setupConfiguredLogger(application,
                                                        {"logfile": "path"}))
        self.assertFalse(os.path.exists(path))


    def test_getLogObserverStdout(self):
        """
        When logfile is empty or set to C{-}, L{app.AppLogger._getLogObserver}
        returns a log observer pointing at C{sys.stdout}.
        """
        logger = app.AppLogger({"logfile": "-"})
        logFiles = _patchFileLogObserver(self.patch)

        observer = logger._getLogObserver()

        self.assertEqual(len(logFiles), 1)
        self.assertIdentical(logFiles[0], sys.stdout)

        logger = app.AppLogger({"logfile": ""})
        observer = logger._getLogObserver()

        self.assertEqual(len(logFiles), 2)
        self.assertIdentical(logFiles[1], sys.stdout)


    def test_getLogObserverFile(self):
        """
        When passing the C{logfile} option, L{app.AppLogger._getLogObserver}
        returns a log observer pointing at the specified path.
        """
        logFiles = _patchFileLogObserver(self.patch)
        filename = self.mktemp()
        logger = app.AppLogger({"logfile": filename})

        observer = logger._getLogObserver()

        self.assertEqual(len(logFiles), 1)
        self.assertEqual(logFiles[0].path,
                          os.path.abspath(filename))


    def test_stop(self):
        """
        L{app.AppLogger.stop} removes the observer created in C{start}, and
        reinitialize its C{_observer} so that if C{stop} is called several
        times it doesn't break.
        """
        removed = []
        observer = object()
        def remove(observer):
            removed.append(observer)
        self.patch(log, 'removeObserver', remove)
        logger = app.AppLogger({})
        logger._observer = observer
        logger.stop()
        self.assertEqual(removed, [observer])
        logger.stop()
        self.assertEqual(removed, [observer])
        self.assertIdentical(logger._observer, None)



class UnixAppLoggerTestCase(unittest.TestCase):
    """
    Tests for L{UnixAppLogger}.

    @ivar signals: list of signal handlers installed.
    @type signals: C{list}
    """
    if _twistd_unix is None:
        skip = "twistd unix not available"

    def setUp(self):
        """
        Fake C{signal.signal} for not installing the handlers but saving them
        in C{self.signals}.
        """
        self.signals = []
        def fakeSignal(sig, f):
            self.signals.append((sig, f))
        self.patch(signal, "signal", fakeSignal)


    def test_getLogObserverStdout(self):
        """
        When non-daemonized and C{logfile} is empty or set to C{-},
        L{UnixAppLogger._getLogObserver} returns a log observer pointing at
        C{sys.stdout}.
        """
        logFiles = _patchFileLogObserver(self.patch)

        logger = UnixAppLogger({"logfile": "-", "nodaemon": True})
        observer = logger._getLogObserver()
        self.assertEqual(len(logFiles), 1)
        self.assertIdentical(logFiles[0], sys.stdout)

        logger = UnixAppLogger({"logfile": "", "nodaemon": True})
        observer = logger._getLogObserver()
        self.assertEqual(len(logFiles), 2)
        self.assertIdentical(logFiles[1], sys.stdout)


    def test_getLogObserverStdoutDaemon(self):
        """
        When daemonized and C{logfile} is set to C{-},
        L{UnixAppLogger._getLogObserver} raises C{SystemExit}.
        """
        logger = UnixAppLogger({"logfile": "-", "nodaemon": False})
        error = self.assertRaises(SystemExit, logger._getLogObserver)
        self.assertEqual(str(error), "Daemons cannot log to stdout, exiting!")


    def test_getLogObserverFile(self):
        """
        When C{logfile} contains a file name, L{app.AppLogger._getLogObserver}
        returns a log observer pointing at the specified path, and a signal
        handler rotating the log is installed.
        """
        logFiles = _patchFileLogObserver(self.patch)
        filename = self.mktemp()
        logger = UnixAppLogger({"logfile": filename})
        observer = logger._getLogObserver()

        self.assertEqual(len(logFiles), 1)
        self.assertEqual(logFiles[0].path,
                          os.path.abspath(filename))

        self.assertEqual(len(self.signals), 1)
        self.assertEqual(self.signals[0][0], signal.SIGUSR1)

        d = Deferred()
        def rotate():
            d.callback(None)
        logFiles[0].rotate = rotate

        rotateLog = self.signals[0][1]
        rotateLog(None, None)
        return d


    def test_getLogObserverDontOverrideSignalHandler(self):
        """
        If a signal handler is already installed,
        L{UnixAppLogger._getLogObserver} doesn't override it.
        """
        def fakeGetSignal(sig):
            self.assertEqual(sig, signal.SIGUSR1)
            return object()
        self.patch(signal, "getsignal", fakeGetSignal)
        filename = self.mktemp()
        logger = UnixAppLogger({"logfile": filename})
        observer = logger._getLogObserver()

        self.assertEqual(self.signals, [])


    def test_getLogObserverDefaultFile(self):
        """
        When daemonized and C{logfile} is empty, the observer returned by
        L{UnixAppLogger._getLogObserver} points at C{twistd.log} in the current
        directory.
        """
        logFiles = _patchFileLogObserver(self.patch)
        logger = UnixAppLogger({"logfile": "", "nodaemon": False})
        observer = logger._getLogObserver()

        self.assertEqual(len(logFiles), 1)
        self.assertEqual(logFiles[0].path,
                          os.path.abspath("twistd.log"))


    def test_getLogObserverSyslog(self):
        """
        If C{syslog} is set to C{True}, L{UnixAppLogger._getLogObserver} starts
        a L{syslog.SyslogObserver} with given C{prefix}.
        """
        logs = _setupSyslog(self)
        logger = UnixAppLogger({"syslog": True, "prefix": "test-prefix"})
        observer = logger._getLogObserver()
        self.assertEqual(logs, ["test-prefix"])
        observer({"a": "b"})
        self.assertEqual(logs, ["test-prefix", {"a": "b"}])

    if syslog is None:
        test_getLogObserverSyslog.skip = "Syslog not available"




class DeprecationTests(unittest.TestCase):
    """
    Tests for deprecated features.
    """

    def test_initialLog(self):
        """
        L{app.initialLog} is deprecated.
        """
        logs = []
        log.addObserver(logs.append)
        self.addCleanup(log.removeObserver, logs.append)
        self.callDeprecated(Version("Twisted", 8, 2, 0), app.initialLog)
        self.assertEqual(len(logs), 2)
        self.assertIn("starting up", logs[0]["message"][0])
        self.assertIn("reactor class", logs[1]["message"][0])