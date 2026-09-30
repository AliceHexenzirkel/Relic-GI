using System.Text;
using Relic.Core.Util;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace Relic.Core.Server;

/// <summary>SSH endpoint + credentials for a fresh-server install. Password and/or private key
/// (OpenSSH / PEM / PuTTY .ppk, optional passphrase). A non-root user needs sudo; the sudo password
/// defaults to the SSH password when blank.</summary>
public sealed record SshTarget(string Host, int Port, string User, string Password, string KeyPath, string KeyPassphrase, string SudoPassword)
{
    public bool IsRoot => string.Equals(User, "root", StringComparison.Ordinal);
    public string EffectiveSudoPassword => string.IsNullOrEmpty(SudoPassword) ? Password : SudoPassword;
}

/// <summary>Outcome of an install. <paramref name="Warning"/> is set when the installer itself succeeded
/// but the agent did not answer /health in time: the box IS configured and running the new token, so this
/// must never be raised as a failure — the caller still has to save the connection, or the admin is left
/// with an agent whose token nobody has ever seen.</summary>
public sealed record DeployResult(string HostKeyFingerprint, string Health, string Warning = "",
    IReadOnlyList<string>? Fetch = null)
{
    /// <summary>The versions the agent really has to download afterwards — the plan's marks minus the
    /// ones whose folder already held a stack (install_agent.sh skips those silently).</summary>
    public IReadOnlyList<string> FetchVersions => Fetch ?? Array.Empty<string>();
}

/// <summary>
/// Installs/upgrades the GIO agent on a Linux box over SSH by running the SAME installer an admin
/// would run by hand (<c>install_agent.sh --yes</c> with the plan as environment) — one path, so the
/// launcher-driven install cannot drift from the documented one. Distro-neutral (Alma/RHEL uses
/// firewalld, Ubuntu uses ufw or none); idempotent — a re-run is the upgrade. The older password-only
/// deploy that hand-merged /etc/gio-agent/config and uploaded the repo's unit file was removed in
/// 2026-09 together with the Settings form that drove it: the installer's own upgrade merge
/// (the stored config as the run's defaults) is the one merge that exists now.
/// </summary>
public static class AgentDeployer
{
    /// <summary>
    /// Install the agent on a Linux box by running the SAME installer an admin would run by hand:
    /// upload agent/ (script, installers, unit, payloads) to ~/.relic-agent-upload, then
    /// <c>install_agent.sh --yes</c> with the plan as environment — as root directly, via <c>sudo -n</c>
    /// when passwordless, else <c>sudo -S -k -p ''</c> with the sudo password as the ONLY stdin line (the
    /// script runs by path, never <c>bash -s</c>, so a non-prompting sudo can never execute the password
    /// as a command). The script selftests the uploaded agent, writes /etc/gio-agent/config, opens
    /// firewalld/ufw and enables the systemd unit. Verifies /health at the end and removes the upload.
    /// </summary>
    public static DeployResult Install(SshTarget ssh, AgentInstallPlan plan, string agentDir, Action<string>? log = null)
    {
        var errs = plan.Validate();
        if (errs.Count > 0) throw new InvalidOperationException(string.Join("\n", errs));
        string script = Path.Combine(agentDir, "gio_agent.py");
        string installer = Path.Combine(agentDir, "install_agent.sh");
        if (!File.Exists(script) || !File.Exists(installer))
            throw new InvalidOperationException(L.T("core.deploy.agentMissingInBuild", new { path = agentDir }));

        var auth = new List<AuthenticationMethod>();
        if (!string.IsNullOrWhiteSpace(ssh.KeyPath))
        {
            if (!File.Exists(ssh.KeyPath)) throw new InvalidOperationException(L.T("core.deploy.keyMissing", new { path = ssh.KeyPath }));
            var key = string.IsNullOrEmpty(ssh.KeyPassphrase) ? new PrivateKeyFile(ssh.KeyPath) : new PrivateKeyFile(ssh.KeyPath, ssh.KeyPassphrase);
            auth.Add(new PrivateKeyAuthenticationMethod(ssh.User, key));
        }
        if (!string.IsNullOrEmpty(ssh.Password)) auth.Add(new PasswordAuthenticationMethod(ssh.User, ssh.Password));
        if (auth.Count == 0) throw new InvalidOperationException(L.T("core.deploy.noCredentials"));
        var ci = new ConnectionInfo(ssh.Host, ssh.Port, ssh.User, auth.ToArray()) { Timeout = TimeSpan.FromSeconds(20) };

        string fingerprint = "";
        using var client = new SshClient(ci);
        client.HostKeyReceived += (_, e) =>
        {
            fingerprint = "SHA256:" + Convert.ToBase64String(System.Security.Cryptography.SHA256.HashData(e.HostKey)).TrimEnd('=');
            log?.Invoke(L.T("core.deploy.hostKey", new { fingerprint }));
            e.CanTrust = true; // first contact — the fingerprint is returned for the caller to record
        };
        log?.Invoke(L.T("core.deploy.connecting", new { host = ssh.Host, port = ssh.Port, user = ssh.User }));
        client.Connect();
        using var sftp = new SftpClient(ci);
        sftp.Connect();

        string home = RunOut(client, "echo ~").Trim();
        if (home.Length == 0) home = ssh.IsRoot ? "/root" : "/home/" + ssh.User;
        string up = home + "/.relic-agent-upload";

        // Stack folders must already be there — unless the version is marked for download: then the
        // folder is expected to be absent and the AGENT fills it (agent 3.4). Nothing is started here:
        // under GIO_RELIC_ENV=1 install_agent.sh leaves the download to the launcher, which runs the
        // fetch jobs from the Server page after deploy.done (they take 10-45 minutes on archive.org).
        var missing = plan.MissingStackEntries(p => RunOut(client, $"test -e {Q(p)} && echo yes || echo no").Trim() == "yes");
        if (missing.Count > 0) throw new InvalidOperationException(L.T("core.deploy.stackIncomplete", new { list = string.Join(", ", missing) }));
        // The same probe install_agent.sh runs: a folder that already holds a stack is NOT downloaded,
        // so the launcher must not queue it either (the agent would answer 409 "already on this server"
        // and the queue behind it would be dropped).
        var willFetch = plan.FetchVersionsReally(p => RunOut(client, $"test -e {Q(p)} && echo yes || echo no").Trim() == "yes");
        foreach (var v in plan.FetchVersions())
            log?.Invoke(willFetch.Contains(v)
                ? L.T("core.deploy.willFetch", new { version = v, dir = plan.DirFor(v) })
                : L.T("core.deploy.fetchSkipped", new { version = v, dir = plan.DirFor(v) }));
        // Neither is a stop any more: install_agent.sh installs python3 (>= 3.9) and docker + compose v2
        // itself when they are missing -- the lines only tell the admin what the installer is about to do.
        if (RunOut(client, "command -v python3 >/dev/null 2>&1 && echo yes || echo no").Trim() != "yes")
            log?.Invoke(L.T("core.deploy.noPython3OnBox"));
        if (RunOut(client, "docker compose version >/dev/null 2>&1 && echo yes || echo no").Trim() != "yes")
            log?.Invoke(L.T("core.deploy.noDockerWarn"));

        log?.Invoke(L.T("core.deploy.uploading", new { dir = up }));
        // 0700 BEFORE anything lands in it: the staging dir is about to hold the bearer token.
        RunOut(client, $"rm -rf {Q(up)} && mkdir -p {Q(up)} && chmod 700 {Q(up)}");
        try
        {
            foreach (var f in new[] { "gio_agent.py", "install_agent.sh", "uninstall_agent.sh", "gio-agent.service" })
            {
                string src = Path.Combine(agentDir, f);
                if (File.Exists(src)) UploadFile(sftp, src, up + "/" + f);
            }
            // Only create payloads/ when there is something to put in it. install_agent.sh replaces the
            // box's payloads whenever the directory merely EXISTS, so an empty one used to delete them.
            string payloads = Path.Combine(agentDir, "payloads");
            if (Directory.Exists(payloads) && Directory.EnumerateFiles(payloads, "*", SearchOption.AllDirectories).Any())
                UploadTree(sftp, client, payloads, up + "/payloads");
            // CRLF-proof the shell scripts (a repo checkout on Windows may carry CRLF).
            RunOut(client, $"sed -i 's/\\r$//' {Q(up)}/install_agent.sh {Q(up)}/uninstall_agent.sh 2>/dev/null; chmod 755 {Q(up)}/install_agent.sh {Q(up)}/uninstall_agent.sh 2>/dev/null; true");

            // The plan goes over as a 0600 file that the installer SOURCES — never as `env KEY=...` on the
            // remote command line. There it was readable in /proc/<pid>/cmdline by every local user and was
            // written verbatim and permanently into sudo's auth.log, and it also leaked through any SSH.NET
            // exception that quotes CommandText. The token IS the security boundary (CLAUDE.md).
            // GIO_AGENT_START=y: the admin clicked Install — a running agent is the expected outcome even
            // when a previous one on the box had been stopped (an upgrade run keeps that state otherwise).
            string envFile = up + "/install.env";
            var envText = new StringBuilder();
            foreach (var kv in plan.ToEnvironment()) envText.Append(kv.Key).Append('=').Append(Q(kv.Value)).Append('\n');
            envText.Append("GIO_AGENT_START=y\n");
            // Sentinel, checked by the runner below. A failed `.` (source) is NOT fatal in a plain
            // `bash -c`, so without this a file root cannot read — or a truncated upload — would let
            // the installer run with NO plan at all: it mints its OWN random token, guesses the stack
            // paths and exits 0, and we would then save and show a token the box has never seen.
            envText.Append("GIO_RELIC_ENV=1\n");
            // Installer-only, never written to /etc/gio-agent/config. Only relaxes the INCONCLUSIVE branch
            // of the pre-upgrade probe — a genuinely busy agent still refuses to be upgraded.
            if (plan.UpgradeForce) envText.Append("GIO_UPGRADE_FORCE=y\n");
            UploadText(sftp, envText.ToString(), envFile);
            RunOut(client, $"chmod 600 {Q(envFile)}");

            // set -a exports everything the file assigns, so the installer sees exactly the environment it
            // used to be handed on the command line — empty values included, which it treats as "not set".
            string runner = "set -a; . ./install.env || exit 97; set +a; "
                + "[ \"$GIO_RELIC_ENV\" = 1 ] || exit 97; exec bash ./install_agent.sh --yes";
            string cmdText;
            string? stdin = null;
            if (ssh.IsRoot) cmdText = $"cd {Q(up)} && bash -c {Q(runner)}";
            else if (Exit(client, "sudo -n true") == 0) cmdText = $"cd {Q(up)} && sudo -n bash -c {Q(runner)}";
            else
            {
                if (string.IsNullOrEmpty(ssh.EffectiveSudoPassword)) throw new InvalidOperationException(L.T("core.deploy.sudoPasswordNeeded"));
                cmdText = $"cd {Q(up)} && sudo -S -k -p '' bash -c {Q(runner)}";
                stdin = ssh.EffectiveSudoPassword + "\n";
            }
            log?.Invoke(L.T("core.deploy.runningInstaller"));
            // Two secrets ride in install.env: the token and (when chosen) the MUIP sign key. Both are
            // blanked wherever the installer's output could echo them — a bash diagnostic quoting the
            // failing assignment, a sudo refusal, the failure tail that goes into the error modal.
            string[] secrets = plan.MuipKey.Trim().Length > 0 ? new[] { plan.Token, plan.MuipKey.Trim() } : new[] { plan.Token };
            var (code, output) = RunStreaming(client, cmdText, stdin, secrets, log);
            if (output.Contains("must have a tty", StringComparison.OrdinalIgnoreCase) || output.Contains("requiretty", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(L.T("core.deploy.requireTty"));
            if (output.Contains("incorrect password", StringComparison.OrdinalIgnoreCase) || output.Contains("Sorry, try again", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(L.T("core.deploy.sudoRefused"));
            if (code == 97) throw new InvalidOperationException(L.T("core.deploy.envNotApplied"));
            if (code != 0) throw new InvalidOperationException(L.T("core.deploy.installerFailed", new { code, tail = Redact(Tail(output, 600), secrets) }));

            // Dial where the agent actually bound, not a hardcoded 127.0.0.1 (a specific bind answers only on
            // itself), and POLL: the installer restarts the unit and sleeps 1 s, which a loaded box can easily
            // outrun. A connection refusal comes back instantly, so a single shot used to turn a finished
            // install into a hard failure — and with it the only copy of the generated token.
            string url = $"http://{plan.ProbeAuthority}/health";
            string probe = $"curl -fsS -m 5 {Q(url)} 2>/dev/null || python3 -c \"import urllib.request;print(urllib.request.urlopen('{url}',timeout=5).read().decode())\" 2>/dev/null";
            string health = "";
            for (int attempt = 0; attempt < 12; attempt++)
            {
                health = RunOut(client, probe, TimeSpan.FromSeconds(30));
                if (health.Contains("gio-agent")) break;
                if (attempt == 0) log?.Invoke(L.T("core.deploy.waitingHealth", new { url }));
                Thread.Sleep(2000);
            }
            // A silent agent is reported as a WARNING, never a failure: install_agent.sh has already written
            // /etc/gio-agent/config and restarted the unit, so throwing here would discard the only copy of a
            // token the box is already using.
            string warning = health.Contains("gio-agent") ? "" : L.T("core.deploy.noHealthRemote", new { port = plan.ProbeAuthority });
            log?.Invoke(warning.Length > 0 ? warning : L.T("core.deploy.installedOk"));
            return new DeployResult(fingerprint, health.Trim(), warning, willFetch);
        }
        finally
        {
            // On EVERY exit — the staging dir holds install.env, i.e. the token.
            try { RunOut(client, $"rm -rf {Q(up)}", TimeSpan.FromSeconds(30)); } catch { }
        }
    }

    private static string Q(string s) => "'" + (s ?? "").Replace("'", "'\\''") + "'";

    /// <summary>Every short probe gets a deadline. SSH.NET's default is infinite, so a wedged dockerd or a
    /// stalled NFS stack path used to hang the install for good: the job flag stays held (every later server
    /// operation answers "busy") and the overlay's Close button is disabled while it runs.</summary>
    private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(60);

    private static string RunOut(SshClient c, string text, TimeSpan? timeout = null)
    {
        using var cmd = c.CreateCommand(text);
        cmd.CommandTimeout = timeout ?? ProbeTimeout;
        try { return cmd.Execute(); }
        catch (SshOperationTimeoutException) { return ""; }
    }

    private static int Exit(SshClient c, string text, TimeSpan? timeout = null)
    {
        using var cmd = c.CreateCommand(text);
        cmd.CommandTimeout = timeout ?? ProbeTimeout;
        try { cmd.Execute(); } catch (SshOperationTimeoutException) { return -1; }
        return cmd.ExitStatus ?? -1;
    }

    /// <summary>Blank out every secret (the token, the MUIP sign key) wherever it appears. Applied to
    /// everything that can reach the admin's screen or relic.log — a sudo refusal or a bash error quoting
    /// the failing expansion matches none of the known "Agent token"/"GIO_AGENT_TOKEN=" line shapes.
    /// A secret shorter than 8 characters is never replaced: it would blank ordinary words.</summary>
    private static string Redact(string text, params string[] secrets)
    {
        string s = text ?? "";
        foreach (var secret in secrets)
            if (secret is { Length: >= 8 }) s = s.Replace(secret, "***", StringComparison.Ordinal);
        return s;
    }

    /// <summary>Run a long command, streaming its stdout lines to <paramref name="log"/> (a line that
    /// carries a secret's value is never surfaced), optionally feeding <paramref name="stdin"/> once.</summary>
    private static (int Code, string Output) RunStreaming(SshClient c, string text, string? stdin, string[] secrets, Action<string>? log)
    {
        using var cmd = c.CreateCommand(text);
        // A fresh box runs apt-get update + the whole docker install inside this one command, which 10
        // minutes on a slow link does not cover — and a timeout here kills the install half-done.
        cmd.CommandTimeout = TimeSpan.FromMinutes(45);
        // BeginExecute FIRST, then CreateInputStream. SSH.NET only assigns the channel inside the execute
        // call, and CreateInputStream dereferences it — called first it throws before the installer ever
        // runs, which is the whole password-sudo path (a non-root admin). Documented in that order by
        // SSH.NET itself. The try/catch covers a command that finished before we got here.
        var async = cmd.BeginExecute();
        Stream? input = null;
        if (stdin is not null)
        {
            try { input = cmd.CreateInputStream(); }
            catch (InvalidOperationException) { input = null; }
        }
        if (input is not null)
        {
            var bytes = Encoding.UTF8.GetBytes(stdin!);
            input.Write(bytes, 0, bytes.Length);
            input.Flush();
            input.Dispose();
        }
        var sb = new StringBuilder();
        using (var reader = new StreamReader(cmd.OutputStream))
        {
            while (!async.IsCompleted || !reader.EndOfStream)
            {
                string? line = reader.ReadLine();
                if (line is null) { Thread.Sleep(100); continue; }
                // Redacted BEFORE it is accumulated: `output` feeds installerFailed's tail, which goes to
                // the error modal and verbatim into relic.log.
                sb.AppendLine(Redact(line, secrets));
                // Only a secret's VALUE stays out of the log (plus the installer's two lines that print the
                // token). The installer's "token   : kept / CHANGES" summary must reach the admin: it is the
                // one warning that this run is about to disconnect every other launcher build; its MUIP
                // key line shows a fingerprint only, so it passes.
                bool leaks = secrets.Any(s => s.Length > 0 && line.Contains(s, StringComparison.Ordinal))
                    || line.Contains("GIO_AGENT_TOKEN=", StringComparison.Ordinal)
                    || line.TrimStart().StartsWith("Agent token", StringComparison.Ordinal)
                    || line.Contains("generated token:", StringComparison.Ordinal);
                if (!leaks) log?.Invoke(line.TrimEnd());
            }
        }
        try { cmd.EndExecute(async); }
        catch (SshOperationTimeoutException)
        {
            // SSH.NET builds the timeout message as "Command '<CommandText>' timed out." — never let that
            // reach the admin or the log: it would quote the whole remote command line.
            throw new InvalidOperationException(L.T("core.deploy.installerTimeout", new { minutes = (int)cmd.CommandTimeout.TotalMinutes }));
        }
        // stderr goes through the same filter as stdout — a sudo/bash diagnostic can echo the assignment.
        string err = Redact(cmd.Error ?? "", secrets);
        if (err.Length > 0)
        {
            sb.AppendLine(err);
            foreach (var l in err.Split('\n')) if (l.Trim().Length > 0) log?.Invoke(l.TrimEnd());
        }
        return (cmd.ExitStatus ?? -1, sb.ToString());
    }

    private static void UploadTree(SftpClient sftp, SshClient ssh, string localDir, string remoteDir)
    {
        RunOut(ssh, $"mkdir -p {Q(remoteDir)}");
        foreach (var f in Directory.GetFiles(localDir)) UploadFile(sftp, f, remoteDir + "/" + Path.GetFileName(f));
        foreach (var d in Directory.GetDirectories(localDir)) UploadTree(sftp, ssh, d, remoteDir + "/" + Path.GetFileName(d));
    }

    private static string Tail(string s, int n) => s.Length <= n ? s : s[^n..];

    private static void UploadFile(SftpClient sftp, string localPath, string remotePath)
    {
        using var fs = File.OpenRead(localPath);
        sftp.UploadFile(fs, remotePath, canOverride: true);
    }

    private static void UploadText(SftpClient sftp, string text, string remotePath)
    {
        using var ms = new MemoryStream(Encoding.UTF8.GetBytes(text.Replace("\r\n", "\n")));
        sftp.UploadFile(ms, remotePath, canOverride: true);
    }
}
