using System.Diagnostics;
using System.Net.Http;
using Microsoft.Win32;
using Relic.Core.Util;

namespace Relic.Core.Server;

/// <summary>
/// The agent config on this PC, parsed the way the agent itself parses it (<c>parse_config_text</c> +
/// <c>apply_config_file</c> in gio_agent.py): BOM tolerated, '#' comments and blank lines skipped,
/// blanks around '=' trimmed, matching surrounding quotes stripped, keys limited to
/// <c>[A-Za-z_][A-Za-z0-9_]*</c>, and the FIRST occurrence of a repeated key wins. Two parsers that
/// disagree would have the launcher dial an agent on a port it never bound.
/// </summary>
public sealed record LocalAgentConfig(IReadOnlyDictionary<string, string> All)
{
    private string Get(string key) => All.TryGetValue(key, out var v) ? v : "";

    public string Token => Get("GIO_AGENT_TOKEN");
    /// <summary>Blank in the file = the agent's own default bind (loopback, 18080).</summary>
    public string Listen => Get("GIO_AGENT_LISTEN") is { Length: > 0 } l ? l : "127.0.0.1:18080";
    public string BindIp => Get("GIO_BIND_IP");
    public string AdvertisedIp => Get("GIO_ADVERTISED_IP");
    public string AdvertisedHost => Get("GIO_ADVERTISED_HOST");
    public string Dir16 => Get("GIO_DIR_16");
    public string Dir28 => Get("GIO_DIR_28");
    public string ServerName => Get("GIO_SERVER_NAME");
    /// <summary>Blank = derived by the agent per version from the stack's <c>.env</c> <c>OUTER_IP</c>. Not a
    /// secret (the sign key is <c>GIO_MUIP_KEY</c>): a re-install form pre-fills it, since the form writes the
    /// key even blank and a blank field would drop a value saved from the Agent settings card.</summary>
    public string MuipHost => Get("GIO_MUIP_HOST");
    public int ListenPort => new AgentInstallPlan { Listen = Listen }.ListenPort;
    public string ListenHost => new AgentInstallPlan { Listen = Listen }.ListenHost;
    /// <summary>The address the launcher saves and dials for this agent — see <see cref="HostFor"/>.</summary>
    public string Host => HostFor(BindIp, ListenHost);

    /// <summary>The ONE rule for "which host do we save for the agent on this PC": the bind IP (reachable
    /// by other launchers on the LAN) only when the agent actually answers on it — a wildcard bind, or a
    /// bind on that very address; otherwise loopback. The install used to save the bind IP even for a
    /// loopback-bound agent, a direct config for an address the agent never bound.</summary>
    public static string HostFor(string bindIp, string listenHost)
    {
        bindIp = (bindIp ?? "").Trim();
        listenHost = (listenHost ?? "").Trim();
        bool wildcard = listenHost is "0.0.0.0" or "::" or "*" or "";
        if (bindIp.Length > 0 && (wildcard || string.Equals(bindIp, listenHost, StringComparison.OrdinalIgnoreCase)))
            return bindIp;
        return "127.0.0.1";
    }
}

/// <summary>What <see cref="LocalAgent.Uninstall"/> did, step by step; <paramref name="Leftovers"/> names
/// the files it could not delete (in use — a hotpatch pack a client is downloading, an open log).</summary>
public sealed record UninstallReport(bool Stopped, bool AutostartRemoved, bool FirewallRemoved, string? FirewallError,
    bool FilesRemoved, IReadOnlyList<string> Leftovers);

/// <summary>
/// The GIO agent running on THIS Windows PC (Docker Desktop + Python). Relic ships the same
/// <c>gio_agent.py</c> it deploys to Linux boxes; here it copies it (plus the provisioning payloads)
/// under <c>%LOCALAPPDATA%\Relic\agent\</c>, writes the KEY=VALUE config, runs the agent's selftest,
/// starts it with <c>pythonw</c> (hidden, logging to a file) and can register a per-user autostart.
/// No admin rights anywhere except the two explicit, user-clicked firewall steps
/// (<see cref="OpenFirewallPort"/> at install, <see cref="RemoveFirewallRule"/> at uninstall), which
/// Windows cannot do unelevated.
/// </summary>
public static class LocalAgent
{
    /// <summary>Spikes only: point every path at a temp folder so a test never installs into, stops or
    /// deletes the developer's real agent (state file, hotpatch mirror). Null = the real root.</summary>
    public static string? RootOverride { get; set; }

    public static string Root => RootOverride ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Relic", "agent");
    public static string ConfigPath => Path.Combine(Root, "config");
    public static string ScriptPath => Path.Combine(Root, "gio_agent.py");
    public static string PayloadsDir => Path.Combine(Root, "payloads");
    public static string LogPath => Path.Combine(Root, "agent.log");
    public static string PidPath => Path.Combine(Root, "agent.pid");
    public static string PythonPath => Path.Combine(Root, "python.txt");
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValue = "RelicGioAgent";

    /// <summary>Where the shipped agent lives next to the exe (copied by the csproj / publish).</summary>
    public static string ShippedAgentDir => Path.Combine(AppContext.BaseDirectory, "agent");

    public sealed record PythonInfo(string Exe, string ExeW, string Version);

    public static bool IsInstalled => File.Exists(ConfigPath) && File.Exists(ScriptPath);

    /// <summary>Find a usable CPython ≥ 3.10: the <c>py</c> launcher first, then <c>python</c> on PATH.
    /// Returns null when none qualifies (the UI then shows install guidance).</summary>
    public static PythonInfo? FindPython()
    {
        foreach (var (exe, args) in new[] { ("py", "-3 -c"), ("python", "-c"), ("python3", "-c") })
        {
            try
            {
                var psi = new ProcessStartInfo(exe)
                {
                    Arguments = args + " \"import sys;print(sys.executable);print('%d.%d'%sys.version_info[:2])\"",
                    UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
                };
                using var p = Process.Start(psi);
                if (p is null) continue;
                string output = p.StandardOutput.ReadToEnd();
                if (!p.WaitForExit(8000)) { try { p.Kill(); } catch { } continue; }
                if (p.ExitCode != 0) continue;
                var lines = output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                if (lines.Length < 2) continue;
                string path = lines[0], ver = lines[1];
                var parts = ver.Split('.');
                if (parts.Length < 2 || !int.TryParse(parts[0], out int maj) || !int.TryParse(parts[1], out int min)) continue;
                if (maj < 3 || (maj == 3 && min < 10)) continue;
                if (!File.Exists(path)) continue;
                // WindowsApps stubs (the Store alias) resolve through an app-execution alias; that is fine.
                string exeW = Path.Combine(Path.GetDirectoryName(path)!, "pythonw.exe");
                if (!File.Exists(exeW)) exeW = path;
                return new PythonInfo(path, exeW, ver);
            }
            catch { /* next candidate */ }
        }
        return null;
    }

    /// <summary>A 7z extractor the agent on this PC can use for the server-package download (agent 3.4,
    /// the same candidates as its <c>find_extractor()</c>): 7-Zip's 7z.exe in its usual homes or on PATH,
    /// Bandizip's bz.exe, else Windows' own <c>System32\tar.exe</c> — bsdtar, accepted only when it was
    /// built with liblzma (it reads LZMA2 .7z then), and always by FULL path: Git for Windows puts a GNU
    /// tar on PATH that shadows it and knows no 7z at all. Null = nothing usable.</summary>
    public static string? FindExtractor()
    {
        var candidates = new List<string>();
        foreach (var env in new[] { "ProgramFiles", "ProgramFiles(x86)" })
        {
            string? pf = Environment.GetEnvironmentVariable(env);
            if (!string.IsNullOrEmpty(pf)) candidates.Add(Path.Combine(pf, "7-Zip", "7z.exe"));
        }
        candidates.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "7-Zip", "7z.exe"));
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            try { candidates.Add(Path.Combine(dir, "7z.exe")); } catch { /* an unusable PATH entry */ }
        }
        string? pf64 = Environment.GetEnvironmentVariable("ProgramFiles");
        if (!string.IsNullOrEmpty(pf64)) candidates.Add(Path.Combine(pf64, "Bandizip", "bz.exe"));
        foreach (var c in candidates)
        {
            try { if (File.Exists(c)) return c; } catch { /* next */ }
        }
        string sysRoot = Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows";
        string tar = Path.Combine(sysRoot, "System32", "tar.exe");
        try
        {
            if (File.Exists(tar))
            {
                var res = Proc.Run(tar, "--version");
                if (res.Ok && (res.Out + res.Err).Contains("liblzma", StringComparison.OrdinalIgnoreCase)) return tar;
            }
        }
        catch (Exception ex) { Log.Info($"tar.exe probe: {ex.Message}"); }
        return null;
    }

    /// <summary>Copy the shipped agent + payloads, write the config (merged with the previous one, see
    /// <see cref="MergeConfig(string, string?)"/>), run <c>--selftest</c>. Does not start the agent. Throws
    /// with a user-facing message on failure.</summary>
    public static IReadOnlyList<string> Install(AgentInstallPlan plan, PythonInfo python, Action<string>? log = null, string? agentDir = null)
    {
        if (!plan.IsWindows) throw new InvalidOperationException("LocalAgent.Install: plan target is not windows");
        var errs = plan.Validate();
        if (errs.Count > 0) throw new InvalidOperationException(string.Join("\n", errs));
        // A version marked for download is skipped by the stack check — but the agent will have to
        // EXTRACT what it downloads, and on Windows nothing guarantees a 7z tool: refuse now, with the
        // fix in the message, rather than let a 2 GB download fail at its very last step.
        var missing = plan.MissingStackEntries(p => Directory.Exists(p) || File.Exists(p));
        if (missing.Count > 0) throw new InvalidOperationException(L.T("core.deploy.stackIncomplete", new { list = string.Join(", ", missing) }));
        if (plan.FetchVersions().Count > 0 && FindExtractor() is null)
            throw new InvalidOperationException(L.T("core.deploy.noExtractorWindows"));

        string src = agentDir ?? ShippedAgentDir;
        string srcScript = Path.Combine(src, "gio_agent.py");
        if (!File.Exists(srcScript)) throw new InvalidOperationException(L.T("core.deploy.agentMissingInBuild", new { path = srcScript }));
        Directory.CreateDirectory(Root);
        log?.Invoke(L.T("core.deploy.copyingAgent", new { dir = Root }));
        File.Copy(srcScript, ScriptPath, overwrite: true);
        string srcPayloads = Path.Combine(src, "payloads");
        if (Directory.Exists(srcPayloads))
        {
            if (Directory.Exists(PayloadsDir)) Directory.Delete(PayloadsDir, recursive: true);
            CopyTree(srcPayloads, PayloadsDir);
        }
        // MERGE, never overwrite: the form renders only the keys it manages, and everything else in the
        // file — set from the Agent settings card (agent 3.6) or added by hand (GIO_FETCH_DISK_RESERVE,
        // HTTPS_PROXY, …) — survives the re-install, like install_agent.sh keeps it on Linux. An unreadable
        // file keeps nothing (it is replaced either way); values are never logged, only the key names.
        string? previous = null;
        try { if (File.Exists(ConfigPath)) previous = File.ReadAllText(ConfigPath); }
        catch (Exception ex) { Log.Error("reading the previous local agent config (non-fatal: nothing is kept)", ex); }
        string config = MergeConfig(plan.RenderConfig(), previous, out var kept);
        File.WriteAllText(ConfigPath, config, new System.Text.UTF8Encoding(false));
        if (kept.Count > 0)
        {
            Log.Info($"local agent install: kept from the previous config: {string.Join(", ", kept)}");
            log?.Invoke(L.T("core.deploy.keptConfig", new { keys = string.Join(", ", kept) }));
        }
        File.WriteAllText(PythonPath, python.Exe + Environment.NewLine + python.ExeW, new System.Text.UTF8Encoding(false));
        // Same rule as install_agent.sh: a folder that already holds a stack is not downloaded. The
        // caller uses the returned list to queue the fetch jobs, so a version skipped here is never
        // queued and can never fail with the agent's 409 "already on this server".
        var willFetch = plan.FetchVersionsReally(p => Directory.Exists(p) || File.Exists(p));
        foreach (var v in plan.FetchVersions())
            log?.Invoke(willFetch.Contains(v)
                ? L.T("core.deploy.willFetch", new { version = v, dir = plan.DirFor(v) })
                : L.T("core.deploy.fetchSkipped", new { version = v, dir = plan.DirFor(v) }));

        log?.Invoke(L.T("core.deploy.selftest"));
        var (code, output) = Run(python.Exe, $"\"{ScriptPath}\" --selftest", Root, timeoutMs: 120_000);
        Log.Info($"local agent selftest exit={code}\n{Tail(output, 2000)}");
        if (code != 0) throw new InvalidOperationException(L.T("core.deploy.selftestFailed", new { tail = Tail(output, 600) }));
        log?.Invoke(L.T("core.deploy.selftestOk"));
        return willFetch;
    }

    /// <summary>The one comment line <see cref="MergeConfig(string, string?)"/> puts above the kept keys.</summary>
    public const string KeptConfigHeader = "# Kept from the previous configuration (Agent settings or hand edits):";

    /// <summary>A config key as the agent accepts it (<c>parse_config_text</c>).</summary>
    private static readonly System.Text.RegularExpressions.Regex ConfigKeyRe = new("^[A-Za-z_][A-Za-z0-9_]*$");

    /// <summary>The key of one config line, read exactly as <see cref="ReadConfig"/> reads it; null for a
    /// comment, a blank line, a line without '=' or a key the agent would not accept.</summary>
    private static string? ConfigLineKey(string raw)
    {
        string line = raw.Trim();
        if (line.Length == 0 || line[0] == '#') return null;
        int eq = line.IndexOf('=');
        if (eq < 0) return null;
        string k = line[..eq].Trim();
        return ConfigKeyRe.IsMatch(k) ? k : null;
    }

    /// <summary>The config a (re-)install writes: <paramref name="rendered"/> (the install form,
    /// <see cref="AgentInstallPlan.RenderConfig"/>) followed — under the one line
    /// <see cref="KeptConfigHeader"/> — by every KEY=VALUE line of the <paramref name="existing"/> file whose
    /// key the rendered text does not name: its FIRST occurrence (the agent's <c>--config</c> reading is
    /// first-wins), written as it was spelled there (trimmed; quotes and blanks around '=' kept, the agent
    /// parses them the same way), in the file's order. Comments, blank lines, non-pairs and bad keys are
    /// dropped — the previous header included, so a merge of a merge adds nothing. A key the form renders,
    /// even SET AND EMPTY, is the form's: its old line never comes back behind the new one. The keys the
    /// form omits when blank (the provision / txt-fix modes) are therefore kept, the same "blank keeps
    /// the box's value" rule install_agent.sh applies. Null / empty existing, or nothing to keep: the
    /// rendered text unchanged, no header. A BOM and CRLF in the old file are tolerated; the output uses
    /// LF. Pure — no I/O.</summary>
    public static string MergeConfig(string rendered, string? existing) => MergeConfig(rendered, existing, out _);

    /// <summary><see cref="MergeConfig(string, string?)"/>, also naming the keys it kept (for the install log —
    /// the names only: a kept value may hold a proxy password).</summary>
    public static string MergeConfig(string rendered, string? existing, out IReadOnlyList<string> keptKeys)
    {
        rendered ??= "";
        var managed = new HashSet<string>(StringComparer.Ordinal);
        foreach (var raw in rendered.Split('\n'))
            if (ConfigLineKey(raw) is { } k) managed.Add(k);
        var keys = new List<string>();
        var lines = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        if (!string.IsNullOrEmpty(existing))
            foreach (var raw in existing.TrimStart('﻿').Split('\n'))
            {
                if (ConfigLineKey(raw) is not { } k || managed.Contains(k) || !seen.Add(k)) continue;
                keys.Add(k);
                lines.Add(raw.Trim());
            }
        keptKeys = keys;
        if (keys.Count == 0) return rendered;
        var sb = new System.Text.StringBuilder(rendered);
        if (sb.Length > 0 && sb[^1] != '\n') sb.Append('\n');
        sb.Append(KeptConfigHeader).Append('\n');
        foreach (var l in lines) sb.Append(l).Append('\n');
        return sb.ToString();
    }

    /// <summary>The config file on this PC, parsed like the agent parses it; null when there is none (or it
    /// cannot be read — logged, never thrown: the start screen calls this on every init).</summary>
    public static LocalAgentConfig? ReadConfig()
    {
        try
        {
            if (!File.Exists(ConfigPath)) return null;
            var d = new Dictionary<string, string>(StringComparer.Ordinal);
            // ReadAllText already drops a UTF-8 BOM; the explicit trim covers a file written without one
            // being read through a different detection path.
            foreach (var raw in File.ReadAllText(ConfigPath).TrimStart('\uFEFF').Split('\n'))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == '#') continue;
                int eq = line.IndexOf('=');
                if (eq < 0) continue;
                string k = line[..eq].Trim(), v = line[(eq + 1)..].Trim();
                if (v.Length >= 2 && v[0] == v[^1] && v[0] is '"' or '\'') v = v[1..^1];
                if (!ConfigKeyRe.IsMatch(k)) continue;
                // FIRST occurrence wins: apply_config_file only sets a key the environment lacks.
                if (!d.ContainsKey(k)) d[k] = v;
            }
            return new LocalAgentConfig(d);
        }
        catch (Exception ex)
        {
            Log.Error("reading the local agent config (non-fatal)", ex);
            return null;
        }
    }

    /// <summary>Start the agent hidden with <c>pythonw</c> (stdout/stderr go to <see cref="LogPath"/>
    /// through the agent's own <c>--log</c>). Returns the pid. Idempotent: a healthy running agent is
    /// left alone.</summary>
    public static async Task<int> StartAsync(int port, Action<string>? log = null)
    {
        if (!IsInstalled) throw new InvalidOperationException(L.T("core.deploy.notInstalledLocally"));
        var (running, pid) = RunningPid();
        if (running && await IsHealthyAsync(port).ConfigureAwait(false)) { log?.Invoke(L.T("core.deploy.alreadyRunning")); return pid; }
        var python = ReadPython() ?? FindPython() ?? throw new InvalidOperationException(L.T("core.deploy.noPython"));
        var psi = new ProcessStartInfo(python.ExeW)
        {
            Arguments = $"\"{ScriptPath}\" --config \"{ConfigPath}\" --log \"{LogPath}\"",
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        var p = Process.Start(psi) ?? throw new InvalidOperationException(L.T("core.deploy.startFailed"));
        File.WriteAllText(PidPath, p.Id.ToString());
        log?.Invoke(L.T("core.deploy.started", new { pid = p.Id }));
        for (int i = 0; i < 40; i++)
        {
            await Task.Delay(250).ConfigureAwait(false);
            if (p.HasExited) throw new InvalidOperationException(L.T("core.deploy.exitedEarly", new { code = p.ExitCode, log = LogPath }));
            if (await IsHealthyAsync(port).ConfigureAwait(false)) return p.Id;
        }
        throw new InvalidOperationException(L.T("core.deploy.noHealth", new { port, log = LogPath }));
    }

    /// <summary>Stop the agent on this PC — the one the pid file names, or, when that file is gone or
    /// stale (an autostarted agent never has one), the python process LISTENING on <paramref name="port"/>.
    /// Kills the whole tree, then waits (≤ 3 s) until /health stops answering, so a StartAsync right
    /// after never races the dying process for the port.</summary>
    public static void Stop(int port, Action<string>? log = null)
    {
        var (running, pid) = FindRunning(port);
        if (!running)
        {
            log?.Invoke(L.T("core.deploy.notRunning"));
            try { File.Delete(PidPath); } catch { }
            return;
        }
        try
        {
            using var p = Process.GetProcessById(pid);
            p.Kill(entireProcessTree: true);
            p.WaitForExit(5000);
            log?.Invoke(L.T("core.deploy.stopped"));
        }
        catch (Exception ex) { Log.Info($"local agent stop (pid {pid}): {ex.Message}"); }
        for (int i = 0; i < 12; i++)
        {
            if (!IsHealthyAsync(port, 500).GetAwaiter().GetResult()) break;
            Thread.Sleep(250);
        }
        try { File.Delete(PidPath); } catch { }
    }

    /// <summary>Stop on the port the config names (18080 when there is no config).</summary>
    public static void Stop(Action<string>? log = null) => Stop(ReadConfig()?.ListenPort ?? ServerAddress.DefaultAgentPort, log);

    /// <summary>(running, pid) from the pid file, verified against a live python process.</summary>
    public static (bool Running, int Pid) RunningPid()
    {
        try
        {
            if (!File.Exists(PidPath)) return (false, 0);
            if (!int.TryParse(File.ReadAllText(PidPath).Trim(), out int pid)) return (false, 0);
            using var p = Process.GetProcessById(pid);
            if (p.HasExited) return (false, pid);
            string name = p.ProcessName.ToLowerInvariant();
            return (name.Contains("python"), pid);
        }
        catch { return (false, 0); }
    }

    /// <summary>The agent process on this PC whether or not WE started it: the pid file first, else the
    /// python process listening on <paramref name="port"/> (an agent started by the HKCU Run value writes
    /// no pid file and used to read as "installed, stopped" — a second start then died on the bound
    /// port). Same "is it python" test as <see cref="RunningPid"/>.</summary>
    public static (bool Running, int Pid) FindRunning(int port)
    {
        var byFile = RunningPid();
        if (byFile.Running) return byFile;
        int? pid = ListeningPid(port);
        if (pid is null || pid.Value <= 0) return (false, 0);
        try
        {
            using var p = Process.GetProcessById(pid.Value);
            if (p.HasExited) return (false, 0);
            return (p.ProcessName.ToLowerInvariant().Contains("python"), pid.Value);
        }
        catch { return (false, 0); }
    }

    /// <summary>Owner pid of the TCP socket LISTENING on <paramref name="port"/> (IPv4 and IPv6 tables),
    /// or null. <c>netstat -ano</c> is shelled out unelevated — the repo's "no new package" rule
    /// (GameProcessWatcher does the same with PowerShell as a last tier). A listening row is recognised
    /// by its foreign address ending in ":0" as well as by the state word, so a localised Windows whose
    /// netstat prints the state in another language still answers.</summary>
    public static int? ListeningPid(int port)
    {
        string suffix = ":" + port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        foreach (var proto in new[] { "tcp", "tcpv6" })
        {
            Proc.Result res;
            try { res = Proc.Run("netstat", "-ano", "-p", proto); }
            catch (Exception ex) { Log.Info($"netstat -p {proto}: {ex.Message}"); continue; }
            if (!res.Ok) continue;
            foreach (var raw in res.Out.Split('\n'))
            {
                var cols = raw.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (cols.Length < 5 || !cols[0].StartsWith("TCP", StringComparison.OrdinalIgnoreCase)) continue;
                if (!cols[1].EndsWith(suffix, StringComparison.Ordinal)) continue;
                bool listening = cols[3].Contains("LISTEN", StringComparison.OrdinalIgnoreCase)
                    || cols[2].EndsWith(":0", StringComparison.Ordinal);
                if (!listening) continue;
                if (int.TryParse(cols[^1], out int pid) && pid > 0) return pid;
            }
        }
        return null;
    }

    /// <remarks>ConfigureAwait(false) is load-bearing, not decoration: <see cref="Stop"/> blocks on this
    /// method, and Stop used to be called straight from the WebView2 message handler — i.e. on the UI
    /// thread. With the continuation posted back to that same (blocked) thread the launcher deadlocked
    /// for good: no repaint, no further rpc reply, so every later screen — the command catalogue above
    /// all — sat on its spinner forever. The blocking callers now run off the UI thread as well, and
    /// this keeps the method safe to block on from anywhere.</remarks>
    public static async Task<bool> IsHealthyAsync(int port, int timeoutMs = 1500)
    {
        try
        {
            using var http = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = TimeSpan.FromMilliseconds(timeoutMs) };
            string body = await http.GetStringAsync($"http://127.0.0.1:{port}/health").ConfigureAwait(false);
            return body.Contains("gio-agent");
        }
        catch { return false; }
    }

    public static bool AutostartEnabled
    {
        get
        {
            try
            {
                using var k = Registry.CurrentUser.OpenSubKey(RunKey, writable: false);
                return k?.GetValue(RunValue) is string s && s.Length > 0;
            }
            catch { return false; }
        }
    }

    /// <summary>Per-user autostart via HKCU\...\Run (no admin; <c>schtasks /sc onlogon</c> would need it).</summary>
    public static void SetAutostart(bool on)
    {
        using var k = Registry.CurrentUser.CreateSubKey(RunKey, writable: true) ?? throw new InvalidOperationException("HKCU Run key unavailable");
        if (!on) { k.DeleteValue(RunValue, throwOnMissingValue: false); return; }
        var python = ReadPython() ?? FindPython() ?? throw new InvalidOperationException(L.T("core.deploy.noPython"));
        k.SetValue(RunValue, $"\"{python.ExeW}\" \"{ScriptPath}\" --config \"{ConfigPath}\" --log \"{LogPath}\"", RegistryValueKind.String);
    }

    /// <summary>The Run value's command line, or null when there is none.</summary>
    private static string? AutostartCommand()
    {
        try
        {
            using var k = Registry.CurrentUser.OpenSubKey(RunKey, writable: false);
            return k?.GetValue(RunValue) as string;
        }
        catch { return null; }
    }

    /// <summary>The one name the add/show/delete netsh calls share, so they can never drift apart.</summary>
    public static string FirewallRuleName(int port) => $"Relic GIO agent ({port})";

    /// <summary>The first of the two elevations in Relic, admin-mode only and user-clicked: an inbound
    /// allow rule for the agent's TCP port scoped to the python executable. Returns null on success,
    /// else the error text (a declined UAC prompt is a normal outcome).</summary>
    public static string? OpenFirewallPort(int port)
    {
        try
        {
            var python = ReadPython() ?? FindPython();
            string program = python?.ExeW ?? "";
            string rule = $"advfirewall firewall add rule name=\"{FirewallRuleName(port)}\" dir=in action=allow protocol=TCP localport={port}" +
                          (program.Length > 0 ? $" program=\"{program}\"" : "");
            var psi = new ProcessStartInfo("netsh.exe", rule) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
            using var p = Process.Start(psi);
            if (p is null) return "netsh did not start";
            if (!p.WaitForExit(60_000)) return "netsh timed out";
            return p.ExitCode == 0 ? null : $"netsh exit code {p.ExitCode}";
        }
        catch (System.ComponentModel.Win32Exception ex) when (ex.NativeErrorCode == 1223)
        {
            return L.T("core.deploy.uacDeclined");
        }
        catch (Exception ex) { return ex.Message; }
    }

    /// <summary>Is our inbound rule for <paramref name="port"/> present? Read unelevated — showing rules
    /// needs no rights; netsh answers exit 1 ("No rules match") when there is none.</summary>
    public static bool FirewallRuleExists(int port)
    {
        try
        {
            var res = Proc.Run("netsh", "advfirewall", "firewall", "show", "rule", "name=" + FirewallRuleName(port));
            return res.Ok && !res.Out.Contains("No rules match", StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception ex) { Log.Info($"netsh show rule: {ex.Message}"); return false; }
    }

    /// <summary>The second elevation, the mirror of <see cref="OpenFirewallPort"/>: delete the rule that
    /// call added. User-clicked (the uninstall dialog's checkbox, off by default), never run from the
    /// launcher's own --uninstall-cleanup. Null on success, else the error text (UAC declined =
    /// <c>core.deploy.uacDeclined</c>).</summary>
    public static string? RemoveFirewallRule(int port)
    {
        try
        {
            string rule = $"advfirewall firewall delete rule name=\"{FirewallRuleName(port)}\"";
            var psi = new ProcessStartInfo("netsh.exe", rule) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
            using var p = Process.Start(psi);
            if (p is null) return "netsh did not start";
            if (!p.WaitForExit(60_000)) return "netsh timed out";
            return p.ExitCode == 0 ? null : $"netsh exit code {p.ExitCode}";
        }
        catch (System.ComponentModel.Win32Exception ex) when (ex.NativeErrorCode == 1223)
        {
            // Its own text: the "was not added" of OpenFirewallPort would tell the admin the opposite
            // of what happened and hide that the inbound rule outlives the agent.
            return L.T("core.deploy.uacDeclinedRemove");
        }
        catch (Exception ex) { return ex.Message; }
    }

    /// <summary>
    /// Remove the agent from this PC in the order <c>uninstall_agent.sh</c> uses on Linux: the process
    /// (pid file or listener), the autostart value — only when it points at THIS root, so a spike root
    /// never touches the real one — the firewall rule when asked (the one elevated step), then every
    /// file under <see cref="Root"/> entry by entry, bottom-up, one retry after a second: a single locked
    /// file (a voice pack a client is being served, an open log) must cost that file alone, not the whole
    /// cleanup, and is reported in the leftovers. Nothing outside the root is ever touched — the docker
    /// stacks the config names are only NAMED in the confirm dialog. Throws only off Windows.
    /// </summary>
    public static UninstallReport Uninstall(int port, bool removeFirewall, Action<string>? log = null)
    {
        if (!OperatingSystem.IsWindows()) throw new InvalidOperationException(L.T("backend.localAgent.windowsOnly"));
        var (wasRunning, _) = FindRunning(port);
        Stop(port, log);
        bool stopped = wasRunning && !FindRunning(port).Running;

        bool autostartRemoved = false;
        try
        {
            string? cmd = AutostartCommand();
            if (cmd is not null && cmd.Contains(ScriptPath, StringComparison.OrdinalIgnoreCase))
            {
                SetAutostart(false);
                autostartRemoved = !AutostartEnabled;
                if (autostartRemoved) log?.Invoke(L.T("core.deploy.autostartOff"));
            }
        }
        catch (Exception ex) { Log.Error("removing the agent autostart value (non-fatal)", ex); }

        bool firewallRemoved = false;
        string? firewallError = null;
        if (removeFirewall && FirewallRuleExists(port))
        {
            firewallError = RemoveFirewallRule(port);
            firewallRemoved = firewallError is null;
            if (firewallRemoved) log?.Invoke(L.T("core.deploy.firewallRemoved"));
        }

        var leftovers = new List<string>();
        string root = Root;
        if (Directory.Exists(root))
        {
            DeleteTreeEntryByEntry(root, leftovers);
            if (leftovers.Count == 0)
            {
                try { Directory.Delete(root); }
                catch (Exception ex) { leftovers.Add(L.T("core.deploy.fileLeft", new { path = root, error = ex.Message })); }
            }
        }
        bool filesRemoved = !Directory.Exists(root);
        if (filesRemoved) log?.Invoke(L.T("core.deploy.filesRemoved", new { dir = root }));
        else foreach (var l in leftovers) log?.Invoke(l);
        return new UninstallReport(stopped, autostartRemoved, firewallRemoved, firewallError, filesRemoved, leftovers);
    }

    /// <summary>Delete <paramref name="dir"/>'s contents one entry at a time (never
    /// <c>Directory.Delete(recursive: true)</c>, which aborts everything at the first locked file),
    /// deepest first, one retry after 1 s per failure; failures are collected, never thrown.</summary>
    private static void DeleteTreeEntryByEntry(string dir, List<string> leftovers)
    {
        string[] subdirs, files;
        try { subdirs = Directory.GetDirectories(dir); files = Directory.GetFiles(dir); }
        catch (Exception ex) { leftovers.Add(L.T("core.deploy.fileLeft", new { path = dir, error = ex.Message })); return; }
        foreach (var sub in subdirs)
        {
            // A junction/symlink is deleted as the LINK, never walked: the hotpatch mirror lives
            // inside this root and is exactly the thing an admin redirects to another drive with
            // `mklink /J`. Recursing into it would delete the target's contents — outside the root
            // this method promises never to leave.
            try
            {
                if (new DirectoryInfo(sub).Attributes.HasFlag(FileAttributes.ReparsePoint))
                {
                    TryTwice(sub, leftovers, () => Directory.Delete(sub));
                    continue;
                }
            }
            catch (Exception ex) { leftovers.Add(L.T("core.deploy.fileLeft", new { path = sub, error = ex.Message })); continue; }
            DeleteTreeEntryByEntry(sub, leftovers);
            // A folder that still holds a leftover cannot go, and saying so twice (file + folder) would
            // only pad the report: the file's own line already explains why the folder stayed.
            bool empty;
            try { empty = !Directory.EnumerateFileSystemEntries(sub).Any(); }
            catch { empty = false; }
            if (empty) TryTwice(sub, leftovers, () => Directory.Delete(sub));
        }
        foreach (var f in files)
            TryTwice(f, leftovers, () =>
            {
                File.SetAttributes(f, FileAttributes.Normal);
                File.Delete(f);
            });
    }

    private static void TryTwice(string path, List<string> leftovers, Action del)
    {
        for (int attempt = 1; ; attempt++)
        {
            try { del(); return; }
            catch (DirectoryNotFoundException) { return; }
            catch (FileNotFoundException) { return; }
            catch (Exception ex)
            {
                if (attempt == 1) { Thread.Sleep(1000); continue; }
                Log.Info($"local agent uninstall: {path}: {ex.Message}");
                leftovers.Add(L.T("core.deploy.fileLeft", new { path, error = ex.Message }));
                return;
            }
        }
    }

    private static PythonInfo? ReadPython()
    {
        try
        {
            if (!File.Exists(PythonPath)) return null;
            var lines = File.ReadAllLines(PythonPath);
            if (lines.Length == 0 || !File.Exists(lines[0])) return null;
            string w = lines.Length > 1 && File.Exists(lines[1]) ? lines[1] : lines[0];
            return new PythonInfo(lines[0], w, "");
        }
        catch { return null; }
    }

    private static (int Code, string Output) Run(string exe, string args, string cwd, int timeoutMs)
    {
        var psi = new ProcessStartInfo(exe, args)
        {
            WorkingDirectory = cwd, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
        };
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        using var p = Process.Start(psi) ?? throw new InvalidOperationException($"cannot start {exe}");
        var sb = new System.Text.StringBuilder();
        p.OutputDataReceived += (_, e) => { if (e.Data is not null) lock (sb) sb.AppendLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data is not null) lock (sb) sb.AppendLine(e.Data); };
        p.BeginOutputReadLine(); p.BeginErrorReadLine();
        if (!p.WaitForExit(timeoutMs)) { try { p.Kill(true); } catch { } return (-1, sb.ToString() + "\n(timeout)"); }
        p.WaitForExit();
        return (p.ExitCode, sb.ToString());
    }

    private static void CopyTree(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var f in Directory.GetFiles(src)) File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), overwrite: true);
        foreach (var d in Directory.GetDirectories(src)) CopyTree(d, Path.Combine(dst, Path.GetFileName(d)));
    }

    private static string Tail(string s, int n) => s.Length <= n ? s : s[^n..];
}
