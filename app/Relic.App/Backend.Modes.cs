using System.Text.Json;
using System.Text.Json.Nodes;
using Relic.Core.Fiddler;
using Relic.Core.Install;
using Relic.Core.Server;
using Relic.Core.State;
using Relic.Core.Util;

namespace Relic.App;

/// <summary>
/// Player / Server Admin modes, the start-screen flows (server address, TXT port discovery,
/// connection test, admin token), per-version removal, the local Windows agent, fresh-server agent
/// installs, player self-service accounts and the admin account policy. Mode is a UX gate — the
/// agent bearer token is the security boundary: every admin rpc goes through <see cref="RequireAdmin"/>.
/// </summary>
public sealed partial class Backend
{
    // ── mode + server address ──

    private bool IsAdminMode => string.Equals(_state.Settings.Mode, "admin", StringComparison.OrdinalIgnoreCase);

    /// <summary>Throws when no server address is configured yet (a build without a baked server, before
    /// the start screen was completed): playing, installing and the cert step all template the
    /// Fiddler rules for the server, which need a host.</summary>
    private void RequireServerAddress()
    {
        if (string.IsNullOrWhiteSpace(_state.Settings.ServerHost))
            throw new InvalidOperationException(L.T("backend.noServer"));
    }

    /// <summary>Throws unless the app is in admin mode AND has an agent token.</summary>
    private void RequireAdmin()
    {
        if (!IsAdminMode || AdminServer() is null)
            throw new InvalidOperationException(L.T("backend.adminOnly"));
    }

    /// <summary>The agent connection for ADMIN calls. The HOST is always the configured server address
    /// (start screen / agent install → <c>Settings.ServerHost</c>); server.json only lends its token
    /// (and SSH details) when it was saved for that same host, else the build's baked token applies.
    /// Null when no token applies — see <see cref="ServerConfig.Effective"/>. (Before 2026-08-21 the
    /// saved file won outright, so changing the address on the start screen changed only the label
    /// while every call still went to the previous server.)</summary>
    private ServerConfig? AdminServer() =>
        ServerConfig.Effective(StoredServer(), _state.Settings.ServerHost, _state.Settings.AgentPort);

    /// <summary>server.json as saved by the user (null when the app runs on the build's defaults).</summary>
    private ServerConfig? StoredServer() => _serverSaved ? _server : null;

    /// <summary>Token-less client for the configured server (player mode status, signup).</summary>
    private AgentPublicClient PublicClient()
    {
        string host = _state.Settings.ServerHost;
        if (string.IsNullOrWhiteSpace(host)) throw new InvalidOperationException(L.T("backend.noServer"));
        return new AgentPublicClient(host, _state.Settings.AgentPort);
    }

    private object SetMode(string? mode)
    {
        mode = (mode ?? "").Trim().ToLowerInvariant();
        if (mode is not ("player" or "admin" or "")) throw new InvalidOperationException(L.T("backend.badMode"));
        _state.Settings.Mode = mode;
        _state.Save();
        return BuildInitState();
    }

    /// <summary>Parse + resolve (TXT) the server address typed on the start screen — no side effects.</summary>
    private static async Task<ServerAddress.Resolved> ResolveAddressAsync(string input)
    {
        if (!ServerAddress.TryParse(input, out var addr, out var err) || addr is null)
            throw new InvalidOperationException(L.T("backend.badAddress", new { detail = err ?? "" }));
        return await addr.ResolveAsync(timeout: TimeSpan.FromSeconds(3));
    }

    /// <summary>The agent port to use for a resolved address: re-entering the SAME host keeps the agent
    /// port already configured for it (an agent installed on a custom listen port, the start screen's
    /// agent-port field) unless the resolution actually LEARNED one. Only a TXT record publishing <c>agent=</c> does
    /// that, which is why this reads <c>AgentPortFromTxt</c> rather than comparing AgentPort against
    /// the default: a record that publishes agent=18080 really did learn it. By contrast
    /// an explicit GAME port ("192.0.2.10:21000", which the start-screen placeholder invites) carries no
    /// information about the agent port, yet used to reset it to 18080 and lock admin mode out.
    /// Switching BACK to a server whose agent listens on a custom port compares against the host being
    /// left, so the agent port saved with that server's token in server.json is the next witness —
    /// without it the switch dialled 18080 and read as "unreachable".</summary>
    private int AgentPortFor(ServerAddress.Resolved r)
    {
        var s = _state.Settings;
        if (r.AgentPortFromTxt is not null) return r.AgentPort;
        if (ServerConfig.SameHost(s.ServerHost, r.Host) && s.AgentPort > 0) return s.AgentPort;
        if (StoredServer() is { } stored && ServerConfig.SameHost(stored.Host, r.Host) && stored.AgentPort > 0)
            return stored.AgentPort;
        return r.AgentPort;
    }

    /// <summary>Persist a resolved address (host, game port, agent port, where the port came from).
    /// <paramref name="agentPortOverride"/>: the start screen's explicit agent port (validated by the
    /// caller), which beats every inference of <see cref="AgentPortFor"/>.</summary>
    private void ApplyAddress(ServerAddress.Resolved r, int? agentPortOverride = null)
    {
        var s = _state.Settings;
        int agentPort = agentPortOverride ?? AgentPortFor(r);
        s.ServerHost = r.Host;
        s.ServerPort = r.Port;
        s.AgentPort = agentPort;
        s.ServerPortSource = r.Source;
        s.ServerResolvedAt = DateTime.UtcNow.ToString("o");
        _state.Save();
        Log.Info($"server address set: {r.Host}:{r.Port} agent={agentPort} source={r.Source}");
    }

    /// <summary>Point the launcher at an agent we have just installed, applying the address as a WHOLE the
    /// way <see cref="ApplyAddress"/> does. Both install targets share it: they each used to hand-roll a
    /// partial copy that moved ServerHost but left <c>ServerPort</c> on the PREVIOUS server's game port,
    /// so the next PLAY templated the Fiddler redirect to newhost:oldport and the client hung on login.
    /// The agent port is the one we just told the agent to bind, so it wins. Never throws — both callers
    /// run inside a background job whose exception would surface as a deploy failure.</summary>
    private void ApplyInstalledAgent(string host, int agentPort)
    {
        var s = _state.Settings;
        if (!ServerConfig.SameHost(s.ServerHost, host))
        {
            // A different box: nothing is known about its GAME port yet. Drop back to the default and let
            // the next start-screen entry re-resolve it (TXT included) rather than keep a foreign port.
            s.ServerPort = ServerAddress.DefaultGamePort;
            s.ServerPortSource = ServerAddress.SourceDefault;
        }
        s.ServerHost = host;
        if (agentPort > 0) s.AgentPort = agentPort;
        s.ServerResolvedAt = DateTime.UtcNow.ToString("o");
        s.Mode = "admin";
        _state.Save();
        Log.Info($"agent installed: host={host} agent={s.AgentPort} game={s.ServerPort} source={s.ServerPortSource}");
    }

    /// <summary>Parse + resolve + persist the server address typed on the start screen (player mode).</summary>
    private async Task<object> SetServerAddressAsync(string input)
    {
        var r = await ResolveAddressAsync(input);
        ApplyAddress(r);
        return new { host = r.Host, port = r.Port, agentPort = _state.Settings.AgentPort, source = r.Source };
    }

    /// <summary>A typed agent port from the start screen: 1-65535 counts, anything else (0 = the field
    /// was left empty) means "infer it" (<see cref="AgentPortFor"/>).</summary>
    private static int? ExplicitAgentPort(int agentPort) => agentPort is > 0 and <= 65535 ? agentPort : null;

    /// <summary>Connection test for the start screen: resolve, probe the game port (sdk) and the
    /// agent's public status. Never throws for an unreachable server — the result says so.</summary>
    private async Task<object> TestServerAsync(string? input, int agentPortOverride = 0)
    {
        string text = string.IsNullOrWhiteSpace(input) ? _state.Settings.ServerHost : input!;
        if (!ServerAddress.TryParse(text, out var addr, out var err) || addr is null)
            throw new InvalidOperationException(L.T("backend.badAddress", new { detail = err ?? "" }));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var r = await addr.ResolveAsync(timeout: TimeSpan.FromSeconds(3));
        // The SAME decision the persisting paths make (see AgentPortFor): Test must dial the port that
        // admin login will actually use, or an agent on a non-default listen port is reported unreachable
        // here and then connects fine on the very next click. A port typed on the start screen wins.
        int agentPort = ExplicitAgentPort(agentPortOverride) ?? AgentPortFor(r);
        var gameTask = ServerProbe.ProbeSdkAsync(r.Host, r.Port, TimeSpan.FromSeconds(4));
        object agent;
        try
        {
            using var pub = new AgentPublicClient(r.Host, agentPort, TimeSpan.FromSeconds(5));
            var st = await pub.StatusAsync();
            var up = new List<string>();
            if (st.TryGetProperty("versions", out var vs) && vs.ValueKind == JsonValueKind.Object)
                foreach (var v in vs.EnumerateObject())
                    if (v.Value.TryGetProperty("up", out var u) && u.ValueKind == JsonValueKind.True) up.Add(v.Name);
            agent = new
            {
                reachable = true,
                name = st.TryGetProperty("name", out var n) ? n.GetString() : null,
                versionsUp = up,
                signup = st.TryGetProperty("signup", out var sg) ? sg.Clone() : default,
                playerCommands = st.TryGetProperty("playerCommands", out var pc) && pc.ValueKind == JsonValueKind.True,
                status = st,
            };
        }
        catch (Exception ex)
        {
            agent = new { reachable = false, error = ex.Message };
        }
        var game = await gameTask;
        return new
        {
            host = r.Host, port = r.Port, agentPort, source = r.Source,
            game = new { reachable = game.Reachable, detail = game.Detail },
            agent,
            ms = sw.ElapsedMilliseconds,
        };
    }

    /// <summary>Admin login from the start screen. Transactional: the typed address is resolved but
    /// NOT persisted until the token is accepted — a refused login leaves the previous server in place.
    /// Token precedence: the typed one → the one stored for THIS host → the build's baked one. A typed
    /// token must be validated against the agent's /status (401 = refused; unreachable = cannot tell,
    /// so refused too); a token already stored for this host was validated before, so an unreachable
    /// agent lets the admin in with a warning (the server may simply be down right now — the launcher
    /// is still needed to install the game or start a local agent). <paramref name="agentPort"/>: the
    /// start screen's optional explicit agent port (0 = infer), the only way to reach an agent on a
    /// non-default listen port that no <c>_relic</c> TXT record publishes since the Settings form went.</summary>
    private async Task<object> AdminLoginAsync(string address, string token, int agentPort = 0)
    {
        var r = await ResolveAddressAsync(address);
        token = (token ?? "").Trim();
        var storedForHost = StoredServer() is { } st && ServerConfig.SameHost(st.Host, r.Host) ? st : null;
        bool typed = token.Length > 0;
        if (!typed) token = storedForHost?.AgentToken ?? "";
        if (token.Length == 0) token = BuildDefaults.AgentToken;
        if (token.Length == 0) throw new InvalidOperationException(L.T("backend.tokenRequired"));
        int? explicitPort = ExplicitAgentPort(agentPort);
        // An SSH tunnel override saved for this host keeps its SSH details; anything else is direct.
        var cfg = storedForHost is not null && !storedForHost.IsDirect
            ? storedForHost with { Host = r.Host, AgentToken = token }
            : new ServerConfig(r.Host, storedForHost?.Port ?? 22, storedForHost?.User ?? "root",
                storedForHost?.Password ?? "", token, explicitPort ?? AgentPortFor(r), "direct");
        string? warning = null;
        try
        {
            await using var a = new AgentClient(cfg);
            await a.ConnectAsync();
            await a.StatusAsync();
        }
        catch (AgentHttpException ex) when (ex.Status == 401)
        {
            throw new InvalidOperationException(L.T("backend.tokenRefused"));
        }
        catch (Exception ex)
        {
            if (typed || storedForHost is null)
                throw new InvalidOperationException(L.T("backend.agentUnreachable", new { detail = ex.Message }));
            warning = L.T("backend.agentUnreachableEnter", new { detail = ex.Message });
            Log.Info($"admin login: agent at {r.Host}:{cfg.AgentPort} unreachable ({ex.Message}) — entering with the stored token");
        }
        ApplyAddress(r, explicitPort);
        // Persist the token (DPAPI) for this host so the next start does not ask again.
        ServerConfigStore.Save(cfg);
        _server = cfg;
        _serverSaved = true;
        _state.Settings.Mode = "admin";
        _state.Save();
        Log.Info($"admin login ok: {r.Host} agent={cfg.AgentPort} mode={cfg.Mode}");
        return new { state = BuildInitState(), warning };
    }

    private void SetLoginMuted(bool muted)
    {
        _state.Settings.LoginMuted = muted;
        _state.Save();
    }

    private void SetLoginAnimOff(bool off)
    {
        _state.Settings.LoginAnimOff = off;
        _state.Save();
    }

    // ── version removal ──

    private object StartRemove(string versionId, bool deleteFiles)
    {
        if (string.IsNullOrWhiteSpace(versionId)) throw new InvalidOperationException(L.T("backend.missingVersion"));
        if (Interlocked.Exchange(ref _installBusy, 1) != 0)
            throw new InvalidOperationException(L.T("backend.install.busy"));
        _ = Task.Run(async () =>
        {
            try
            {
                await AwaitRecovery();
                var svc = new InstallService(_state, Environment.ProcessPath ?? "");
                var progress = new Progress<InstallProgress>(pr =>
                    PostEvent("remove.progress", new { versionId, phase = pr.Phase.ToString(), pr.Fraction, pr.Message }));
                var report = await svc.RemoveAsync(versionId, deleteFiles, progress);
                Log.Info($"remove done: {versionId} deleteFiles={deleteFiles} leftovers={report.Leftovers.Count}");
                PostEvent("remove.done", new { versionId, leftovers = report.Leftovers, state = BuildInitState() });
            }
            catch (Exception ex)
            {
                Log.Error($"remove failed: {versionId}", ex);
                PostEvent("remove.error", new { versionId, message = ex.Message });
            }
            finally { Volatile.Write(ref _installBusy, 0); }
        });
        return new { started = true };
    }

    // ── shell ──

    private static void OpenUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u) || (u.Scheme != "https" && u.Scheme != "http"))
            throw new InvalidOperationException("only http(s) links can be opened");
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(u.ToString()) { UseShellExecute = true });
    }

    /// <summary>The GIO server guide shipped with the build — <c>assets\GIO-guide.pdf</c>, a copy of
    /// the vendor's guide book with every document property stripped (no author, producer or dates).
    /// Opened with whatever the system uses for PDFs; a build without the file says so instead of
    /// failing silently.</summary>
    private static void OpenGuide()
    {
        string path = Path.Combine(AppContext.BaseDirectory, "assets", "GIO-guide.pdf");
        if (!File.Exists(path)) throw new InvalidOperationException(L.T("backend.guideMissing"));
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(path) { UseShellExecute = true });
    }

    // ── local Windows agent ──

    private static void FiddlerGuard()
    {
        if (FiddlerAutomation.IsRunning) throw new InvalidOperationException(L.T("backend.closeFiddlerFirst"));
    }

    /// <summary>Everything here is blocking and none of it touches the state or WinForms, so it runs
    /// OFF the UI thread: WebView2 raises the message there, and the probes below are two `netstat`
    /// runs, a `netsh` rule query, up to three interpreter probes with 8 s timeouts each and a
    /// directory walk of a folder that can hold a multi-GB hotpatch mirror. On the UI thread that is
    /// a frozen window every time the Server page opens.</summary>
    private Task<object> LocalAgentStatusAsync() => Task.Run<object>(async () =>
    {
        var c = LocalAgent.ReadConfig();
        int port = c?.ListenPort ?? ServerAddress.DefaultAgentPort;
        // FindRunning, not the pid file alone: an agent started by the HKCU Run value has no pid file and
        // read as "installed, stopped" — the next Start then spawned a second pythonw that died on the port.
        var (running, pid) = LocalAgent.FindRunning(port);
        bool healthy = running && await LocalAgent.IsHealthyAsync(port);
        var py = LocalAgent.FindPython();
        return new
        {
            installed = LocalAgent.IsInstalled,
            running,
            healthy,
            pid,
            port,
            host = c?.Host ?? "127.0.0.1",
            autostart = LocalAgent.AutostartEnabled,
            python = py is null ? null : new { py.Exe, py.Version },
            root = LocalAgent.Root,
            logPath = LocalAgent.LogPath,
            fiddlerRunning = FiddlerAutomation.IsRunning,
            firewallRule = LocalAgent.FirewallRuleExists(port),
            dir16 = c?.Dir16 ?? "",
            dir28 = c?.Dir28 ?? "",
            // The rest of what a re-install form pre-fills from the installed config, so "Install again"
            // does not reset the bind, the advertised address, the name or the MUIP host (agent 3.6): the form
            // writes GIO_BIND_IP and GIO_MUIP_HOST even blank, and MergeConfig lets every key the form writes
            // win, so a value saved from the Agent settings card survives only when the form carries it.
            // listen is the agent's effective value (its own loopback default when the file names none);
            // "" = no config. bindIp is the raw GIO_BIND_IP — host above is loopback whenever the agent
            // listens on loopback or another address, so it cannot stand in for it. Neither is a secret.
            listen = c?.Listen ?? "",
            bindIp = c?.BindIp ?? "",
            advertisedIp = c?.AdvertisedIp ?? "",
            advertisedHost = c?.AdvertisedHost ?? "",
            serverName = c?.ServerName ?? "",
            muipHost = c?.MuipHost ?? "",
            // Size of the agent folder for the uninstall dialog; null = unknown (the walk gave up).
            rootBytes = RootBytes(LocalAgent.Root, TimeSpan.FromSeconds(3)),
        };
    });

    /// <summary>Bytes under <paramref name="dir"/>, or null when the walk exceeds <paramref name="budget"/>:
    /// <c>hotpatch\</c> can hold GBs in thousands of files, and a status rpc must stay quick.</summary>
    private static long? RootBytes(string dir, TimeSpan budget)
    {
        try
        {
            if (!Directory.Exists(dir)) return 0;
            var sw = System.Diagnostics.Stopwatch.StartNew();
            long total = 0;
            var opts = new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true };
            foreach (string f in Directory.EnumerateFiles(dir, "*", opts))
            {
                if (sw.Elapsed > budget) return null;
                try { total += new FileInfo(f).Length; } catch { /* vanished mid-walk */ }
            }
            return total;
        }
        catch (Exception ex) { Log.Error("measuring the agent folder (non-fatal)", ex); return null; }
    }

    /// <summary>The port the agent on this PC listens on — one parser (<see cref="LocalAgent.ReadConfig"/>),
    /// the agent's own, so a quoted or BOM-prefixed line reads the same on both sides.</summary>
    private int LocalAgentPort() => LocalAgent.ReadConfig()?.ListenPort ?? ServerAddress.DefaultAgentPort;

    /// <summary>The start screen's facts about the agent on this PC: installed, and where the launcher
    /// would dial it. Cheap by design (two File.Exists, one small read, one registry read) — init is
    /// synchronous; whether it is RUNNING is learned when the button is pressed.</summary>
    private static object LocalAgentInit()
    {
        var c = LocalAgent.ReadConfig();
        return new
        {
            installed = LocalAgent.IsInstalled,
            host = c?.Host ?? "127.0.0.1",
            port = c?.ListenPort ?? ServerAddress.DefaultAgentPort,
            autostart = LocalAgent.AutostartEnabled,
        };
    }

    /// <summary>"Use the agent on this PC" from the start screen: enter admin mode with the locally
    /// installed agent, persisting EXACTLY what the install persists — transactional like
    /// <see cref="AdminLoginAsync"/>, nothing is saved until the config's token is accepted. Health FIRST,
    /// whatever the pid file says: an autostarted agent has no pid file, and StartAsync would spawn a
    /// second pythonw that dies on the bound port. The Fiddler guard applies only when a start is
    /// needed — entering with an already-running agent while Fiddler is up is not a local-server operation.</summary>
    private async Task<object> LocalAgentEnterAsync()
    {
        if (!OperatingSystem.IsWindows()) throw new InvalidOperationException(L.T("backend.localAgent.windowsOnly"));
        var c = LocalAgent.ReadConfig();
        if (c is null || !LocalAgent.IsInstalled) throw new InvalidOperationException(L.T("core.deploy.notInstalledLocally"));
        if (c.Token.Length == 0) throw new InvalidOperationException(L.T("backend.localAgent.noToken"));
        if (!await LocalAgent.IsHealthyAsync(c.ListenPort))
        {
            FiddlerGuard();
            if (Interlocked.Exchange(ref _serverJobBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
            try { await LocalAgent.StartAsync(c.ListenPort, s => Log.Info("local agent enter: " + s)); }
            finally { Volatile.Write(ref _serverJobBusy, 0); }
        }
        var cfg = new ServerConfig(c.Host, 22, "root", "", c.Token, c.ListenPort, "direct");
        try
        {
            await using var a = new AgentClient(cfg);
            await a.ConnectAsync();
            await a.StatusAsync();
        }
        catch (AgentHttpException ex) when (ex.Status == 401)
        {
            // The running process was started with another config (a hand edit after the start).
            throw new InvalidOperationException(L.T("backend.localAgent.tokenMismatch"));
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException(L.T("backend.agentUnreachable", new { detail = ex.Message }));
        }
        // From here on: the install's tail, verbatim (StartLocalAgentInstall).
        ServerConfigStore.Save(cfg);
        _server = cfg; _serverSaved = true;
        ApplyInstalledAgent(c.Host, c.ListenPort);
        Log.Info($"local agent enter: {c.Host}:{c.ListenPort}");
        return new { state = BuildInitState(), warning = (string?)null };
    }

    /// <summary>Remove the agent from this PC (start screen link or the local agent card). Refused
    /// while the agent runs a job (a killed bootstrap/provision/hotpatch leaves a half-done stack) and
    /// while this launcher follows one. Then <see cref="LocalAgent.Uninstall"/>, the saved connection is
    /// forgotten when it is THIS agent's (token match first — the host may be 127.0.0.1 or the bind IP —
    /// host+port second), and admin mode is left when no token remains for the configured server: the
    /// UI returns to the start screen. <c>Settings.ServerHost</c> is kept — the docker stack on this PC
    /// may still run and a player can still connect to it.</summary>
    private async Task<object> LocalAgentUninstallAsync(JsonElement p)
    {
        if (!OperatingSystem.IsWindows()) throw new InvalidOperationException(L.T("backend.localAgent.windowsOnly"));
        bool firewall = Bool(p, "firewall");
        var c = LocalAgent.ReadConfig();
        if (!LocalAgent.IsInstalled && !Directory.Exists(LocalAgent.Root))
            throw new InvalidOperationException(L.T("core.deploy.notInstalledLocally"));
        int port = c?.ListenPort ?? LocalAgentPort();
        if (Interlocked.Exchange(ref _serverJobBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
        try
        {
            // Refuse mid-job, like the Linux upgrade does. A 401 (the process runs with another token)
            // must not block the uninstall — we are about to kill it anyway; neither must an agent that
            // answers /health but not /status. Only a real "busy" object refuses.
            string? busyKind = null;
            if (c is { Token.Length: > 0 } && await LocalAgent.IsHealthyAsync(port))
            {
                try
                {
                    await using var a = new AgentClient(new ServerConfig(c.Host, 22, "root", "", c.Token, port, "direct"));
                    await a.ConnectAsync();
                    var st = await a.StatusAsync();
                    if (st.ValueKind == JsonValueKind.Object && st.TryGetProperty("busy", out var busy) && busy.ValueKind == JsonValueKind.Object)
                        busyKind = busy.TryGetProperty("kind", out var k) && k.ValueKind == JsonValueKind.String ? k.GetString() ?? "" : "";
                }
                catch (Exception ex) { Log.Info($"local agent uninstall: status probe skipped ({ex.Message})"); }
            }
            if (busyKind is not null)
                throw new InvalidOperationException(L.T("backend.localAgent.jobRunning", new { kind = busyKind }));
            // Off the UI thread: the agent root holds the hotpatch mirror (GBs in thousands of files,
            // each delete retried once), and the whole tree is walked entry by entry. On the WebView2
            // message thread that is a frozen window for as long as it takes. _serverJobBusy is
            // already held around this, so the rpc still answers exactly once.
            var rep = await Task.Run(() => LocalAgent.Uninstall(port, firewall, s => Log.Info("local agent uninstall: " + s)));
            if (IsThisPcsAgent(StoredServer(), c, port))
            {
                ServerConfigStore.Delete();
                _server = ServerConfig.BuiltInDefault();
                _serverSaved = false;
                Log.Info("local agent uninstall: the saved connection was this agent's — forgotten");
            }
            if (IsAdminMode && AdminServer() is null)
            {
                _state.Settings.Mode = "";
                _state.Save();
            }
            return new
            {
                report = new
                {
                    stopped = rep.Stopped,
                    autostartRemoved = rep.AutostartRemoved,
                    firewallRemoved = rep.FirewallRemoved,
                    firewallError = rep.FirewallError,
                    filesRemoved = rep.FilesRemoved,
                    leftovers = rep.Leftovers,
                },
                state = BuildInitState(),
            };
        }
        finally { Volatile.Write(ref _serverJobBusy, 0); }
    }

    /// <summary>Is <paramref name="cfg"/> a connection to the agent installed on THIS PC (config
    /// <paramref name="c"/>, listening on <paramref name="port"/>)? The token first — the saved host may be
    /// 127.0.0.1 or the bind IP, whichever the install chose — then the same host + port. Shared by the
    /// uninstall (forget the saved connection when it was this agent's) and the Agent settings card
    /// (<c>local</c>: offer a folder picker on this PC).</summary>
    private static bool IsThisPcsAgent(ServerConfig? cfg, LocalAgentConfig? c, int port) =>
        cfg is not null && ((c is not null && c.Token.Length > 0 && cfg.AgentToken == c.Token)
            || (ServerConfig.SameHost(cfg.Host, c?.Host ?? "127.0.0.1") && cfg.AgentPort == port));

    // ── agent settings (agent 3.6) ──

    /// <summary><c>agent.config.get</c>: the agent's settings (GET /agent/config) plus <c>local</c>. A 404 is an
    /// agent older than 3.6 (no such route) and comes back as <c>{tooOld: true}</c> for the card to say
    /// "update the agent" — not as an error toast.</summary>
    private async Task<object?> AgentConfigGetAsync()
    {
        JsonElement cfg;
        try { cfg = (JsonElement)(await WithAgent(a => a.AgentConfigGetAsync()))!; }
        catch (AgentHttpException ex) when (ex.Status == 404) { return new { tooOld = true }; }
        return AgentConfigReply(cfg);
    }

    /// <summary><c>agent.config.set</c>: write settings through POST /agent/config; the reply is the agent's (the
    /// GET body + applied / restartRequired) plus <c>local</c>. Refusals (400 naming the key, 409 for an
    /// environment-overridden key or a file the agent cannot write) surface with the agent's own text.</summary>
    private async Task<object?> AgentConfigSetAsync(IReadOnlyDictionary<string, string> set)
    {
        var cfg = (JsonElement)(await WithAgent(a => a.AgentConfigSetAsync(set)))!;
        return AgentConfigReply(cfg);
    }

    /// <summary>The <c>set</c> object of an <c>agent.config.set</c> payload as the strings the agent expects: a
    /// string as is, a bool as "1"/"0", a number in invariant culture (an integral value without a decimal
    /// part), null as "" (= remove the key's line). Anything else goes out as its raw JSON text so the agent
    /// refuses it with a 400 that names the key, instead of the change vanishing here. Absent / not an object
    /// = an empty set, which the agent refuses (400) too.</summary>
    private static Dictionary<string, string> AgentConfigValues(JsonElement p)
    {
        var d = new Dictionary<string, string>(StringComparer.Ordinal);
        if (p.ValueKind != JsonValueKind.Object || !p.TryGetProperty("set", out var set) || set.ValueKind != JsonValueKind.Object)
            return d;
        var inv = System.Globalization.CultureInfo.InvariantCulture;
        foreach (var kv in set.EnumerateObject())
        {
            var v = kv.Value;
            d[kv.Name] = v.ValueKind switch
            {
                JsonValueKind.String => v.GetString() ?? "",
                JsonValueKind.True => "1",
                JsonValueKind.False => "0",
                JsonValueKind.Null => "",
                JsonValueKind.Number => v.TryGetInt64(out long l) ? l.ToString(inv)
                    : v.TryGetDecimal(out decimal m) ? (m == decimal.Truncate(m) ? decimal.Truncate(m).ToString(inv) : m.ToString(inv))
                    : v.GetRawText(),
                _ => v.GetRawText(),
            };
        }
        return d;
    }

    /// <summary>The agent's /agent/config answer with <c>local</c> added: true when the configured admin server is
    /// the agent installed on THIS PC (<see cref="IsThisPcsAgent"/>) AND the agent says it runs on Windows —
    /// the cross-check that keeps a remote box whose admin reused this PC's token from being offered a local
    /// folder picker. A non-object answer is passed through untouched.</summary>
    private object AgentConfigReply(JsonElement cfg)
    {
        if (cfg.ValueKind != JsonValueKind.Object) return cfg;
        bool local = false;
        if (OperatingSystem.IsWindows()
            && cfg.TryGetProperty("platform", out var pl) && pl.ValueKind == JsonValueKind.String
            && string.Equals(pl.GetString(), "windows", StringComparison.OrdinalIgnoreCase))
        {
            var c = LocalAgent.ReadConfig();
            local = IsThisPcsAgent(AdminServer(), c, c?.ListenPort ?? ServerAddress.DefaultAgentPort);
        }
        var obj = JsonNode.Parse(cfg.GetRawText())!.AsObject();
        obj["local"] = local;
        return obj;
    }

    /// <summary><c>agent.restart</c>: restart the agent so its restart-kind settings apply, and wait until it
    /// answers again. Refused while this launcher follows a server job, and holds that same flag while it
    /// waits (the agent itself refuses new jobs while it restarts). <c>started</c> is read first, the restart
    /// POSTed, then /agent/config polled (<see cref="AgentClient.WaitForRestartAsync"/>: first after ~1.5 s,
    /// then every second, ≤ 60 s) until a new process answers. <c>back: false</c> is an outcome with its own
    /// <c>message</c>, not an exception: <c>restarted: false</c> = the agent answers but kept its old process (it
    /// could not start a new copy of itself — the pending settings did not apply); without <c>restarted</c> = no
    /// telling answer in time — the agent may simply take longer, or need a look on the box.</summary>
    private async Task<object> AgentRestartAsync()
    {
        var cfg = AdminServer() ?? throw new InvalidOperationException(L.T("backend.server.notConfigured"));
        if (Interlocked.Exchange(ref _serverJobBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
        try
        {
            await using var a = new AgentClient(cfg);
            await a.ConnectAsync();
            string before = AgentClient.StartedOf(await a.AgentConfigGetAsync());
            await a.AgentRestartAsync();
            Log.Info($"agent restart requested: {cfg.Host}:{cfg.AgentPort} (started {before})");
            var outcome = await a.WaitForRestartAsync(before, AgentClient.RestartWait);
            Log.Info("agent restart: " + outcome switch
            {
                RestartOutcome.Back => "back",
                RestartOutcome.NotRestarted => "still the old process (it could not start a new one; see the agent log)",
                _ => "no answer within " + AgentClient.RestartWait.TotalSeconds + " s",
            });
            return outcome switch
            {
                RestartOutcome.Back => (object)new { ok = true, back = true, restarted = true },
                RestartOutcome.NotRestarted => new
                {
                    ok = true,
                    back = false,
                    restarted = false,
                    message = L.T("backend.agent.restartFailed"),
                },
                _ => new
                {
                    ok = true,
                    back = false,
                    message = L.T("backend.agent.restartTimeout", new { seconds = (int)AgentClient.RestartWait.TotalSeconds }),
                },
            };
        }
        finally { Volatile.Write(ref _serverJobBusy, 0); }
    }

    /// <summary>The relocation mode of <c>server.relocate(.check)</c>, normalised (trimmed, lowercase) but NOT
    /// defaulted: <c>move</c> moves files, so an unknown value must reach the agent as itself and be refused
    /// (400), never be turned into a move here.</summary>
    private static string RelocateMode(string raw) => (raw ?? "").Trim().ToLowerInvariant();

    private static AgentInstallPlan ReadPlan(JsonElement p) => new()
    {
        Target = Str(p, "target").Length > 0 ? Str(p, "target") : AgentInstallPlan.TargetLinux,
        Dir16 = Str(p, "dir16").Trim(),
        Dir28 = Str(p, "dir28").Trim(),
        // "I already have this package" switched OFF in the form = download it (agent 3.4).
        Fetch16 = Bool(p, "fetch16"),
        Fetch28 = Bool(p, "fetch28"),
        BindIp = Str(p, "bindIp").Trim(),
        AdvertisedIp = Str(p, "advertisedIp").Trim(),
        AdvertisedHost = Str(p, "advertisedHost").Trim(),
        Listen = Str(p, "listen").Trim().Length > 0 ? Str(p, "listen").Trim() : "0.0.0.0:18080",
        Token = Str(p, "token").Trim(),
        MuipHost = Str(p, "muipHost").Trim(),
        // A secret like the token: it reaches the box inside install.env / the config file only and is
        // never echoed back in deploy.done / deploy.error.
        MuipKey = Str(p, "muipKey").Trim(),
        Region = Str(p, "region").Trim().Length > 0 ? Str(p, "region").Trim() : "dev_docker",
        ServerName = Str(p, "serverName").Trim(),
        UpgradeForce = Bool(p, "upgradeForce"),
    };

    /// <summary>Install + start the agent on this PC, then point the app at it (admin mode).</summary>
    private object StartLocalAgentInstall(JsonElement p)
    {
        FiddlerGuard();
        var plan = ReadPlan(p);
        // Read off the payload BEFORE the job starts: p points into the JsonDocument that OnMessage
        // disposes as soon as this returns, so touching it from inside Task.Run throws ObjectDisposedException.
        bool autostart = Bool(p, "autostart");
        plan.Target = AgentInstallPlan.TargetWindows;
        if (plan.Token.Length == 0) plan.Token = AgentInstallPlan.NewToken();
        var python = LocalAgent.FindPython() ?? throw new InvalidOperationException(L.T("core.deploy.noPython"));
        if (Interlocked.Exchange(ref _serverJobBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
        _ = Task.Run(async () =>
        {
            void log(string s) => PostEvent("deploy.log", new { target = "windows", text = s });
            try
            {
                var willFetch = LocalAgent.Install(plan, python, log);
                int pid = await LocalAgent.StartAsync(plan.ListenPort, log);
                if (autostart) { LocalAgent.SetAutostart(true); log(L.T("backend.localAgent.autostartOn")); }
                // Point the app at the local agent: the bind IP (reachable by others) only when the agent
                // really answers on it — a wildcard bind, or a bind on that very address — else loopback.
                // The SAME rule LocalAgent.ReadConfig().Host applies later ("Use the agent on this PC"),
                // so the two paths can never save different hosts for one agent.
                string host = LocalAgentConfig.HostFor(plan.BindIp, plan.ListenHost);
                var cfg = new ServerConfig(host, 22, "root", "", plan.Token, plan.ListenPort, "direct");
                ServerConfigStore.Save(cfg);
                _server = cfg; _serverSaved = true;
                ApplyInstalledAgent(host, plan.ListenPort);
                // fetch: the versions marked for download — the agent is up now, so the UI queues the
                // fetch jobs one after another (agent 3.4).
                PostEvent("deploy.done", new { target = "windows", pid, token = plan.Token, fetch = plan.FetchVersions(), state = BuildInitState() });
            }
            catch (Exception ex)
            {
                Log.Error("local agent install failed", ex);
                PostEvent("deploy.error", new { target = "windows", message = ex.Message, token = plan.Token });
            }
            finally { Volatile.Write(ref _serverJobBusy, 0); }
        });
        return new { started = true };
    }

    /// <summary>Install the agent on a remote Linux box over SSH (password or key, sudo-aware), then
    /// save the connection. Streams `deploy.log`, ends with `deploy.done` / `deploy.error`.</summary>
    private object StartRemoteAgentInstall(JsonElement p)
    {
        var plan = ReadPlan(p);
        plan.Target = AgentInstallPlan.TargetLinux;
        if (plan.Token.Length == 0) plan.Token = AgentInstallPlan.NewToken();
        var ssh = new SshTarget(
            Host: Str(p, "sshHost").Trim().Length > 0 ? Str(p, "sshHost").Trim() : _state.Settings.ServerHost,
            Port: Int(p, "sshPort", 22),
            User: Str(p, "sshUser").Trim().Length > 0 ? Str(p, "sshUser").Trim() : "root",
            Password: Str(p, "sshPassword"),
            KeyPath: Str(p, "sshKeyPath").Trim(),
            KeyPassphrase: Str(p, "sshKeyPassphrase"),
            SudoPassword: Str(p, "sudoPassword"));
        if (ssh.Host.Length == 0) throw new InvalidOperationException(L.T("backend.noServer"));
        if (Interlocked.Exchange(ref _serverJobBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
        _ = Task.Run(() =>
        {
            void log(string s) => PostEvent("deploy.log", new { target = "linux", text = s });
            try
            {
                var result = AgentDeployer.Install(ssh, plan, Path.Combine(AppContext.BaseDirectory, "agent"), log);
                var cfg = new ServerConfig(ssh.Host, ssh.Port, ssh.User, "", plan.Token, plan.ListenPort, "direct");
                ServerConfigStore.Save(cfg);
                _server = cfg; _serverSaved = true;
                ApplyInstalledAgent(ssh.Host, plan.ListenPort);
                // fetch: the versions marked for download, for the UI to queue as jobs — but only when
                // the agent answered /health: with a warning the download would be a certain failure,
                // and the card's own "Download the server" button is there once the agent is up.
                var fetch = result.Warning.Length > 0 ? Array.Empty<string>() : result.FetchVersions.ToArray();
                PostEvent("deploy.done", new { target = "linux", result.HostKeyFingerprint, token = plan.Token, warning = result.Warning, fetch, state = BuildInitState() });
            }
            catch (Exception ex)
            {
                // The token goes out with the failure too: the installer may already have written it on the
                // box, and it exists nowhere else — it is generated here and filtered out of the log stream.
                Log.Error("remote agent install failed", ex);
                PostEvent("deploy.error", new { target = "linux", message = ex.Message, token = plan.Token });
            }
            finally { Volatile.Write(ref _serverJobBusy, 0); }
        });
        return new { started = true };
    }

    // ── player accounts ──

    /// <summary>Create an account through the agent's token-less signup route (<c>/public/account/create</c>):
    /// the player card, and the admin card's fallback on an agent older than 3.5 — so the Player accounts
    /// policy and the signup limits apply. Streams <c>account.log</c>, ends with <c>account.done</c> /
    /// <c>account.error</c>; every event echoes <c>origin</c> ("player" | "admin") and <c>version</c> so the
    /// UI routes it to the card that asked, and can drop it when the server changed while it ran.
    /// <para>The target server (host, game port, agent port) is captured at the START: the job can take
    /// minutes (a busy slot is waited out), and a server switch meanwhile must neither send the rest of the
    /// follow to the new server nor file this account under the new server's key. <c>remember</c> (default
    /// true — the player card's own login; the admin fallback sends what the admin chose) files the created
    /// name as this PC's login for that version, only while the configured server is still the one it was
    /// created on.</para></summary>
    private object StartAccountCreate(JsonElement p)
    {
        string version = Str(p, "version"), name = Str(p, "name").Trim(), template = Str(p, "template").Trim();
        string? password = Str(p, "password");
        if (version.Length == 0 || name.Length == 0) throw new InvalidOperationException(L.T("backend.account.missingFields"));
        if (template.Length == 0) template = "fresh";
        bool remember = BoolOrNull(p, "remember") != false;
        string origin = Str(p, "origin") == "admin" ? "admin" : "player";
        string host = _state.Settings.ServerHost;
        int port = _state.Settings.ServerPort, agentPort = _state.Settings.AgentPort;
        if (string.IsNullOrWhiteSpace(host)) throw new InvalidOperationException(L.T("backend.noServer"));
        if (Interlocked.Exchange(ref _accountBusy, 1) != 0) throw new InvalidOperationException(L.T("backend.server.jobBusy"));
        // The player's own signups carry this launcher's id on that server: agent 3.7 counts them against the
        // version's maxPerPlayer. The admin card's fallback (an agent older than 3.5, which has no limit) sends
        // none — those accounts are the admin's friends', not the admin's own.
        string? client = null;
        if (origin == "player")
        {
            bool made = string.IsNullOrEmpty(_state.ClientSecret);
            client = _state.ClientIdFor(host);
            if (made) _state.Save();
        }
        _ = Task.Run(async () =>
        {
            try
            {
                using var pub = new AgentPublicClient(host, agentPort);
                var result = await pub.CreateAccountAndWaitAsync(version, name, password, template,
                    line => PostEvent("account.log", new { origin, version, text = line }), client: client);
                string created = result.ValueKind == JsonValueKind.Object && result.TryGetProperty("name", out var n)
                    && n.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(n.GetString()) ? n.GetString()!.Trim() : name;
                bool remembered = false;
                if (remember)
                {
                    string? gen = null;
                    try
                    {
                        var st = await pub.StatusAsync();
                        if (st.TryGetProperty("versions", out var vs) && vs.ValueKind == JsonValueKind.Object
                            && vs.TryGetProperty(version, out var vv) && vv.ValueKind == JsonValueKind.Object
                            && vv.TryGetProperty("generation", out var g) && g.ValueKind == JsonValueKind.String)
                            gen = g.GetString();
                    }
                    catch (Exception ex) { Log.Info($"account create: generation unknown ({ex.Message})"); }
                    // Same drop rule as RememberServerAccounts: the login belongs to the server it was created
                    // on, and a name filed under another server's key would be shown as THAT server's login.
                    if (ServerConfig.SameHost(_state.Settings.ServerHost, host) && _state.Settings.ServerPort == port)
                    {
                        _state.RememberAccount(host, port, version, created, gen);
                        _state.Save();
                        remembered = true;
                    }
                    else
                        Log.Info($"account created on {host}:{port} not remembered — dropped, the configured server moved to " +
                                 $"{_state.Settings.ServerHost}:{_state.Settings.ServerPort} while it ran");
                }
                Log.Info($"account create done: {version} origin={origin} remembered={remembered}");
                PostEvent("account.done", new { origin, version, result, remembered, state = BuildInitState() });
            }
            catch (Exception ex)
            {
                Log.Error("account create failed", ex);
                PostEvent("account.error", new { origin, version, message = ex.Message });
            }
            finally { Volatile.Write(ref _accountBusy, 0); }
        });
        return new { started = true };
    }

    /// <summary>The tail of an ADMIN account creation (<c>server.accountcreate</c>) with "Show it as my login
    /// on this PC": file the created name as this launcher's own login for that version, on the server it
    /// was created on (<paramref name="host"/>:<paramref name="port"/>, captured when the job started).
    /// Dropped — like <see cref="RememberServerAccounts"/> drops a late status answer — when the configured
    /// server moved meanwhile. Never throws: the account WAS created, and the job's result (the generated
    /// password included) must still reach the admin. Nothing about the password is logged or kept.</summary>
    private async Task RememberAdminCreatedAsync(AgentClient a, JsonElement snap, string version, string typed,
        string host, int port)
    {
        try
        {
            // RunJobAsync hands back the job's last snapshot {state, result: {name, …}}; an agent that did the
            // work inside the POST would return the result object itself. The agent's spelling wins over what
            // was typed (it is the name the player logs in with).
            static string? NameOf(JsonElement e) =>
                e.ValueKind == JsonValueKind.Object && e.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String
                    && !string.IsNullOrWhiteSpace(n.GetString()) ? n.GetString()!.Trim() : null;
            string created = (snap.ValueKind == JsonValueKind.Object && snap.TryGetProperty("result", out var r) ? NameOf(r) : null)
                ?? NameOf(snap) ?? typed;
            // The generation the Library compares against (/public/status "generation" = provisionedAt): a
            // re-provision with the default save wipes player accounts, and the stale login is then flagged.
            string? gen = null;
            try
            {
                var st = await a.StatusAsync();
                if (st.ValueKind == JsonValueKind.Object && st.TryGetProperty("versions", out var vs) && vs.ValueKind == JsonValueKind.Object
                    && vs.TryGetProperty(version, out var vv) && vv.ValueKind == JsonValueKind.Object
                    && vv.TryGetProperty("provisionedAt", out var g) && g.ValueKind == JsonValueKind.String)
                    gen = g.GetString();
            }
            catch (Exception ex) { Log.Info($"admin account create: generation unknown ({ex.Message})"); }
            if (!ServerConfig.SameHost(_state.Settings.ServerHost, host) || _state.Settings.ServerPort != port)
            {
                Log.Info($"admin-created account on {host}:{port} not remembered — dropped, the configured server moved to " +
                         $"{_state.Settings.ServerHost}:{_state.Settings.ServerPort} while it ran");
                return;
            }
            _state.RememberAccount(host, port, version, created, gen);
            _state.Save();
            Log.Info($"admin-created account remembered as this PC's login: {version} on {host}:{port}");
        }
        catch (Exception ex) { Log.Error("remembering the admin-created account (non-fatal)", ex); }
    }

    private async Task<object?> PublicStatusAsync()
    {
        using var pub = PublicClient();
        return await pub.StatusAsync();
    }

    /// <summary>Every account this launcher created on the CURRENT server, per version id, oldest first — the
    /// player card lists them (agent 3.7: several per player, up to the server's maxPerPlayer).</summary>
    private Dictionary<string, object> AccountListsForCurrentServer()
    {
        var d = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
        string host = _state.Settings.ServerHost;
        int port = _state.Settings.ServerPort;
        string key = $"{host}:{port}";
        var versions = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (_state.AccountLists.TryGetValue(key, out var lists)) versions.UnionWith(lists.Keys);
        if (_state.Accounts.TryGetValue(key, out var cur)) versions.UnionWith(cur.Keys);
        foreach (var v in versions)
            d[v] = _state.AccountListFor(host, port, v)
                .Select(a => new { name = a.Name, createdAt = a.CreatedAt, generation = a.Generation }).ToList();
        return d;
    }

    /// <summary>The login shown per version on the CURRENT server (one of <see cref="AccountListsForCurrentServer"/>).</summary>
    private Dictionary<string, object> AccountsForCurrentServer()
    {
        var d = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
        string key = $"{_state.Settings.ServerHost}:{_state.Settings.ServerPort}";
        if (_state.Accounts.TryGetValue(key, out var per))
            foreach (var kv in per)
                d[kv.Key] = new { name = kv.Value.Name, createdAt = kv.Value.CreatedAt, generation = kv.Value.Generation };
        return d;
    }

    /// <summary>The CURRENT server's last answer about its pre-made account per version id ("" = it said
    /// there is none); empty until the server answered once.</summary>
    private Dictionary<string, string> ServerAccountsForCurrentServer()
    {
        var d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string key = $"{_state.Settings.ServerHost}:{_state.Settings.ServerPort}";
        if (_state.ServerAccounts.TryGetValue(key, out var per))
            foreach (var kv in per) d[kv.Key] = kv.Value ?? "";
        return d;
    }

    private static bool Bool(JsonElement p, string name) =>
        p.ValueKind == JsonValueKind.Object && p.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    /// <summary>Tri-state read: true / false for a JSON bool, null when the field is absent or not a bool.
    /// <see cref="Bool"/> collapses "absent" into false, which is wrong for a field whose absence means
    /// "no decision" (server.setup's pathfinding).</summary>
    private static bool? BoolOrNull(JsonElement p, string name) =>
        p.ValueKind == JsonValueKind.Object && p.TryGetProperty(name, out var v)
            ? v.ValueKind switch { JsonValueKind.True => true, JsonValueKind.False => false, _ => null }
            : null;
}
