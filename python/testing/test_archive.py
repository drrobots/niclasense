"""The archive pull, checked as text.

None of it can run here: it is PowerShell, robocopy and the Windows task scheduler. That is
the same position `packaging/` is in, and the same answer applies -- assert statically what
would be expensive to discover on a fleet, and let one real machine cover the rest.

The invariant worth the most here is that nothing in this directory can delete. An archive
that quietly mirrors a capture's retention is indistinguishable from a working one until the
day someone goes looking for last year's data.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

import support

ARCHIVE_DIR = os.path.join(support.REPO_DIR, "archive")


def read(name):
    with open(os.path.join(ARCHIVE_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def code(name):
    """The script with its comments removed.

    Asserting a flag is absent has to mean absent from the *code*: the scripts explain at
    length why /MIR and /PURGE are not there, and a plain substring check finds the
    explanation and fails. Deleting the prose to satisfy the test would be exactly the wrong
    trade -- the comment is the thing most likely to stop someone adding the flag back.
    """
    text = re.sub(r"<#.*?#>", "", read(name), flags=re.DOTALL)
    kept = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        kept.append(line.split(" #", 1)[0])
    return "\n".join(kept)


class Layout(unittest.TestCase):
    def test_every_script_is_present(self):
        for name in ("pull-logs.ps1", "pull-task.ps1", "share-logs.ps1",
                     "push-logs.ps1", "push-task.ps1",
                     "sources.example.txt", "README.md"):
            self.assertTrue(
                os.path.isfile(os.path.join(ARCHIVE_DIR, name)), "missing archive/%s" % name
            )

    def test_the_real_source_list_is_not_committed(self):
        """It names the site's machines and is per-deployment, like nicla.conf."""
        self.assertFalse(os.path.exists(os.path.join(ARCHIVE_DIR, "sources.txt")))
        with open(os.path.join(support.REPO_DIR, ".gitignore"), encoding="utf-8") as handle:
            self.assertIn("archive/sources.txt", handle.read())


class NeverDeletes(unittest.TestCase):
    """The one behaviour that separates an archive from a mirror."""

    def setUp(self):
        self.pull = code("pull-logs.ps1")

    def test_no_mirror_and_no_purge(self):
        # /MIR implies /PURGE, and /PURGE deletes destination files that are gone from the
        # source -- which is precisely what retention.py does to a capture machine on its
        # own schedule, and precisely what the archive exists not to follow.
        self.assertNotIn("/MIR", self.pull)
        self.assertNotIn("/PURGE", self.pull)

    def test_nothing_removes_files(self):
        for forbidden in ("Remove-Item", "del ", "Clear-Content"):
            self.assertNotIn(forbidden, self.pull,
                             "%r can delete; the archive must not" % forbidden)


class DoesNotWedge(unittest.TestCase):
    """An unreachable capture is a normal Tuesday, not an outage."""

    def setUp(self):
        self.pull = read("pull-logs.ps1")
        self.task = read("pull-task.ps1")

    def test_retries_are_bounded(self):
        """robocopy defaults to a million retries thirty seconds apart, which would hold one
        run open for weeks and let the next start on top of it."""
        self.assertRegex(self.pull, r'"/R:\d+"')
        self.assertRegex(self.pull, r'"/W:\d+"')
        retries = int(re.search(r'"/R:(\d+)"', self.pull).group(1))
        self.assertLessEqual(retries, 5, "retry count is high enough to stall a run")

    def test_an_unreachable_share_is_not_a_failure(self):
        self.assertIn("unreachable", self.pull)

    def test_runs_do_not_stack(self):
        self.assertIn("IgnoreNew", self.task)
        self.assertIn("ExecutionTimeLimit", self.task)

    def test_robocopy_exit_codes_are_read_as_a_bitmask(self):
        """Success is not zero. Anything below 8 copied something or had nothing to do, and
        treating non-zero as failure -- the reflex everywhere else -- reports every good run
        as a bad one."""
        self.assertRegex(self.pull, r"-ge\s+8")


class RunsWithoutAPassword(unittest.TestCase):
    def setUp(self):
        self.task = read("pull-task.ps1")
        self.share = read("share-logs.ps1")

    def test_the_task_runs_as_system(self):
        """So it reaches the network as the machine account and no credential is stored."""
        self.assertIn("SYSTEM", self.task)

    def test_it_says_what_the_collector_account_needs(self):
        """Read on each capture and write on the archive, granted to a machine account whose
        name ends in $. Whoever runs this once, months from now, gets told rather than having
        to work out why every source says FAILED."""
        self.assertIn("read", self.task.lower())
        self.assertIn("write", self.task.lower())

    def test_no_password_is_ever_taken_or_stored(self):
        for forbidden in ("-Password", "ConvertTo-SecureString", "PSCredential"):
            self.assertNotIn(forbidden, self.task)
            self.assertNotIn(forbidden, self.share)

    def test_it_starts_without_anyone_logged_on(self):
        """The dashboard is a logon task because it puts a window in front of a person. This
        one has to keep filling the archive whether or not anybody is signed in."""
        self.assertIn("-AtStartup", self.task)
        self.assertNotIn("-AtLogOn", self.task)


class ShareIsReadOnly(unittest.TestCase):
    def setUp(self):
        self.share = read("share-logs.ps1")

    def test_access_is_granted_read_and_nothing_else(self):
        self.assertIn("-AccessRight Read", self.share)
        for forbidden in ("-AccessRight Change", "-AccessRight Full", "-ChangeAccess",
                          "-FullAccess"):
            self.assertNotIn(forbidden, self.share)

    def test_the_default_everyone_grant_is_removed(self):
        """Share ACEs combine by taking the most permissive, so leaving Everyone's default
        Read in place would make the named account beside the point."""
        self.assertIn("Revoke-SmbShareAccess", self.share)
        self.assertIn("Everyone", self.share)

    def test_the_firewall_is_not_touched_unless_asked(self):
        self.assertIn("OpenFirewall", self.share)
        enable = self.share.index("Enable-NetFirewallRule")
        guard = self.share.index("if ($OpenFirewall)")
        self.assertLess(guard, enable, "the firewall change is not behind the switch")

    def test_no_application_port_is_opened_anywhere(self):
        """The point of the file route is that a capture machine opens nothing inbound of
        its own. File sharing is the platform's; 8765 would be ours."""
        for text in (self.share, read("pull-logs.ps1"), read("pull-task.ps1")):
            self.assertNotIn("8765", text)
            self.assertNotIn("netsh advfirewall firewall add rule", text)


class Push(unittest.TestCase):
    """Each capture sends its own logs, and knows about no other machine.

    What this buys over being pulled from: no collector to keep running, no list of sensors
    anywhere, and no read-only share on a capture for something else to reach into. What it
    costs is that every capture can write to the archive, where a collector would have been
    the only one.
    """

    def setUp(self):
        self.push = code("push-logs.ps1")
        self.task = read("push-task.ps1")

    def test_it_never_deletes_either(self):
        """retention.py prunes this machine on its own schedule and the archive outlives it.
        A mirror would faithfully reproduce those deletions."""
        self.assertNotIn("/MIR", self.push)
        self.assertNotIn("/PURGE", self.push)
        for forbidden in ("Remove-Item", "Clear-Content"):
            self.assertNotIn(forbidden, self.push)

    def test_retries_are_bounded(self):
        self.assertRegex(self.push, r'"/R:\d+"')
        self.assertRegex(self.push, r'"/W:\d+"')

    def test_exit_codes_are_read_as_a_bitmask(self):
        self.assertRegex(self.push, r"-ge\s+8")

    def test_only_captures_are_sent(self):
        """The log directory is this machine's; the share is everyone's."""
        self.assertIn('"*.csv"', self.push)

    def test_the_machine_names_itself_portably(self):
        r"""$env:COMPUTERNAME is Windows-only, and reaching .ToLower() through a null is how
        this failed the first time it ran anywhere else."""
        self.assertIn("[Environment]::MachineName", self.push)
        self.assertNotIn("$env:COMPUTERNAME", self.push)

    def test_the_share_root_is_not_created(self):
        """A missing share root means the share is not mounted. Creating it would make a
        local directory that quietly collects logs nobody can see."""
        self.assertIn("archive unreachable", self.push)

    def test_the_task_runs_as_system_without_a_password(self):
        self.assertIn("SYSTEM", self.task)
        self.assertIn("-AtStartup", self.task)
        for forbidden in ("-Password", "ConvertTo-SecureString", "PSCredential"):
            self.assertNotIn(forbidden, self.task)

    def test_runs_do_not_stack(self):
        self.assertIn("IgnoreNew", self.task)
        self.assertIn("ExecutionTimeLimit", self.task)


class InstalledPush(unittest.TestCase):
    """The installer half. Off unless a share is named."""

    def setUp(self):
        with open(os.path.join(support.REPO_DIR, "packaging", "nicla.iss"),
                  encoding="utf-8") as handle:
            self.iss = handle.read()
        with open(os.path.join(support.REPO_DIR, "packaging", "build.ps1"),
                  encoding="utf-8") as handle:
            self.build = handle.read()

    def test_the_scripts_are_shipped(self):
        """Staged from archive\\ rather than packaging\\service\\, since the same two files are
        what you run by hand on a machine installed before there was a share."""
        self.assertIn("push-logs.ps1", self.build)
        self.assertIn("push-task.ps1", self.build)

    def test_an_install_that_names_no_share_pushes_nothing(self):
        rule = [line for line in self.iss.splitlines()
                if "PushTaskArgs" in line and line.startswith("Filename:")]
        self.assertEqual(len(rule), 1)
        self.assertIn("Check: PushesToArchive", rule[0])

    def test_the_share_comes_from_the_command_line(self):
        self.assertIn("{param:Archive|}", self.iss)
        self.assertIn("{param:SensorName|}", self.iss)

    def test_the_task_is_removed_again(self):
        uninstall = self.iss.split("[UninstallRun]", 1)[1]
        self.assertIn("push-task.ps1", uninstall)
        self.assertIn("-Uninstall", uninstall)


class SourceList(unittest.TestCase):
    """The example has to parse under the rules pull-logs.ps1 enforces, since it is what
    everyone copies."""

    NAME = re.compile(r"^[A-Za-z0-9._-]+$")

    def setUp(self):
        self.lines = read("sources.example.txt").splitlines()

    def entries(self):
        for line in self.lines:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line

    def test_the_example_has_entries(self):
        self.assertGreater(len(list(self.entries())), 0)

    def test_every_entry_is_a_name_and_a_unc_path(self):
        for line in self.entries():
            parts = line.split(None, 1)
            self.assertEqual(len(parts), 2, "not '<name> <share>': %r" % line)
            name, share = parts[0], parts[1].strip()
            self.assertRegex(name, self.NAME, "unusable as a directory name: %r" % name)
            self.assertTrue(share.startswith("\\\\"), "not a UNC path: %r" % share)

    def test_names_are_unique(self):
        """They become directories, so a duplicate would silently merge two boards."""
        names = [line.split(None, 1)[0] for line in self.entries()]
        self.assertEqual(sorted(names), sorted(set(names)))


PWSH = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(PWSH, "no PowerShell available to run the scripts")
class Behaviour(unittest.TestCase):
    """What the pull actually does, run rather than read.

    More of it is reachable from a Mac than it looks. With every source unresolvable,
    robocopy is never invoked -- so the parser, the directory layout, the unreachable path,
    the log and the exit code all execute here, and only the copy itself needs Windows.

    This is not decoration. Running it is what found Write-Line emitting on the success
    stream, which made every progress line part of Copy-Source's return value: it printed
    nothing and still counted correctly, and no amount of reading the file was going to
    show that.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nicla-archive-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.script = os.path.join(support.REPO_DIR, "archive", "pull-logs.ps1")

    def sources(self, text):
        path = os.path.join(self.root, "sources.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_pull(self, sources_text):
        archive = os.path.join(self.root, "archive")
        done = subprocess.run(
            [PWSH, "-NoProfile", "-File", self.script,
             "-ArchiveRoot", archive, "-Sources", self.sources(sources_text)],
            capture_output=True, text=True, timeout=120,
        )
        return done, archive

    def test_the_scripts_parse(self):
        """A syntax error would otherwise be found by a fleet."""
        names = ("pull-logs.ps1", "pull-task.ps1", "share-logs.ps1",
                 "push-logs.ps1", "push-task.ps1")
        command = (
            "$bad=0; foreach ($f in @(%s)) { $e=$null; $t=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path $f), [ref]$t, [ref]$e) | Out-Null; "
            "if ($e -and $e.Count) { $bad=1; Write-Output \"${f}: $($e[0].Message)\" } }; exit $bad"
            % ",".join("'%s'" % name for name in names)
        )
        done = subprocess.run(
            [PWSH, "-NoProfile", "-Command", command],
            cwd=os.path.join(support.REPO_DIR, "archive"),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_unreachable_sources_build_the_layout_and_succeed(self):
        done, archive = self.run_pull(
            "# comment\n\nbench   /nonexistent/bench\nrig-a   /nonexistent/riga\n"
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # The directory per source is the board list, so it is made even for a source that
        # was not reachable this time -- the board exists, it just said nothing today.
        self.assertTrue(os.path.isdir(os.path.join(archive, "bench")))
        self.assertTrue(os.path.isdir(os.path.join(archive, "rig-a")))
        self.assertIn("2 unreachable", done.stdout)

    def test_progress_reaches_the_console_and_the_log(self):
        """The regression for Write-Line on the success stream: the counts were right and
        the operator saw nothing."""
        done, archive = self.run_pull("bench   /nonexistent/bench\n")
        self.assertIn("unreachable  bench", done.stdout)
        with open(os.path.join(archive, "pull-logs.log"), encoding="utf-8") as handle:
            self.assertIn("unreachable  bench", handle.read())

    def test_push_reports_an_unreachable_share_and_creates_nothing(self):
        """A capture that is off the network is a normal state, not an alarm -- the next run
        sends what this one could not, because nothing here deletes."""
        logs = os.path.join(self.root, "logs")
        os.makedirs(logs)
        with open(os.path.join(logs, "nicla_20260819_090000.csv"), "w") as handle:
            handle.write("host_iso\n")
        missing = os.path.join(self.root, "not-mounted")
        done = subprocess.run(
            [PWSH, "-NoProfile", "-File",
             os.path.join(support.REPO_DIR, "archive", "push-logs.ps1"),
             "-ArchiveRoot", missing, "-Name", "bench", "-LogDir", logs,
             "-LogPath", os.path.join(self.root, "push.log")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertIn("archive unreachable", done.stdout + done.stderr)
        self.assertFalse(os.path.exists(missing), "it created the share root")

    def test_push_says_so_when_there_is_nothing_to_send(self):
        done = subprocess.run(
            [PWSH, "-NoProfile", "-File",
             os.path.join(support.REPO_DIR, "archive", "push-logs.ps1"),
             "-ArchiveRoot", self.root, "-LogDir", os.path.join(self.root, "absent"),
             "-LogPath", os.path.join(self.root, "push.log")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(done.returncode, 0, "an unconfigured machine is not a failure")
        self.assertIn("nothing to push", done.stdout + done.stderr)

    def test_a_missing_source_list_says_so(self):
        archive = os.path.join(self.root, "archive")
        done = subprocess.run(
            [PWSH, "-NoProfile", "-File", self.script, "-ArchiveRoot", archive,
             "-Sources", os.path.join(self.root, "absent.txt")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no source list", done.stdout + done.stderr)

    def test_an_unreachable_archive_says_which_of_the_two_things_it_is(self):
        """The destination is a share now, so it can be down or merely unwritable, and the
        difference is most of the diagnosis. Without this it surfaces as whatever New-Item
        threw, out of a scheduled task nobody is watching."""
        archive = "/System/nope/archive"        # exists on no machine and cannot be created
        done = subprocess.run(
            [PWSH, "-NoProfile", "-File", self.script,
             "-ArchiveRoot", archive,
             "-Sources", self.sources("bench   /nonexistent/bench\n"),
             "-LogPath", os.path.join(self.root, "pull.log")],
            capture_output=True, text=True, timeout=120,
        )
        output = done.stdout + done.stderr
        self.assertIn("cannot reach or create", output)
        self.assertIn("unmounted", output)
        self.assertNotEqual(done.returncode, 0)

    def test_a_name_that_is_not_a_directory_name_is_refused(self):
        """It becomes a directory unmodified, so it is checked before it can become a path
        somewhere surprising."""
        done, _archive = self.run_pull("bad/name   //host/share\n")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("not usable as a directory name", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
